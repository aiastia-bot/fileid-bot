"""
FileID Bot 托管平台 - 主入口
支持多Bot架构：一个主Bot管理 + 多个用户子Bot
支持分布式部署：standalone / master / worker 三种模式
"""
import asyncio
import logging
import os
import signal
import sys

from services.rate_limit import get_limiter
from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, CallbackQueryHandler,
    PreCheckoutQueryHandler, TypeHandler, filters
)

from config import (
    BOT_TOKEN, ADMIN_IDS, MAX_BOTS_PER_USER,
    API_READ_TIMEOUT, API_WRITE_TIMEOUT, API_CONNECT_TIMEOUT,
    BOT_MODE, WEBHOOK_HOST, WEBHOOK_PATH, WEBHOOK_PORT, WEBHOOK_SECRET,
    ROLE, WORKER_SECRET, REDIS_URL, LOG_LEVEL,
)
from db import init_db
from bot_manager import BotManager
from scheduler import MasterScheduler
from webhook_server import run_webhook_master, run_webhook

# ==================== 日志配置 ====================
_log_level = getattr(logging, LOG_LEVEL.upper(), logging.WARNING)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=_log_level
)
from services.logging import RedactingFormatter
for _handler in logging.getLogger().handlers:
    _handler.setFormatter(RedactingFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
# 第三方库始终只显示 WARNING 以上
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
# 始终打印日志级别（不受级别过滤），方便确认配置生效
print(f"[启动] 日志级别: {LOG_LEVEL.upper()} (来自 LOG_LEVEL 环境变量)")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """主Bot错误处理"""
    from services.inbox import note_error
    note_error(context.error)
    logger.error("主Bot异常: %s", context.error, exc_info=context.error)


async def post_init(application: Application) -> None:
    """主Bot初始化完成后：设置命令、加载所有用户Bot"""
    from services import runtime
    from services.leases import acquire
    await init_db()
    if not await acquire('master'):
        raise RuntimeError('Another master process holds the runtime lease')
    await _init_redis()
    runtime.configure(bot_manager=application.bot_data.get('bot_manager'),
                      bot_scheduler=application.bot_data.get('scheduler'), application=application)
    application.bot_data['_ready'] = True
    # 注册主Bot命令：默认菜单只显示用户命令；
    # 管理员私聊菜单额外显示管理命令（BotCommandScopeChat 需管理员与 Bot 有过会话，失败忽略）
    try:
        from telegram import BotCommandScopeChat
        from config import MASTER_USER_COMMANDS, MASTER_ADMIN_COMMANDS
        await application.bot.set_my_commands(MASTER_USER_COMMANDS)
        for admin_id in ADMIN_IDS:
            try:
                await application.bot.set_my_commands(
                    MASTER_USER_COMMANDS + MASTER_ADMIN_COMMANDS,
                    scope=BotCommandScopeChat(admin_id),
                )
            except Exception:
                pass  # 管理员尚未与 Bot 交互过时设置 chat scope 会失败
    except Exception as e:
        logger.warning("主Bot注册命令失败: %s", e)

    # 设置主Bot用户名，供用户Bot引用
    bot_manager = application.bot_data.get('bot_manager')
    scheduler = application.bot_data.get('scheduler')

    if bot_manager:
        bot_manager.master_bot_username = application.bot.username
    if scheduler:
        scheduler.master_bot_username = application.bot.username

    # 启动 VIP 过期检查定时任务
    _start_vip_expire_job(application)

    from services.entitlements import reconcile_all
    await reconcile_all()
    if scheduler:
        scheduler.start()

    # 加载用户 Bot
    if ROLE == 'master' and scheduler:
        # Master 模式：分配 Bot 到 Worker 节点
        loaded = await scheduler.load_all_to_workers()
        logger.info("✅ 主Bot(Master)启动完成，已分配 %d 个用户Bot到Worker节点", loaded)
    elif bot_manager:
        # Standalone 模式：本地加载所有 Bot
        loaded = await bot_manager.load_all()
        logger.info("✅ 主Bot启动完成，共加载 %d 个用户Bot", loaded)

    if bot_manager:
        bot_manager._ready = True


# ==================== Standalone 模式 ====================

def run_standalone():
    """单机模式：主Bot + 所有用户Bot 在同一进程"""
    if not BOT_TOKEN:
        logger.error("❌ 未设置 BOT_TOKEN 环境变量")
        sys.exit(1)


    bot_manager = BotManager()


    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
            .rate_limiter(get_limiter(BOT_TOKEN))
            .concurrent_updates(False)
        .post_init(post_init)
        .post_stop(post_stop)
        .post_shutdown(post_shutdown)
        .read_timeout(API_READ_TIMEOUT)
        .write_timeout(API_WRITE_TIMEOUT)
        .connect_timeout(API_CONNECT_TIMEOUT)
        .pool_timeout(API_CONNECT_TIMEOUT)
        .build()
    )

    application.bot_data['bot_manager'] = bot_manager

    # 注册主Bot管理命令
    _register_master_handlers(application)

    # 优雅关闭

    # 全局引用
    sys.modules['__main__'].bot_manager = bot_manager
    sys.modules['__main__'].master_app = application

    logger.info("🚀 主Bot启动中... (模式: standalone, bot_mode: %s)", BOT_MODE)
    logger.info("📋 每用户最大Bot数: %d", MAX_BOTS_PER_USER)

    if BOT_MODE == 'webhook':
        run_webhook(application, bot_manager)
    else:
        _run_polling(application)


