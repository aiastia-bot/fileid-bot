"""
FileID Bot 托管平台 - 主入口
支持多Bot架构：一个主Bot管理 + 多个用户子Bot
支持分布式部署：standalone / master / worker 三种模式
"""
import asyncio
import logging
import signal
import sys

from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, CallbackQueryHandler, TypeHandler, filters
)

from config import (
    BOT_TOKEN, ADMIN_IDS, MAX_BOTS_PER_USER,
    API_READ_TIMEOUT, API_WRITE_TIMEOUT, API_CONNECT_TIMEOUT,
    BOT_MODE, WEBHOOK_HOST, WEBHOOK_PATH, WEBHOOK_PORT, WEBHOOK_SECRET,
    ROLE, WORKER_SECRET
)
from database import init_db
from bot_manager import BotManager, MasterScheduler

# ==================== 日志配置 ====================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """主Bot错误处理"""
    logger.error("主Bot异常: %s", context.error, exc_info=context.error)


async def post_init(application: Application) -> None:
    """主Bot初始化完成后：设置命令、加载所有用户Bot"""
    # 注册主Bot命令
    commands = [
        ("start", "开始使用 / 查看帮助"),
        ("newbot", "一键创建你的 Bot"),
        ("addbot", "添加你的 Bot"),
        ("mybots", "查看我的 Bot 列表"),
        ("delbot", "删除 Bot"),
        ("botstatus", "查看 Bot 运行状态"),
        ("updatetoken", "更新失效的 Token"),
        ("platform", "平台统计（管理员）"),
        ("blacklist", "黑名单管理（管理员）"),
        ("export", "导出数据（管理员）"),
        ("broadcast", "广播消息（管理员）"),
        ("startbot", "启动指定Bot（管理员）"),
    ]
    try:
        await application.bot.set_my_commands(commands)
    except Exception as e:
        logger.warning("主Bot注册命令失败: %s", e)

    # 设置主Bot用户名，供用户Bot引用
    bot_manager = application.bot_data.get('bot_manager')
    scheduler = application.bot_data.get('scheduler')

    if bot_manager:
        bot_manager.master_bot_username = application.bot.username
    if scheduler:
        scheduler.master_bot_username = application.bot.username

    # 加载用户 Bot
    if ROLE == 'master' and scheduler:
        # Master 模式：分配 Bot 到 Worker 节点
        loaded = await scheduler.load_all_to_workers()
        logger.info("✅ 主Bot(Master)启动完成，已分配 %d 个用户Bot到Worker节点", loaded)
    elif bot_manager:
        # Standalone 模式：本地加载所有 Bot
        loaded = await bot_manager.load_all()
        logger.info("✅ 主Bot启动完成，共加载 %d 个用户Bot", loaded)


# ==================== Standalone 模式 ====================

def run_standalone():
    """单机模式：主Bot + 所有用户Bot 在同一进程"""
    if not BOT_TOKEN:
        logger.error("❌ 未设置 BOT_TOKEN 环境变量")
        sys.exit(1)

    init_db()
    logger.info("📊 数据库初始化完成")

    bot_manager = BotManager()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .read_timeout(API_READ_TIMEOUT)
        .write_timeout(API_WRITE_TIMEOUT)
        .connect_timeout(API_CONNECT_TIMEOUT)
        .pool_timeout(API_CONNECT_TIMEOUT)
        .build()
    )

    application.bot_data['bot_manager'] = bot_manager

    # 注册主Bot管理命令
    _register_master_handlers(application)

    # 全局引用
    sys.modules['__main__'].bot_manager = bot_manager
    sys.modules['__main__'].master_app = application

    logger.info("🚀 主Bot启动中... (模式: standalone, bot_mode: %s)", BOT_MODE)
    logger.info("📋 每用户最大Bot数: %d", MAX_BOTS_PER_USER)

    if BOT_MODE == 'webhook':
        _run_webhook(application, bot_manager)
    else:
        _run_polling(application)


# ==================== Master 模式 ====================

