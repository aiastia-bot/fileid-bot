"""Webhook 服务器 - Standalone 和 Master 模式的 aiohttp 服务器"""
import asyncio
import logging
from telegram import Update
from telegram.ext import Application
from bot_manager import BotManager
from config import WEBHOOK_HOST, WEBHOOK_PATH, WEBHOOK_PORT, WEBHOOK_SECRET, WORKER_SECRET, MASTER_BOT_COMMANDS
from scheduler import MasterScheduler

logger = logging.getLogger(__name__)

def run_webhook_master(application: Application, bot_manager: BotManager, scheduler: MasterScheduler):
    """Master 的 Webhook 模式：aiohttp 服务器 + 主Bot webhook + 内部管理API"""
    from aiohttp import web

    def _verify_internal_secret(request: web.Request) -> bool:
        if not WORKER_SECRET:
            return True
        return request.headers.get('X-Worker-Secret', '') == WORKER_SECRET

    async def webhook_handler(request: web.Request):
        """处理所有 webhook 请求"""
        path = request.path
        body = await request.json()

        # 验证 secret
        if WEBHOOK_SECRET:
            secret = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
            if secret != WEBHOOK_SECRET:
                logger.warning("webhook secret 验证失败: %s", path)
                return web.Response(status=403)

        # 分发到主Bot
        if path == f"{WEBHOOK_PATH}/master":
            try:
                update = Update.de_json(body, application.bot)
                await application.process_update(update)
            except Exception as e:
                logger.error("主Bot webhook 处理失败: %s", e)
            return web.Response(status=200)

        # 分发到本地用户Bot（fallback）
        parts = path.rstrip('/').split('/')
        if len(parts) >= 2:
            try:
                bot_db_id = int(parts[-1])
                success = await bot_manager.handle_webhook_update(bot_db_id, body)
                if not success:
                    return web.Response(status=503)  # Telegram 自动重试
            except (ValueError, Exception) as e:
                logger.error("用户Bot webhook 处理失败: %s", e)
                return web.Response(status=503)
            return web.Response(status=200)

        return web.Response(status=404)

    async def handle_worker_register(request: web.Request):
        """Worker 注册：POST /internal/register"""
        if not _verify_internal_secret(request):
            return web.json_response({'error': 'unauthorized'}, status=403)
        try:
            data = await request.json()
            await scheduler.handle_worker_register(
                node_id=data['node_id'],
                node_url=data.get('node_url', ''),
                webhook_host=data.get('webhook_host', ''),
                max_bots=data.get('max_bots', 100),
            )
            return web.json_response({'status': 'registered'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=400)

    async def handle_worker_heartbeat(request: web.Request):
        """Worker 心跳：POST /internal/heartbeat"""
        if not _verify_internal_secret(request):
            return web.json_response({'error': 'unauthorized'}, status=403)
        try:
            data = await request.json()
            await scheduler.handle_worker_heartbeat(
                node_id=data['node_id'],
                active_bots=data.get('active_bots', 0),
            )
            return web.json_response({'status': 'ok'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=400)

    async def handle_worker_offline(request: web.Request):
        """Worker 离线通知：POST /internal/worker_offline"""
        if not _verify_internal_secret(request):
            return web.json_response({'error': 'unauthorized'}, status=403)
        try:
            data = await request.json()
            await scheduler.handle_worker_offline(data['node_id'])
            return web.json_response({'status': 'ok'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=400)

    async def handle_worker_status(request: web.Request):
        """查看所有 Worker 状态：GET /internal/workers"""
        if not _verify_internal_secret(request):
            return web.json_response({'error': 'unauthorized'}, status=403)
        workers = scheduler.get_worker_status()
        return web.json_response({'workers': workers})

    async def health_handler(request: web.Request):
        """健康检查"""
        return web.json_response({
            "status": "ok",
            "role": "master",
            "mode": "webhook",
            "bots": bot_manager.active_count,
        })

    async def on_startup(app: web.Application):
        await application.initialize()
        await application.start()

        master_webhook_url = f"https://{WEBHOOK_HOST}{WEBHOOK_PATH}/master"
        all_updates = list(Update.ALL_TYPES) + ['managed_bot']
        await application.bot.set_webhook(
            url=master_webhook_url,
            secret_token=WEBHOOK_SECRET or None,
            allowed_updates=all_updates,
            drop_pending_updates=True,
        )
        logger.info("主Bot webhook 已设置: %s", master_webhook_url)

        bot_manager.master_bot_username = application.bot.username
        scheduler.master_bot_username = application.bot.username

        # 注册主Bot命令
        try:
            await application.bot.set_my_commands(MASTER_BOT_COMMANDS)
        except Exception as e:
            logger.warning("主Bot注册命令失败: %s", e)

        # 后台异步加载 Bot，不阻塞 webhook 服务器启动
        async def _bg_load_bots():
            try:
                loaded = await scheduler.load_all_to_workers()
                logger.info("✅ Master 后台加载完成，已分配 %d 个用户Bot", loaded)
            except Exception as e:
                logger.error("Master 后台加载 Bot 失败: %s", e)

        asyncio.create_task(_bg_load_bots())

        # 启动 webhook 定期验证（防止用户在别处使用 Bot Token）
        await bot_manager.start_webhook_monitor()

    async def on_shutdown(app: web.Application):
        logger.info("正在停止所有Bot...")
        from send_queue import stop_all_queues
        await stop_all_queues()
        await bot_manager.stop_all()
        await application.stop()
        await application.shutdown()
        logger.info("所有Bot已停止")

    web_app = web.Application()
    # Webhook 路由
    web_app.router.add_post(f"{WEBHOOK_PATH}/master", webhook_handler)
    web_app.router.add_post(f"{WEBHOOK_PATH}/{{bot_id:\\d+}}", webhook_handler)
    # 内部管理 API
    web_app.router.add_post("/internal/register", handle_worker_register)
    web_app.router.add_post("/internal/heartbeat", handle_worker_heartbeat)
    web_app.router.add_post("/internal/worker_offline", handle_worker_offline)
    web_app.router.add_get("/internal/workers", handle_worker_status)
    # 健康检查
    web_app.router.add_get("/health", health_handler)

    web_app.on_startup.append(on_startup)
    web_app.on_shutdown.append(on_shutdown)

    logger.info("🌐 Master webhook 服务器监听 0.0.0.0:%d", WEBHOOK_PORT)
    web.run_app(web_app, host="0.0.0.0", port=WEBHOOK_PORT, print=None)



def run_webhook(application: Application, bot_manager: BotManager):
    """Standalone 的 Webhook 模式"""
    from aiohttp import web

    async def webhook_handler(request: web.Request):
        path = request.path
        body = await request.json()

        if WEBHOOK_SECRET:
            secret = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
            if secret != WEBHOOK_SECRET:
                logger.warning("webhook secret 验证失败: %s", path)
                return web.Response(status=403)

        # 分发到主Bot
        if path == f"{WEBHOOK_PATH}/master":
            try:
                update = Update.de_json(body, application.bot)
                await application.process_update(update)
            except Exception as e:
                logger.error("主Bot webhook 处理失败: %s", e)
            return web.Response(status=200)

        # 分发到用户Bot
        parts = path.rstrip('/').split('/')
        if len(parts) >= 2:
            try:
                bot_db_id = int(parts[-1])
                success = await bot_manager.handle_webhook_update(bot_db_id, body)
                if not success:
                    return web.Response(status=503)  # Telegram 自动重试
            except (ValueError, Exception) as e:
                logger.error("用户Bot webhook 处理失败: %s", e)
                return web.Response(status=503)
            return web.Response(status=200)

        return web.Response(status=404)

    async def health_handler(request: web.Request):
        return web.json_response({
            "status": "ok",
            "role": "standalone",
            "mode": "webhook",
            "bots": bot_manager.active_count,
        })

    async def on_startup(app: web.Application):
        await application.initialize()
        await application.start()

        master_webhook_url = f"https://{WEBHOOK_HOST}{WEBHOOK_PATH}/master"
        all_updates = list(Update.ALL_TYPES) + ['managed_bot']
        await application.bot.set_webhook(
            url=master_webhook_url,
            secret_token=WEBHOOK_SECRET or None,
            allowed_updates=all_updates,
            drop_pending_updates=True,
        )
        logger.info("主Bot webhook 已设置: %s", master_webhook_url)

        bot_manager.master_bot_username = application.bot.username

        try:
            await application.bot.set_my_commands(MASTER_BOT_COMMANDS)
        except Exception as e:
            logger.warning("主Bot注册命令失败: %s", e)

        # 后台异步加载 Bot，不阻塞 webhook 服务器启动
        async def _bg_load_bots():
            try:
                loaded = await bot_manager.load_all()
                logger.info("✅ Standalone 后台加载完成，共加载 %d 个用户Bot", loaded)
            except Exception as e:
                logger.error("Standalone 后台加载 Bot 失败: %s", e)

        asyncio.create_task(_bg_load_bots())

        # 启动 webhook 定期验证（防止用户在别处使用 Bot Token）
        await bot_manager.start_webhook_monitor()

    async def on_shutdown(app: web.Application):
        logger.info("正在停止所有Bot...")
        from send_queue import stop_all_queues
        await stop_all_queues()
        await bot_manager.stop_all()
        await application.stop()
        await application.shutdown()
        logger.info("所有Bot已停止")

    web_app = web.Application()
    web_app.router.add_post(f"{WEBHOOK_PATH}/master", webhook_handler)
    web_app.router.add_post(f"{WEBHOOK_PATH}/{{bot_id:\\d+}}", webhook_handler)
    web_app.router.add_get("/health", health_handler)
    web_app.on_startup.append(on_startup)
    web_app.on_shutdown.append(on_shutdown)

    logger.info("🌐 webhook 服务器监听 0.0.0.0:%d", WEBHOOK_PORT)
    web.run_app(web_app, host="0.0.0.0", port=WEBHOOK_PORT, print=None)

