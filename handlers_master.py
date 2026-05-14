"""Master Bot handlers - 入口文件，注册所有 handler，re-export 保持向后兼容"""

from telegram.ext import (
    CommandHandler, CallbackQueryHandler, ConversationHandler, MessageHandler, filters
)

# Re-export all functions from sub-modules
from handlers_master_start import (  # noqa: F401
    master_start, handle_managed_bot, blacklist_check_handler,
)
from handlers_master_newbot import (  # noqa: F401
    new_bot_start, new_bot_input_username, new_bot_input_name,
    new_bot_input_token, new_bot_cancel, add_bot_cmd,
    INPUT_BOT_USERNAME, INPUT_BOT_NAME, INPUT_BOT_TOKEN,
)
from handlers_master_manage import (  # noqa: F401
    my_bots_cmd, delete_bot_cmd, bot_status_cmd,
    restart_bot_callback, update_token_callback, update_token_cmd,
)
from handlers_master_admin import (  # noqa: F401
    platform_stats_cmd, export_data_cmd, start_bot_admin_cmd, stop_bot_admin_cmd,
)
from handlers_master_blacklist import (  # noqa: F401
    blacklist_cmd,
)


def register_master_handlers(application):
    """向 Application 注册所有主 Bot 的 handler"""

    # /newbot 交互式创建 (ConversationHandler)
    newbot_conv = ConversationHandler(
        entry_points=[CommandHandler("newbot", new_bot_start)],
        states={
            INPUT_BOT_USERNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_bot_input_username)],
            INPUT_BOT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, new_bot_input_name)],
            INPUT_BOT_TOKEN: [
                CommandHandler("cancel", new_bot_cancel),
                MessageHandler(filters.TEXT & ~filters.COMMAND, new_bot_input_token),
            ],
        },
        fallbacks=[CommandHandler("cancel", new_bot_cancel)],
        per_user=True,
        per_chat=True,
    )
    application.add_handler(newbot_conv)

    # 用户命令
    application.add_handler(CommandHandler("start", master_start))
    application.add_handler(CommandHandler("addbot", add_bot_cmd))
    application.add_handler(CommandHandler("mybots", my_bots_cmd))
    application.add_handler(CommandHandler("delbot", delete_bot_cmd))
    application.add_handler(CommandHandler("botstatus", bot_status_cmd))
    application.add_handler(CommandHandler("updatetoken", update_token_cmd))

    # 用户回调按钮
    application.add_handler(CallbackQueryHandler(restart_bot_callback, pattern=r"^restart_bot\|"))
    application.add_handler(CallbackQueryHandler(update_token_callback, pattern=r"^update_token\|"))

    # 管理员命令
    application.add_handler(CommandHandler("platform", platform_stats_cmd))
    application.add_handler(CommandHandler("export", export_data_cmd))
    application.add_handler(CommandHandler("startbot", start_bot_admin_cmd))
    application.add_handler(CommandHandler("broadcast", broadcast_cmd))
    application.add_handler(CommandHandler("blacklist", blacklist_cmd))

    # 黑名单检查中间件（在所有 handler 之前）
    # 注意：这个中间件需要通过 application.handlers 在最前面添加
    # 因为 add_handler 默认追加到末尾，我们需要特殊处理
    # 实际上 telegram-bot 库的 group 机制可以控制顺序

    # managed_bot 更新处理
    # 通过 api_kwargs 中的 managed_bot 字段识别
    application.add_handler(MessageHandler(
        filters.ALL,
        handle_managed_bot
    ))