# ==================== Master 模式 ====================

def run_master():
    """Master 模式：主Bot + 调度器，用户Bot运行在Worker节点"""
    if not BOT_TOKEN:
        logger.error("❌ 未设置 BOT_TOKEN 环境变量")
        sys.exit(1)


    bot_manager = BotManager()  # Master 本地也可以运行少量 Bot（fallback）
    scheduler = MasterScheduler()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
            .rate_limiter(get_limiter(BOT_TOKEN))
            .concurrent_updates(False)
        .post_init(post_init)
        .post_stop(post_stop)
        .post_shutdown(post_shutdown)
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

    # 优雅关闭

    # 全局引用
    sys.modules['__main__'].bot_manager = bot_manager
    sys.modules['__main__'].scheduler = scheduler
    sys.modules['__main__'].master_app = application

    logger.info("🚀 主Bot(Master)启动中... (bot_mode: %s)", BOT_MODE)
    logger.info("📋 每用户最大Bot数: %d", MAX_BOTS_PER_USER)

    if BOT_MODE == 'webhook':
        run_webhook_master(application, bot_manager, scheduler)
    else:
        _run_polling(application)


# ==================== Worker 模式 ====================

def run_worker():
    """Worker 模式：只运行用户Bot，接收Master指令"""
    from worker_server import WorkerServer


    bot_manager = BotManager()
    worker_server = WorkerServer()
    worker_server.set_bot_manager(bot_manager)

    from services import runtime
    runtime.configure(bot_manager=bot_manager)

    # 全局引用
    sys.modules['__main__'].bot_manager = bot_manager

    logger.info("🚀 Worker 启动中...")
    worker_server.run()


# ==================== 通用函数 ====================
# 原本用 TypeHandler(Update, _payment_filter_handler) 捕获 successful_payment，
# 但 TypeHandler 匹配所有 Update，按 PTB「同组只跑第一个匹配 handler」的规则，
# 会吞掉同组后续 MessageHandler（如「手动输入用户 ID 发送礼物」），导致输入无响应。
# 改用 MessageHandler(filters.SUCCESSFUL_PAYMENT, ...) 专用 handler，只匹配支付成功消息。


