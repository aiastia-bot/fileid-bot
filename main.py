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
    ConversationHandler, MessageHandler, CallbackQueryHandler,
    PreCheckoutQueryHandler, TypeHandler, filters
)

from config import (
    BOT_TOKEN, ADMIN_IDS, MAX_BOTS_PER_USER,
    API_READ_TIMEOUT, API_WRITE_TIMEOUT, API_CONNECT_TIMEOUT,
    BOT_MODE, WEBHOOK_HOST, WEBHOOK_PATH, WEBHOOK_PORT, WEBHOOK_SECRET,
    ROLE, WORKER_SECRET, REDIS_URL, LOG_LEVEL
)
from database import init_db
from bot_manager import BotManager
from scheduler import MasterScheduler
from webhook_server import run_webhook_master, run_webhook

# ==================== 日志配置 ====================
_log_level = getattr(logging, LOG_LEVEL.upper(), logging.WARNING)
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=_log_level
)
# 第三方库始终只显示 WARNING 以上
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
# 始终打印日志级别（不受级别过滤），方便确认配置生效
print(f"[启动] 日志级别: {LOG_LEVEL.upper()} (来自 LOG_LEVEL 环境变量)")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """主Bot错误处理"""
    logger.error("主Bot异常: %s", context.error, exc_info=context.error)


async def post_init(application: Application) -> None:
    """主Bot初始化完成后：设置命令、加载所有用户Bot"""
    # 注册主Bot命令
    commands = [
        ("start", "开始使用 / 查看帮助"),
        ("vip", "VIP 会员 / 购买星星"),
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
        ("mystars", "星星资产 / 发送礼物（管理员）"),
        ("startbot", "重启/启动Bot（管理员）"),
        ("stopbot", "停止指定Bot（管理员）"),
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

    # 启动 VIP 过期检查定时任务
    _start_vip_expire_job(application)

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

    asyncio.run(init_db())
    logger.info("📊 数据库初始化完成")

    bot_manager = BotManager()


    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
            .concurrent_updates(True)
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
        run_webhook(application, bot_manager)
    else:
        _run_polling(application)


# ==================== Master 模式 ====================

def run_master():
    """Master 模式：主Bot + 调度器，用户Bot运行在Worker节点"""
    if not BOT_TOKEN:
        logger.error("❌ 未设置 BOT_TOKEN 环境变量")
        sys.exit(1)

    asyncio.run(init_db())
    logger.info("📊 数据库初始化完成")

    bot_manager = BotManager()  # Master 本地也可以运行少量 Bot（fallback）
    scheduler = MasterScheduler()

    application = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
            .concurrent_updates(True)
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
        run_webhook_master(application, bot_manager, scheduler)
    else:
        _run_polling(application)


# ==================== Worker 模式 ====================

def run_worker():
    """Worker 模式：只运行用户Bot，接收Master指令"""
    from worker_server import WorkerServer

    asyncio.run(init_db())
    logger.info("📊 数据库初始化完成")

    bot_manager = BotManager()
    worker_server = WorkerServer()
    worker_server.set_bot_manager(bot_manager)

    # 全局引用
    sys.modules['__main__'].bot_manager = bot_manager

    logger.info("🚀 Worker 启动中...")
    worker_server.run()


# ==================== 通用函数 ====================

async def _payment_filter_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """过滤并处理 successful_payment 类型的 Update"""
    if update.message and update.message.successful_payment:
        from handlers_master_stars import successful_payment_handler
        await successful_payment_handler(update, context)


def _start_vip_expire_job(application: Application):
    """启动 VIP 过期检查定时任务"""
    from handlers_master_stars import handle_expired_vips, send_expire_reminders

    async def _vip_expire_check(context: ContextTypes.DEFAULT_TYPE):
        """每小时检查过期VIP"""
        await handle_expired_vips()

    async def _vip_expire_reminder(context: ContextTypes.DEFAULT_TYPE):
        """每天发送过期提醒"""
        await send_expire_reminders()

    try:
        # 每 1 小时检查过期
        application.job_queue.run_repeating(_vip_expire_check, interval=3600, first=60)
        # 每天发送到期提醒
        application.job_queue.run_repeating(_vip_expire_reminder, interval=86400, first=120)
        logger.info("✅ VIP 过期检查定时任务已启动")
    except Exception as e:
        logger.warning("VIP 定时任务启动失败（可能未安装 job_queue 依赖）: %s", e)


def _register_master_handlers(application: Application):
    """注册主Bot的管理命令处理器"""
    from handlers_master import (
        master_start, handle_managed_bot, add_bot_cmd, new_bot_start,
        new_bot_input_username, new_bot_input_name, new_bot_input_token,
        new_bot_cancel, my_bots_cmd, delete_bot_cmd, bot_status_cmd,
        platform_stats_cmd, blacklist_cmd, export_data_cmd,
        start_bot_admin_cmd, stop_bot_admin_cmd,
        broadcast_cmd, set_group_cmd,
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

    # VIP / 星星支付
    from handlers_master_stars import (
        vip_command, vip_callback_router, pre_checkout_handler,
        successful_payment_handler,
    )
    application.add_handler(CommandHandler("vip", vip_command))
    application.add_handler(CallbackQueryHandler(vip_callback_router, pattern=r'^(buy_vip|vip_history)'))
    application.add_handler(PreCheckoutQueryHandler(pre_checkout_handler))
    application.add_handler(TypeHandler(Update, _payment_filter_handler), group=10)

    # 管理员星星资产管理 / 礼物发送
    from handlers_master_gifts import (
        mystars_command, stars_callback_router, handle_gift_user_id_input,
    )
    application.add_handler(CommandHandler("mystars", mystars_command))
    application.add_handler(CallbackQueryHandler(stars_callback_router, pattern=r'^stars_'))
    # 处理管理员手动输入用户 ID 发送礼物（放在较低优先级组）
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_gift_user_id_input), group=10)

    application.add_error_handler(error_handler)


def _run_polling(application: Application):
    """Polling 模式启动"""
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
    logger.info("🔧 启动模式: %s", ROLE)

    # 预初始化 Redis（在 asyncio.run 之外无法 await，由首次调用自动初始化）
    if REDIS_URL:
        logger.info("📦 检测到 REDIS_URL 配置: %s", REDIS_URL[:30] + '...')

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
