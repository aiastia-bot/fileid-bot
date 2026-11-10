"""
Master 调度器
在 master 模式下，将用户 Bot 分配到 Worker 节点运行
"""
import logging
import asyncio
from urllib.parse import urlsplit
from typing import Dict, List, Optional

from db import (
    get_all_active_user_bots, get_best_worker_node,
    register_worker_node, update_worker_heartbeat, set_worker_offline,
    update_user_bot_node, get_user_bot_by_id,
)
from config import WORKER_SECRET, MAX_BOTS_PER_WORKER

logger = logging.getLogger(__name__)


class MasterScheduler:
    """
    Master 调度器
    在 master 模式下，将用户 Bot 分配到 Worker 节点运行
    自己不直接运行任何用户 Bot（或作为 fallback 本地运行）
    """

    def __init__(self):
        self.master_bot_username: str = ""
        self._assign_lock = asyncio.Lock()
        self._task = None

    async def assign_bot_to_worker(self, bot_record: dict) -> Optional[str]:
        async with self._assign_lock:
            return await self._assign_bot(bot_record)

    async def _assign_bot(self, bot_record: dict) -> Optional[str]:
        """将 Bot 分配到最空闲的 Worker 节点，返回 node_id"""
        import httpx

        from db import get_all_worker_nodes
        nodes = {n['node_id']: n for n in await get_all_worker_nodes()}
        preferred = nodes.get(bot_record.get('node_id'))
        node = preferred if preferred and preferred['status'] == 'online' else await get_best_worker_node()
        if not node:
            logger.warning("没有可用的 Worker 节点")
            return None

        node_id = node['node_id']
        node_url = node['node_url']
        bot_db_id = bot_record['id']

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{node_url}/internal/start",
                    json={
                        'bot_db_id': bot_db_id,
                        'bot_token': bot_record['bot_token'],
                        'bot_id': bot_record.get('bot_id'),
                        'bot_username': bot_record.get('bot_username', ''),
                        'bot_firstname': bot_record.get('bot_firstname', ''),
                        'owner_id': bot_record.get('owner_id', 0),
                        'forward_mode': bot_record.get('forward_mode', 0),
                        'auto_delete': bot_record.get('auto_delete', 0),
                    },
                    headers={'X-Worker-Secret': WORKER_SECRET},
                    timeout=120.0
                )

                if resp.status_code == 200:
                    # 更新数据库中的节点分配
                    await update_user_bot_node(bot_db_id, node_id)
                    from db.workers import reserve_worker_slot
                    if not resp.json().get('already_running'):
                        await reserve_worker_slot(node_id)
                    logger.info("Bot @%s 已分配到 Worker %s", bot_record.get('bot_username', ''), node_id)
                    return node_id
                else:
                    if resp.status_code == 507 and preferred:
                        return await self._assign_bot(dict(bot_record, node_id='local'))
                    logger.error("Worker %s 启动 Bot 失败: %s", node_id, resp.text)
                    return None

        except Exception as e:
            logger.error("联系 Worker %s 失败: %s", node_id, e)
            # Heartbeat expiry decides liveness; one slow Bot must not evict an entire worker.
            return None

    async def stop_bot_on_worker(self, bot_db_id: int) -> bool:
        """通知 Worker 停止一个 Bot"""
        import httpx

        bot = await get_user_bot_by_id(bot_db_id)
        if not bot:
            return False

        node_id = bot.get('node_id', 'local')
        if node_id == 'local':
            return True

        # 查找 Worker 节点
        from db import get_all_worker_nodes
        nodes = {n['node_id']: n for n in await get_all_worker_nodes()}
        node = nodes.get(node_id)
        if not node or node['status'] != 'online':
            logger.warning("Worker %s 不在线，无法停止 Bot", node_id)
            return True

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{node['node_url']}/internal/stop",
                    json={'bot_db_id': bot_db_id},
                    headers={'X-Worker-Secret': WORKER_SECRET},
                    timeout=15.0
                )
                return resp.status_code == 200
        except Exception as e:
            logger.error("通知 Worker %s 停止 Bot 失败: %s", node_id, e)
            return False

    async def load_all_to_workers(self) -> int:
        """将所有活跃 Bot 分配到 Worker 节点"""
        bots = await get_all_active_user_bots()
        logger.info("开始分配 %d 个 Bot 到 Worker 节点", len(bots))

        loaded = 0
        for bot in bots:
            node_id = bot.get('node_id', 'local')

            # 已有分配且 Worker 在线，重新启动
            if node_id and node_id != 'local':
                assigned = await self.assign_bot_to_worker(bot)
                if assigned:
                    loaded += 1
                    logger.info("  ✅ @%s → %s", bot.get('bot_username', ''), assigned)
                else:
                    logger.error("  ❌ @%s 分配失败", bot.get('bot_username', ''))
            else:
                # 尚未分配，分配到最空闲的 Worker
                assigned = await self.assign_bot_to_worker(bot)
                if assigned:
                    loaded += 1
                    logger.info("  ✅ @%s → %s", bot.get('bot_username', ''), assigned)
                else:
                    logger.error("  ❌ @%s 无可用 Worker", bot.get('bot_username', ''))

        return loaded

    async def handle_worker_register(self, node_id: str, node_url: str,
                                      webhook_host: str = '',
                                      max_bots: int = MAX_BOTS_PER_WORKER) -> bool:
        """处理 Worker 注册请求"""
        parsed = urlsplit(node_url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Worker URL must be an http(s) origin without credentials')
        if not node_id or not 1 <= max_bots <= 10000:
            raise ValueError('Invalid worker ID or capacity')
        if not await register_worker_node(node_id, node_url.rstrip('/'), webhook_host, max_bots):
            raise RuntimeError('Worker registration failed')
        logger.info("Worker [%s] 已注册 (url=%s, webhook=%s)", node_id, node_url, webhook_host)
        return True

    async def handle_worker_heartbeat(self, node_id: str, active_bots: int) -> bool:
        """处理 Worker 心跳"""
        return await update_worker_heartbeat(node_id, active_bots)

    async def handle_worker_offline(self, node_id: str) -> bool:
        """处理 Worker 离线"""
        await set_worker_offline(node_id)
        logger.warning("Worker [%s] 已离线", node_id)
        return True

    async def get_worker_status(self) -> List[Dict]:
        """获取所有 Worker 状态"""
        from db import get_all_worker_nodes
        return await get_all_worker_nodes()

    async def notify_settings_update(self, bot_db_id: int, settings: dict) -> bool:
        """通知 Worker 更新运行中 Bot 的内存设置
        
        settings: {"forward_mode": 1} 或 {"auto_delete": 30} 等
        """
        import httpx

        bot = await get_user_bot_by_id(bot_db_id)
        if not bot:
            return False

        node_id = bot.get('node_id', 'local')
        if node_id == 'local':
            # standalone 模式，无需通知
            return True

        # 查找 Worker 节点
        from db import get_all_worker_nodes
        nodes = {n['node_id']: n for n in await get_all_worker_nodes()}
        node = nodes.get(node_id)
        if not node or node['status'] != 'online':
            logger.warning("Worker %s 不在线，无法更新设置", node_id)
            return False

        try:
            payload = {'bot_db_id': bot_db_id}
            payload.update(settings)
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{node['node_url']}/internal/update_settings",
                    json=payload,
                    headers={'X-Worker-Secret': WORKER_SECRET},
                    timeout=10.0
                )
                if resp.status_code == 200:
                    logger.info("已通知 Worker %s 更新 Bot %d 设置: %s", node_id, bot_db_id, settings)
                    return True
                else:
                    logger.warning("Worker %s 更新设置失败: %s", node_id, resp.text)
                    return False
        except Exception as e:
            logger.error("通知 Worker %s 更新设置失败: %s", node_id, e)
            return False


    def start(self):
        async def loop():
            while True:
                await asyncio.sleep(30)
                try:
                    from db.workers import expire_workers
                    await expire_workers()
                    from db import get_all_worker_nodes
                    nodes = {n['node_id']: n for n in await get_all_worker_nodes()}
                    for bot in await get_all_active_user_bots():
                        node = nodes.get(bot.get('node_id'))
                        if not node or node['status'] != 'online':
                            await self.assign_bot_to_worker(bot)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception('Worker reconciliation failed')
        self._task = asyncio.create_task(loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