def _start_vip_expire_job(application: Application):
    from handlers.master.stars import handle_expired_vips, send_expire_reminders
    from services.payments import process_pending
    from services.entitlements import reconcile_all
    from services.maintenance import cleanup_records
    from services.leases import acquire
    from services.notices import deliver

    async def renew_master(context):
        if not await acquire('master'):
            application.stop_running()

    async def maintenance(context):
        await process_pending(context.bot)
        await handle_expired_vips()
        await reconcile_all()
        await deliver(context.bot)
        from services.maintenance import auto_purge_dead_bots
        try:
            await auto_purge_dead_bots()
        except Exception:
            logger.exception("死亡 Bot 自动清理异常")

    async def reminders(context):
        await send_expire_reminders()
        await cleanup_records()

    application.job_queue.run_repeating(renew_master, interval=20, first=1, name='master-lease')
    application.job_queue.run_repeating(maintenance, interval=30, first=5, name='membership-orders', job_kwargs={'max_instances': 1})
    application.job_queue.run_repeating(reminders, interval=86400, first=120, name='reminders-maintenance')

    # 级联备份补位检查（Bot 失效后自动重建链路并通知主人）
    from config import BACKUP_ENABLED, BACKUP_RECONCILE_INTERVAL
    if BACKUP_ENABLED:
        from services.backup import reconcile_all

        async def backup_reconcile(context):
            try:
                await reconcile_all()
            except Exception:
                logger.exception("备份补位任务异常")

        application.job_queue.run_repeating(
            backup_reconcile, interval=BACKUP_RECONCILE_INTERVAL,
            first=15, name='backup-reconcile', job_kwargs={'max_instances': 1})


def _register_master_handlers(application: Application):
    """注册主Bot的管理命令处理器"""
    from handlers.master import (
        master_start, handle_managed_bot, add_bot_cmd, new_bot_start,
        new_bot_input_username, new_bot_input_name, new_bot_input_token,
        new_bot_cancel, my_bots_cmd, delete_bot_cmd, bot_status_cmd,
        platform_stats_cmd, blacklist_cmd, export_data_cmd,
        start_bot_admin_cmd, stop_bot_admin_cmd,
        broadcast_cmd, set_group_cmd,
        set_vip_cmd, set_free_cmd, help_admin_cmd,
        restart_bot_callback,
        update_token_callback, update_token_cmd,
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
    application.add_handler(CommandHandler("stopbot", stop_bot_admin_cmd))
    application.add_handler(CallbackQueryHandler(restart_bot_callback, pattern=r'^restart_bot\|'))
    application.add_handler(CallbackQueryHandler(update_token_callback, pattern=r'^update_token\|'))
    application.add_handler(CommandHandler("updatetoken", update_token_cmd))
    application.add_handler(CommandHandler("setgroup", set_group_cmd))
    from handlers.master.membership_admin import setvip_command, setplan_command, vipadmin_command, tasks_command
    application.add_handler(CommandHandler("setvip", setvip_command))
    application.add_handler(CommandHandler("setplan", setplan_command))
    application.add_handler(CommandHandler("vipadmin", vipadmin_command))
    application.add_handler(CommandHandler("tasks", tasks_command))
    application.add_handler(CommandHandler("setfree", set_free_cmd))
    application.add_handler(CommandHandler("helpadmin", help_admin_cmd))

    # 级联备份（面向所有 Bot 主人）
    from handlers.master.backup import backup_command
    application.add_handler(CommandHandler("backup", backup_command))

    # VIP 转发保护设置（Bot 主人设置转发模式）
    from handlers.master.manage import forward_mode_callback, auto_delete_callback
    application.add_handler(CallbackQueryHandler(forward_mode_callback, pattern=r'^fwd_'))
    application.add_handler(CallbackQueryHandler(auto_delete_callback, pattern=r'^adel_'))

    # 死亡 Bot 记录清理（管理员）
    from handlers.master.manage import purge_cmd, purge_bot_callback
    application.add_handler(CommandHandler("purge", purge_cmd))
    application.add_handler(CallbackQueryHandler(purge_bot_callback, pattern=r'^purge_'))

    # VIP / 星星支付
    from handlers.master.stars import (
        vip_command, vip_callback_router, pre_checkout_handler,
        successful_payment_handler,
    )
    application.add_handler(CommandHandler("vip", vip_command))
    application.add_handler(CallbackQueryHandler(vip_callback_router, pattern=r'^(buy_vip|vip_history|vip_migrate)'))
    application.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))
    # 支付成功消息：用专用 filter，避免 TypeHandler(Update) 吞掉普通文本 handler。
    # 内部按 payload 前缀分流 vip_ / premium_。
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler))

    # 管理员星星资产管理 / 礼物发送
    from handlers.master.gifts import (
        mystars_command, stars_callback_router, handle_gift_user_id_input,
    )
    application.add_handler(CommandHandler("mystars", mystars_command))
    application.add_handler(CallbackQueryHandler(stars_callback_router, pattern=r'^stars_'))
    # 处理管理员手动输入用户 ID 发送礼物。
    # 放在 group 10（独立分组）：主 Bot 无其他普通文本 handler，这里也不会干扰 group 0 的命令/回调。
    # 内部有 `if not waiting_gift_user_id: return` 守卫，仅在「手动输入」流程激活时处理。
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_gift_user_id_input), group=10)

    # 用户购买 Telegram Premium 会员（星星支付，Bot 赠送官方 Premium）
    from handlers.master.premium import premium_command, buy_premium_callback
    application.add_handler(CommandHandler("premium", premium_command))
    application.add_handler(CallbackQueryHandler(buy_premium_callback, pattern=r'^buy_premium\|'))

    application.add_error_handler(error_handler)