def run_master():
    """Master 模式：主Bot + 调度器，用户Bot运行在Worker节点"""
    if not BOT_TOKEN:
        logger.error("❌ 未设置 BOT_TOKEN 环境变量")
        sys.exit(1)

    init_db()
    logger.info("📊 数据库初始化完成")

    bot_manager = BotManager()  # Master 本地也可以运行少量 Bot（fallback）
    scheduler = MasterScheduler()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .read_timeout(API_READ_TIMEOUT)
        .write_timeout(API_WRITE_TIMEOUT)
        .connect_timeout(API_CONNECT_TIMEOUT)
        .pool_timeout(API_CONNECT_TIMEOUT)
        .build()
    )

    application.bot_data['bot_manager'] = bot_manager
    application.bot_data['scheduler'] = scheduler

    # 注册主Bot管理命令
    _register_master_handlers(application)

    # 全局引用
    sys.modules['__main__'].bot_manager = bot_manager
    sys.modules['__main__'].scheduler = scheduler
    sys.modules['__main__'].master_app = application

    logger.info("🚀 主Bot(Master)启动中... (bot_mode: %s)", BOT_MODE)
    logger.info("📋 每用户最大Bot数: %d", MAX_BOTS_PER_USER)

    if BOT_MODE == 'webhook':
        _run_webhook_master(application, bot_manager, scheduler)
    else:
        _run_polling(application)


def _run_webhook_master(application: Application, bot_manager: BotManager, scheduler: MasterScheduler):
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
                await bot_manager.handle_webhook_update(bot_db_id, body)
            except (ValueError, Exception) as e:
                logger.error("用户Bot webhook 处理失败: %s", e)
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
        commands = [
            ("start", "开始使用 / 查看帮助"),
            ("newbot", "一键创建你的 Bot"),
            ("addbot", "添加你的 Bot"),
            ("mybots", "查看我的 Bot 列表"),
            ("delbot", "删除 Bot"),
            ("botstatus", "查看 Bot 运行状态"),
            ("updatetoken", "更新失效的 Token"),
            ("platform", "平台统计（管理员）"),
            ("blacklist", "黑名单管理（管理员）"),
            ("export", "导出数据（管理员）"),
            ("broadcast", "广播消息（管理员）"),
            ("startbot", "启动指定Bot（管理员）"),
        ]
        try:
            await application.bot.set_my_commands(commands)
        except Exception as e:
            logger.warning("主Bot注册命令失败: %s", e)

        # 分配 Bot 到 Worker
        loaded = await scheduler.load_all_to_workers()
        logger.info("✅ Master webhook 启动完成，已分配 %d 个用户Bot", loaded)

    async def on_shutdown(app: web.Application):
        logger.info("正在停止所有Bot...")
        await bot_manager.stop_all()
        await application.stop()
        await application.shutdown()
        logger.info("所有Bot已停止")

    web_app = web.Application()
    # Webhook 路由
    web_app.router.add_post(f"{WEBHOOK_PATH}/master", webhook_handler)
    web_app.router.add_post(f"{WEBHOOK_PATH}/{{bot_id:int}}", webhook_handler)
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


# ==================== Worker 模式 ====================

def run_worker():
    """Worker 模式：只运行用户Bot，接收Master指令"""
    from worker_server import WorkerServer

    init_db()
    logger.info("📊 数据库初始化完成")

    bot_manager = BotManager()
    worker_server = WorkerServer()
    worker_server.set_bot_manager(bot_manager)

    # 全局引用
    sys.modules['__main__'].bot_manager = bot_manager

    logger.info("🚀 Worker 启动中...")
    worker_server.run()


# ==================== 通用函数 ====================

