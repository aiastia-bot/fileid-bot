"""Webhook 服务器 - Standalone 和 Master 模式的 aiohttp 服务器"""
import asyncio
import logging
from typing import Optional
from telegram import Update
from telegram.ext import Application
from bot_manager import BotManager
from config import WEBHOOK_HOST, WEBHOOK_PATH, WEBHOOK_PORT, WEBHOOK_SECRET, WORKER_SECRET, MASTER_BOT_COMMANDS

logger = logging.getLogger(__name__)


def _build_webhook_handler(application: Application, bot_manager: BotManager):
    """构建通用的 webhook 请求处理器"""
    async def webhook_handler(request):
        path = request.path
        body = await request.json()

        # 验证 secret
        if WEBHOOK_SECRET:
            secret = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
            if secret != WEBHOOK_SECRET:
                logger.warning("webhook secret 验证失败: %s", path)
                return _web_response(403)

        # 分发到主Bot
        if path == f"{WEBHOOK_PATH}/master":
            try:
                update = Update.de_json(body, application.bot)
                await application.process_update(update)
            except Exception as e:
                logger.error("主Bot webhook 处理失败: %s", e)
            return _web_response(200)

        # 分发到用户Bot
        parts = path.rstrip('/').split('/')
        if len(parts) >= 2:
            try:
                bot_db_id = int(parts[-1])
                success = await bot_manager.handle_webhook_update(bot_db_id, body)
                if not success:
                    return _web_response(503)  # Telegram 自动重试
            except (ValueError, Exception) as e:
                logger.error("用户Bot webhook 处理失败: %s", e)
                return _web_response(503)
            return _web_response(200)

        return _web_response(404)

    return webhook_handler


def _build_on_startup(application: Application, bot_manager: BotManager,
                      scheduler=None, role: str = "standalone"):
    """构建 on_startup 回调，合并 Master 和 Standalone 的公共逻辑"""
    from aiohttp import web

    async def on_startup(app: web.Application):
        await application.initialize()
        await application.start()

        # 设置主Bot webhook
        master_webhook_url = f"https://{WEBHOOK_HOST}{WEBHOOK_PATH}/master"
        all_updates = list(Update.ALL_TYPES) + ['managed_bot']
        await application.bot.set_webhook(
            url=master_webhook_url,
            secret_token=WEBHOOK_SECRET or None,
            allowed_updates=all_updates,
            drop_pending_updates=True,
        )
        logger.info("主Bot webhook 已设置: %s", master_webhook_url)

        # 设置用户名引用
        bot_manager.master_bot_username = application.bot.username
        if scheduler:
            scheduler.master_bot_username = application.bot.username

        # 注册主Bot命令
        try:
            await application.bot.set_my_commands(MASTER_BOT_COMMANDS)
        except Exception as e:
            logger.warning("主Bot注册命令失败: %s", e)

        # 后台异步加载 Bot，不阻塞 webhook 服务器启动
        async def _bg_load_bots():
            try:
                if scheduler:
                    loaded = await scheduler.load_all_to_workers()
                    logger.info("✅ Master 后台加载完成，已分配 %d 个用户Bot", loaded)
                else:
                    loaded = await bot_manager.load_all()
                    logger.info("✅ Standalone 后台加载完成，共加载 %d 个用户Bot", loaded)
            except Exception as e:
                logger.error("后台加载 Bot 失败 (%s): %s", role, e)

        asyncio.create_task(_bg_load_bots())

        # 启动 webhook 定期验证
        await bot_manager.start_webhook_monitor()

    return on_startup


def _build_on_shutdown(application: Application, bot_manager: BotManager):
    """构建 on_shutdown 回调"""
    from aiohttp import web

    async def on_shutdown(app: web.Application):
        logger.info("正在停止所有Bot...")
        from send_queue import stop_all_queues
        await stop_all_queues()
        await bot_manager.stop_all()
        await application.stop()
        await application.shutdown()
        logger.info("所有Bot已停止")

    return on_shutdown


def _web_response(status: int):
    """快速创建 aiohttp Response（延迟导入）"""
    from aiohttp import web
    return web.Response(status=status)


def _register_master_routes(web_app, scheduler):
    """注册 Master 模式的内部管理 API 路由"""
    from aiohttp import web

    def _verify_internal_secret(request: web.Request) -> bool:
        if not WORKER_SECRET:
            return True
        return request.headers.get('X-Worker-Secret', '') == WORKER_SECRET

    async def handle_worker_register(request: web.Request):
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
        if not _verify_internal_secret(request):
            return web.json_response({'error': 'unauthorized'}, status=403)
        try:
            data = await request.json()
            await scheduler.handle_worker_offline(data['node_id'])
            return web.json_response({'status': 'ok'})
        except Exception as e:
            return web.json_response({'error': str(e)}, status=400)

    async def handle_worker_status(request: web.Request):
        if not _verify_internal_secret(request):
            return web.json_response({'error': 'unauthorized'}, status=403)
        workers = scheduler.get_worker_status()
        return web.json_response({'workers': workers})

    web_app.router.add_post("/internal/register", handle_worker_register)
    web_app.router.add_post("/internal/heartbeat", handle_worker_heartbeat)
    web_app.router.add_post("/internal/worker_offline", handle_worker_offline)
    web_app.router.add_get("/internal/workers", handle_worker_status)


def _run_webhook_server(application: Application, bot_manager: BotManager,
                        scheduler=None, role: str = "standalone"):
    """通用 webhook 服务器启动"""
    from aiohttp import web

    webhook_handler = _build_webhook_handler(application, bot_manager)

    async def health_handler(request: web.Request):
        return web.json_response({
            "status": "ok",
            "role": role,
            "mode": "webhook",
            "bots": bot_manager.active_count,
        })

    web_app = web.Application()
    # Webhook 路由
    web_app.router.add_post(f"{WEBHOOK_PATH}/master", webhook_handler)
    web_app.router.add_post(f"{WEBHOOK_PATH}/{{bot_id:\\d+}}", webhook_handler)
    # Master 模式的内部管理 API
    if scheduler:
        _register_master_routes(web_app, scheduler)
    # 健康检查
    web_app.router.add_get("/health", health_handler)

    web_app.on_startup.append(_build_on_startup(application, bot_manager, scheduler, role))
    web_app.on_shutdown.append(_build_on_shutdown(application, bot_manager))

    logger.info("🌐 %s webhook 服务器监听 0.0.0.0:%d", role.capitalize(), WEBHOOK_PORT)
    web.run_app(web_app, host="0.0.0.0", port=WEBHOOK_PORT, print=None)


def run_webhook_master(application: Application, bot_manager: BotManager, scheduler):
    """Master 的 Webhook 模式"""
    from scheduler import MasterScheduler  # noqa: F811 - 确保 type hint 正确
    _run_webhook_server(application, bot_manager, scheduler=scheduler, role="master")


def run_webhook(application: Application, bot_manager: BotManager):
    """Standalone 的 Webhook 模式"""
    _run_webhook_server(application, bot_manager, role="standalone")