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
    from services.security import webhook_authorized
    async def webhook_handler(request):
        if not webhook_authorized(request):
            return _web_response(403)
        try:
            body = await request.json()
            key = 'master' if request.path == f"{WEBHOOK_PATH}/master" else request.match_info['bot_id']
            await request.app['inbox'].accept(key, body)
        except (ValueError, TypeError, KeyError):
            return _web_response(400)
        except Exception:
            logger.exception('Durable webhook acceptance failed')
            return _web_response(503)
        return _web_response(200)
    return webhook_handler


def _build_on_startup(application: Application, bot_manager: BotManager,
                      scheduler=None, role: str = "standalone"):
    async def on_startup(app):
        from db import init_db
        from services.inbox import Inbox
        await init_db()
        from services.leases import acquire
        if not await acquire('master'):
            raise RuntimeError('Another master process is active')
        await application.initialize()
        await application.start()
        app['inbox'] = Inbox(lambda key: application if key == 'master' else bot_manager.get_all_apps().get(int(key)))
        app['inbox'].start()
        await application.bot.set_webhook(
            url=f"https://{WEBHOOK_HOST}{WEBHOOK_PATH}/master",
            secret_token=WEBHOOK_SECRET, allowed_updates=list(Update.ALL_TYPES)+['managed_bot'],
            drop_pending_updates=False)
        bot_manager._loading = True
        async def load():
            try:
                await application.post_init(application)
                await bot_manager.start_webhook_monitor()
            except Exception:
                logger.exception("Master initialization failed; readiness remains unavailable")
            finally:
                bot_manager._loading = False
        app['startup_task'] = asyncio.create_task(load())
    return on_startup


def _build_on_shutdown(application: Application, bot_manager: BotManager):
    async def on_shutdown(app):
        startup = app.get('startup_task')
        if startup:
            startup.cancel()
            await asyncio.gather(startup, return_exceptions=True)
        await app['inbox'].stop()
        await application.stop()
        if application.post_stop:
            await application.post_stop(application)
        await application.shutdown()
        if application.post_shutdown:
            await application.post_shutdown(application)
    return on_shutdown


def _web_response(status: int):
    """快速创建 aiohttp Response（延迟导入）"""
    from aiohttp import web
    return web.Response(status=status)


def _register_master_routes(web_app, scheduler):
    """注册 Master 模式的内部管理 API 路由"""
    from aiohttp import web

    def _verify_internal_secret(request: web.Request) -> bool:
        from services.security import internal_authorized
        return internal_authorized(request)

    async def handle_worker_register(request: web.Request):
        if not _verify_internal_secret(request):
            return web.json_response({'error': 'unauthorized'}, status=403)
        try:
            data = await request.json()
            await scheduler.handle_worker_register(
                node_id=data['node_id'],
                node_url=data['node_url'],
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
            found = await scheduler.handle_worker_heartbeat(
                node_id=data['node_id'],
                active_bots=data.get('active_bots', 0),
            )
            return web.json_response({'status': 'ok' if found else 'unknown'}, status=200 if found else 404)
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
        workers = await scheduler.get_worker_status()
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

    web_app = web.Application()
    # Webhook 路由
    web_app.router.add_post(f"{WEBHOOK_PATH}/master", webhook_handler)
    web_app.router.add_post(f"{WEBHOOK_PATH}/{{bot_id:\\d+}}", webhook_handler)
    # Master 模式的内部管理 API
    if scheduler:
        _register_master_routes(web_app, scheduler)
    # 健康检查
    from services.health import add_health_routes
    add_health_routes(web_app, bot_manager)

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