def _register_master_handlers(application: Application):
    """注册主Bot的管理命令处理器"""
    from handlers_master import (
        master_start, handle_managed_bot, add_bot_cmd, new_bot_start,
        new_bot_input_username, new_bot_input_name, new_bot_input_token,
        new_bot_cancel, my_bots_cmd, delete_bot_cmd, bot_status_cmd,
        platform_stats_cmd, blacklist_cmd, export_data_cmd,
        broadcast_cmd,
        restart_bot_callback,
        update_token_callback, update_token_cmd,
        start_bot_admin_cmd,
        blacklist_check_handler,
        INPUT_BOT_USERNAME, INPUT_BOT_NAME, INPUT_BOT_TOKEN
    )

    # /newbot 交互式对话
    newbot_conv = ConversationHandler(
        entry_points=[CommandHandler("newbot", new_bot_start)],
        states={
            INPUT_BOT_USERNAME: [
                CommandHandler("newbot", new_bot_start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, new_bot_input_username),
            ],
            INPUT_BOT_NAME: [
                CommandHandler("newbot", new_bot_start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, new_bot_input_name),
            ],
            INPUT_BOT_TOKEN: [
                CommandHandler("newbot", new_bot_start),
                MessageHandler(filters.TEXT & ~filters.COMMAND, new_bot_input_token),
            ],
        },
        fallbacks=[CommandHandler("cancel", new_bot_cancel)],
    )

    # 黑名单检查中间件
    application.add_handler(TypeHandler(Update, blacklist_check_handler), group=-2)
    # Managed Bot 自动处理
    application.add_handler(TypeHandler(Update, handle_managed_bot), group=-1)

    application.add_handler(CommandHandler("start", master_start))
    application.add_handler(CommandHandler("help", master_start))
    application.add_handler(newbot_conv)
    application.add_handler(CommandHandler("addbot", add_bot_cmd))
    application.add_handler(CommandHandler("mybots", my_bots_cmd))
    application.add_handler(CommandHandler("delbot", delete_bot_cmd))
    application.add_handler(CommandHandler("botstatus", bot_status_cmd))
    application.add_handler(CommandHandler("platform", platform_stats_cmd))
    application.add_handler(CommandHandler("blacklist", blacklist_cmd))
    application.add_handler(CommandHandler("export", export_data_cmd))
    application.add_handler(CommandHandler("broadcast", broadcast_cmd))
    application.add_handler(CommandHandler("startbot", start_bot_admin_cmd))
    application.add_handler(CallbackQueryHandler(restart_bot_callback, pattern=r'^restart_bot\|'))
    application.add_handler(CallbackQueryHandler(update_token_callback, pattern=r'^update_token\|'))
    application.add_handler(CommandHandler("updatetoken", update_token_cmd))
    application.add_error_handler(error_handler)


def _run_polling(application: Application):
    """Polling 模式启动"""
    all_updates = list(Update.ALL_TYPES) + ['managed_bot']
    application.run_polling(allowed_updates=all_updates)


def _run_webhook(application: Application, bot_manager: BotManager):
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
                await bot_manager.handle_webhook_update(bot_db_id, body)
            except (ValueError, Exception) as e:
                logger.error("用户Bot webhook 处理失败: %s", e)
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

        commands = [
            ("start", "开始使用 / 查看帮助"),
            ("newbot", "一键创建你的 Bot"),
            ("addbot", "添加你的 Bot"),
            ("mybots", "查看我的 Bot 列表"),
            ("delbot", "删除 Bot"),
            ("botstatus", "查看 Bot 运行状态"),
            ("updatetoken", "更新失效的 Token"),
            ("platform", "平台统计（管理员）"),
            ("blacklist", "黑名单管理（管理员）"),
            ("export", "导出数据（管理员）"),
            ("broadcast", "广播消息（管理员）"),
            ("startbot", "启动指定Bot（管理员）"),
        ]
        try:
            await application.bot.set_my_commands(commands)
        except Exception as e:
            logger.warning("主Bot注册命令失败: %s", e)

        loaded = await bot_manager.load_all()
        logger.info("✅ webhook 服务器启动完成，共加载 %d 个用户Bot", loaded)

    async def on_shutdown(app: web.Application):
        logger.info("正在停止所有Bot...")
        await bot_manager.stop_all()
        await application.stop()
        await application.shutdown()
        logger.info("所有Bot已停止")

    web_app = web.Application()
    web_app.router.add_post(f"{WEBHOOK_PATH}/master", webhook_handler)
    web_app.router.add_post(f"{WEBHOOK_PATH}/{{bot_id:int}}", webhook_handler)
    web_app.router.add_get("/health", health_handler)
    web_app.on_startup.append(on_startup)
    web_app.on_shutdown.append(on_shutdown)

    logger.info("🌐 webhook 服务器监听 0.0.0.0:%d", WEBHOOK_PORT)
    web.run_app(web_app, host="0.0.0.0", port=WEBHOOK_PORT, print=None)


# ==================== 入口 ====================

def main():
    """根据 ROLE 选择启动模式"""
    logger.info("🔧 启动模式: %s", ROLE)

    if ROLE == 'standalone':
        run_standalone()
    elif ROLE == 'master':
        run_master()
    elif ROLE == 'worker':
        run_worker()
    else:
        logger.error("❌ 未知 ROLE: %s（可选: standalone / master / worker）", ROLE)
        sys.exit(1)


if __name__ == '__main__':
    main()