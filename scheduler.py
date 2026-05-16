"""
Master 调度器
在 master 模式下，将用户 Bot 分配到 Worker 节点运行
"""
import logging
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

    async def assign_bot_to_worker(self, bot_record: dict) -> Optional[str]:
        """将 Bot 分配到最空闲的 Worker 节点，返回 node_id"""
        import httpx

        node = await get_best_worker_node()
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
                    },
                    headers={'X-Worker-Secret': WORKER_SECRET},
                    timeout=30.0
                )

                if resp.status_code == 200:
                    # 更新数据库中的节点分配
                    await update_user_bot_node(bot_db_id, node_id)
                    logger.info("Bot @%s 已分配到 Worker %s", bot_record.get('bot_username', ''), node_id)
                    return node_id
                else:
                    logger.error("Worker %s 启动 Bot 失败: %s", node_id, resp.text)
                    return None

        except Exception as e:
            logger.error("联系 Worker %s 失败: %s", node_id, e)
            await set_worker_offline(node_id)
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
        await register_worker_node(node_id, node_url, webhook_host, max_bots)
        logger.info("Worker [%s] 已注册 (url=%s, webhook=%s)", node_id, node_url, webhook_host)
        return True

    async def handle_worker_heartbeat(self, node_id: str, active_bots: int) -> bool:
        """处理 Worker 心跳"""
        await update_worker_heartbeat(node_id, active_bots)
        return True

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
