"""
Worker 节点服务
接收 Master 节点的指令，管理本地运行的用户 Bot
提供内部 HTTP API：启动/停止 Bot、状态查询、健康检查
"""
import asyncio
import logging
import time
from typing import Dict, Optional

from aiohttp import web
import httpx

from config import (
    WORKER_SECRET, WORKER_PORT, MASTER_URL, NODE_ID,
    MAX_BOTS_PER_WORKER, HEALTH_CHECK_INTERVAL,
    API_READ_TIMEOUT, API_WRITE_TIMEOUT, API_CONNECT_TIMEOUT,
    WEBHOOK_SECRET, WEBHOOK_PATH
)

logger = logging.getLogger(__name__)


def _verify_secret(request: web.Request) -> bool:
    """验证内部通信密钥"""
    if not WORKER_SECRET:
        return True
    auth = request.headers.get('X-Worker-Secret', '')
    return auth == WORKER_SECRET


class WorkerServer:
    """
    Worker 节点服务
    管理本地 Bot 实例，响应 Master 的指令
    """

    def __init__(self):
        self._bot_manager = None  # 由 main.py 注入
        self._app: Optional[web.Application] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    def set_bot_manager(self, bot_manager):
        """注入 BotManager 实例"""
        self._bot_manager = bot_manager

    def create_app(self) -> web.Application:
        """创建 aiohttp 应用，注册路由"""
        app = web.Application()

        # 内部管理 API（Master 调用）
        app.router.add_post('/internal/start', self._handle_start)
        app.router.add_post('/internal/stop', self._handle_stop)
        app.router.add_post('/internal/restart', self._handle_restart)
        app.router.add_get('/internal/status', self._handle_status)
        app.router.add_get('/internal/health', self._handle_health)

        # Webhook 处理（Telegram 消息入口）
        app.router.add_post(f"{WEBHOOK_PATH}/{{bot_id:int}}", self._handle_webhook)

        # 启动/停止时的钩子
        app.on_startup.append(self._on_startup)
        app.on_shutdown.append(self._on_shutdown)

        self._app = app
        return app

    # ==================== 内部 API ====================

    async def _handle_start(self, request: web.Request) -> web.Response:
        """启动一个 Bot：POST /internal/start
        Body: {"bot_db_id": 1, "bot_token": "xxx", "bot_username": "xxx", ...}
        """
        if not _verify_secret(request):
            return web.json_response({'error': 'unauthorized'}, status=403)

        try:
            data = await request.json()
        except Exception:
            return web.json_response({'error': 'invalid json'}, status=400)

        bot_db_id = data.get('bot_db_id')
        bot_token = data.get('bot_token')
        if not bot_db_id or not bot_token:
            return web.json_response({'error': 'missing bot_db_id or bot_token'}, status=400)

        # 检查容量
        if self._bot_manager and self._bot_manager.active_count >= MAX_BOTS_PER_WORKER:
            return web.json_response({'error': 'worker full', 'active_count': self._bot_manager.active_count}, status=507)

        # 构建 bot_record
        bot_record = {
            'id': bot_db_id,
            'owner_id': data.get('owner_id', 0),
            'bot_token': bot_token,
            'bot_id': data.get('bot_id'),
            'bot_username': data.get('bot_username', 'unknown'),
            'bot_firstname': data.get('bot_firstname', ''),
        }

        if not self._bot_manager:
            return web.json_response({'error': 'bot_manager not ready'}, status=503)

        success = await self._bot_manager.start_bot(bot_record)
        if success:
            logger.info("Worker 接收 Bot @%s (db_id=%s) 启动成功", bot_record.get('bot_username'), bot_db_id)
            return web.json_response({'status': 'started', 'bot_db_id': bot_db_id})
        else:
            logger.error("Worker 接收 Bot @%s (db_id=%s) 启动失败", bot_record.get('bot_username'), bot_db_id)
            return web.json_response({'error': 'start failed'}, status=500)

    async def _handle_stop(self, request: web.Request) -> web.Response:
        """停止一个 Bot：POST /internal/stop
        Body: {"bot_db_id": 1}
        """
        if not _verify_secret(request):
            return web.json_response({'error': 'unauthorized'}, status=403)

        try:
            data = await request.json()
        except Exception:
            return web.json_response({'error': 'invalid json'}, status=400)

        bot_db_id = data.get('bot_db_id')
        if not bot_db_id:
            return web.json_response({'error': 'missing bot_db_id'}, status=400)

        if not self._bot_manager:
            return web.json_response({'error': 'bot_manager not ready'}, status=503)

        success = await self._bot_manager.stop_bot(int(bot_db_id))
        if success:
            logger.info("Worker 停止 Bot (db_id=%s) 成功", bot_db_id)
            return web.json_response({'status': 'stopped', 'bot_db_id': bot_db_id})
        else:
            return web.json_response({'error': 'bot not found or already stopped'}, status=404)

    async def _handle_restart(self, request: web.Request) -> web.Response:
        """重启一个 Bot：POST /internal/restart
        Body: {"bot_db_id": 1, "bot_token": "xxx", ...}
        """
        if not _verify_secret(request):
            return web.json_response({'error': 'unauthorized'}, status=403)

        try:
            data = await request.json()
        except Exception:
            return web.json_response({'error': 'invalid json'}, status=400)

        bot_db_id = data.get('bot_db_id')
        if not bot_db_id:
            return web.json_response({'error': 'missing bot_db_id'}, status=400)

        if not self._bot_manager:
            return web.json_response({'error': 'bot_manager not ready'}, status=503)

        # 先停止
        await self._bot_manager.stop_bot(int(bot_db_id))

        # 再启动
        bot_record = {
            'id': bot_db_id,
            'owner_id': data.get('owner_id', 0),
            'bot_token': data.get('bot_token'),
            'bot_id': data.get('bot_id'),
            'bot_username': data.get('bot_username', 'unknown'),
            'bot_firstname': data.get('bot_firstname', ''),
        }

        success = await self._bot_manager.start_bot(bot_record)
        if success:
            return web.json_response({'status': 'restarted', 'bot_db_id': bot_db_id})
        else:
            return web.json_response({'error': 'restart failed'}, status=500)

    async def _handle_status(self, request: web.Request) -> web.Response:
        """获取 Worker 状态：GET /internal/status"""
        if not _verify_secret(request):
            return web.json_response({'error': 'unauthorized'}, status=403)

        active_count = self._bot_manager.active_count if self._bot_manager else 0
        bot_list = []
        if self._bot_manager:
            for bot_db_id, app in self._bot_manager.get_all_apps().items():
                try:
                    bot_list.append({
                        'bot_db_id': bot_db_id,
                        'username': app.bot.username,
                    })
                except Exception:
                    bot_list.append({'bot_db_id': bot_db_id, 'username': 'unknown'})

        return web.json_response({
            'node_id': NODE_ID,
            'status': 'online',
            'active_bots': active_count,
            'max_bots': MAX_BOTS_PER_WORKER,
            'uptime': time.time(),
            'bots': bot_list,
        })

    async def _handle_health(self, request: web.Request) -> web.Response:
        """健康检查（不需要鉴权）：GET /internal/health"""
        return web.json_response({
            'status': 'ok',
            'node_id': NODE_ID,
            'active_bots': self._bot_manager.active_count if self._bot_manager else 0,
        })

    # ==================== Webhook 处理 ====================

    async def _handle_webhook(self, request: web.Request) -> web.Response:
        """处理 Telegram webhook 更新，分发给对应的 Bot"""
        bot_db_id = int(request.match_info['bot_id'])
        try:
            body = await request.json()
        except Exception:
            return web.Response(status=400)

        if not self._bot_manager:
            return web.Response(status=503)

        success = await self._bot_manager.handle_webhook_update(bot_db_id, body)
        if success:
            return web.Response(status=200)
        else:
            return web.Response(status=404)

    # ==================== 心跳 ====================

    async def _on_startup(self, app: web.Application):
        """启动时：向 Master 注册，开始心跳"""
        logger.info("Worker [%s] 启动，监听端口 %d", NODE_ID, WORKER_PORT)

        # 向 Master 注册
        await self._register_to_master()

        # 启动心跳任务
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def _on_shutdown(self, app: web.Application):
        """关闭时：停止心跳，通知 Master"""
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        # 停止所有 Bot
        if self._bot_manager:
            await self._bot_manager.stop_all()

        # 通知 Master 离线
        await self._notify_master_offline()
        logger.info("Worker [%s] 已关闭", NODE_ID)

    async def _register_to_master(self):
        """向 Master 注册自己"""
        if not MASTER_URL:
            logger.warning("未配置 MASTER_URL，跳过注册")
            return

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{MASTER_URL}/internal/register",
                    json={
                        'node_id': NODE_ID,
                        'active_bots': self._bot_manager.active_count if self._bot_manager else 0,
                    },
                    headers={'X-Worker-Secret': WORKER_SECRET},
                    timeout=10.0
                )
                if resp.status_code == 200:
                    logger.info("Worker [%s] 已向 Master 注册成功", NODE_ID)
                else:
                    logger.warning("Worker [%s] 注册失败: %s", NODE_ID, resp.text)
        except Exception as e:
            logger.warning("Worker [%s] 注册失败（Master 可能未就绪）: %s", NODE_ID, e)

    async def _heartbeat_loop(self):
        """定期向 Master 发送心跳"""
        while True:
            try:
                await asyncio.sleep(HEALTH_CHECK_INTERVAL)
                if not MASTER_URL:
                    continue

                active_count = self._bot_manager.active_count if self._bot_manager else 0
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        f"{MASTER_URL}/internal/heartbeat",
                        json={
                            'node_id': NODE_ID,
                            'active_bots': active_count,
                        },
                        headers={'X-Worker-Secret': WORKER_SECRET},
                        timeout=10.0
                    )
                    if resp.status_code != 200:
                        logger.warning("心跳发送失败: %s", resp.text)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("心跳异常: %s", e)

    async def _notify_master_offline(self):
        """通知 Master 自己已离线"""
        if not MASTER_URL:
            return

        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{MASTER_URL}/internal/worker_offline",
                    json={'node_id': NODE_ID},
                    headers={'X-Worker-Secret': WORKER_SECRET},
                    timeout=5.0
                )
        except Exception:
            pass

    def run(self):
        """启动 Worker HTTP 服务"""
        app = self.create_app()
        web.run_app(app, host="0.0.0.0", port=WORKER_PORT, print=None)