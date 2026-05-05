"""
FileID Bot 托管平台 - 主入口
支持多Bot架构：一个主Bot管理 + 多个用户子Bot
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
    BOT_MODE, WEBHOOK_HOST, WEBHOOK_PATH, WEBHOOK_PORT, WEBHOOK_SECRET
)
from database import init_db
from bot_manager import BotManager

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
        ("platform", "平台统计（管理员）"),
        ("blacklist", "黑名单管理（管理员）"),
        ("export", "导出数据（管理员）"),
    ]
    try:
        await application.bot.set_my_commands(commands)
    except Exception as e:
        logger.warning("主Bot注册命令失败: %s", e)

    # 设置主Bot用户名，供用户Bot引用
    application.bot_data['bot_manager'].master_bot_username = application.bot.username

    # 加载所有用户Bot
    loaded = await application.bot_data['bot_manager'].load_all()
    logger.info("✅ 主Bot启动完成，共加载 %d 个用户Bot", loaded)


def main():
    """启动主Bot和所有用户Bot"""
    if not BOT_TOKEN:
        logger.error("❌ 未设置 BOT_TOKEN 环境变量")
        sys.exit(1)

    # 初始化数据库
    init_db()
    logger.info("📊 数据库初始化完成")

    # 创建 BotManager
    bot_manager = BotManager()

    # 构建主Bot Application（带超时配置）
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

    # 将 BotManager 存入 bot_data 供 handler 使用
    application.bot_data['bot_manager'] = bot_manager

    # 注册主Bot管理命令
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

    # /newbot 交互式对话（3步：用户名 → 名称 → Token）
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

    # 黑名单检查中间件（group=-2，最先执行）
    application.add_handler(
        TypeHandler(Update, blacklist_check_handler), group=-2
    )

    # Managed Bot 自动处理（独立组，不影响其他处理器）
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
    # 重启Bot回调按钮
    application.add_handler(CallbackQueryHandler(restart_bot_callback, pattern=r'^restart_bot\|'))
    # 更新Token回调和命令
    application.add_handler(CallbackQueryHandler(update_token_callback, pattern=r'^update_token\|'))
    application.add_handler(CommandHandler("updatetoken", update_token_cmd))
    application.add_error_handler(error_handler)

    # 全局引用（供 handlers_master 获取 bot_manager）
    sys.modules['__main__'].bot_manager = bot_manager
    sys.modules['__main__'].master_app = application

    logger.info("🚀 主Bot启动中... (模式: %s)", BOT_MODE)
    logger.info("📋 每用户最大Bot数: %d", MAX_BOTS_PER_USER)

    if BOT_MODE == 'webhook':
        _run_webhook(application, bot_manager)
    else:
        _run_polling(application)


def _run_polling(application: Application):
    """Polling 模式启动"""
    all_updates = list(Update.ALL_TYPES) + ['managed_bot']
    application.run_polling(allowed_updates=all_updates)


def _run_webhook(application: Application, bot_manager: BotManager):
    """Webhook 模式启动：aiohttp 服务器 + 主Bot webhook"""
    from aiohttp import web

    async def webhook_handler(request: web.Request):
        """处理所有 webhook 请求，根据路径分发给对应 Bot"""
        path = request.path
        body = await request.json()

        # 可选：验证 secret token
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

        # 分发到用户Bot: /webhook/{bot_db_id}
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
        """健康检查端点"""
        return web.json_response({
            "status": "ok",
            "mode": "webhook",
            "bots": bot_manager.active_count,
        })

    async def on_startup(app: web.Application):
        """aiohttp 启动时：初始化主Bot和所有用户Bot"""
        # 初始化主Bot
        await application.initialize()
        await application.start()

        # 设置主Bot webhook
        master_webhook_url = bot_manager._get_webhook_url_for_master()
        all_updates = list(Update.ALL_TYPES) + ['managed_bot']
        await application.bot.set_webhook(
            url=master_webhook_url,
            secret_token=WEBHOOK_SECRET or None,
            allowed_updates=all_updates,
            drop_pending_updates=True,
        )
        logger.info("主Bot webhook 已设置: %s", master_webhook_url)

        # 设置主Bot用户名
        bot_manager.master_bot_username = application.bot.username

        # 注册主Bot命令
        commands = [
            ("start", "开始使用 / 查看帮助"),
            ("newbot", "一键创建你的 Bot"),
            ("addbot", "添加你的 Bot"),
            ("mybots", "查看我的 Bot 列表"),
            ("delbot", "删除 Bot"),
            ("botstatus", "查看 Bot 运行状态"),
            ("platform", "平台统计（管理员）"),
            ("blacklist", "黑名单管理（管理员）"),
            ("export", "导出数据（管理员）"),
        ]
        try:
            await application.bot.set_my_commands(commands)
        except Exception as e:
            logger.warning("主Bot注册命令失败: %s", e)

        # 加载所有用户Bot
        loaded = await bot_manager.load_all()
        logger.info("✅ webhook 服务器启动完成，共加载 %d 个用户Bot", loaded)

    async def on_shutdown(app: web.Application):
        """aiohttp 关闭时：停止所有Bot"""
        logger.info("正在停止所有Bot...")
        await bot_manager.stop_all()
        await application.stop()
        await application.shutdown()
        logger.info("所有Bot已停止")

    # 创建 aiohttp 应用
    web_app = web.Application()
    web_app.router.add_post(f"{WEBHOOK_PATH}/master", webhook_handler)
    web_app.router.add_post(f"{WEBHOOK_PATH}/{{bot_id:int}}", webhook_handler)
    web_app.router.add_get("/health", health_handler)
    web_app.on_startup.append(on_startup)
    web_app.on_shutdown.append(on_shutdown)

    logger.info("🌐 webhook 服务器监听 0.0.0.0:%d", WEBHOOK_PORT)
    web.run_app(web_app, host="0.0.0.0", port=WEBHOOK_PORT, print=None)


if __name__ == '__main__':
    main()