async def post_stop(application):
    from send_queue import stop_all_queues
    from services import runtime
    # 补发未完成的附件汇总（文件已入库只差通知；bot 仍在 shutdown 前，可发送）
    from handlers.messages import flush_all_attachment_reply_buffers
    apps = [application]
    if runtime.manager:
        apps += list(runtime.manager.get_all_apps().values())
    for app in apps:
        await flush_all_attachment_reply_buffers(app)
    if runtime.scheduler:
        await runtime.scheduler.stop()
    await stop_all_queues()
    if runtime.manager:
        await runtime.manager.stop_all()
    runner = application.bot_data.pop('_health_runner', None)
    if runner:
        await runner.cleanup()


async def post_shutdown(application):
    from db.core import close_db
    from redis_manager import get_redis
    from services.leases import release
    await release('master')
    await (await get_redis()).close()
    await close_db()


def _run_polling(application: Application):
    """Polling 模式启动"""
    # Polling also exposes /health and /ready on the configured HTTP port.
    original = application.post_init
    async def polling_init(app):
        await original(app)
        from aiohttp import web
        from services.health import add_health_routes
        health_app = web.Application()
        add_health_routes(health_app, app.bot_data['bot_manager'])
        runner = web.AppRunner(health_app)
        await runner.setup()
        await web.TCPSite(runner, '0.0.0.0', WEBHOOK_PORT).start()
        app.bot_data['_health_runner'] = runner
    application.post_init = polling_init
    all_updates = list(Update.ALL_TYPES) + ['managed_bot']
    application.run_polling(allowed_updates=all_updates)


# ==================== 入口 ====================

async def _init_redis():
    """初始化 Redis（可选）"""
    try:
        from redis_manager import get_redis
        r = await get_redis()
        if r.available:
            logger.info("📦 Redis 已连接")
        else:
            logger.info("📦 使用内存降级方案（未配置 REDIS_URL）")
    except Exception as e:
        logger.warning("📦 Redis 初始化失败: %s，使用内存降级方案", e)


def main():
    """根据 ROLE 选择启动模式"""
    from config import validate_config
    validate_config()
    logger.info("🔧 启动模式: %s", ROLE)

    # 预初始化 Redis（在 asyncio.run 之外无法 await，由首次调用自动初始化）
    if REDIS_URL:
        logger.info("📦 已配置 Redis")

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
