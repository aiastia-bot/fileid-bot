"""
Bot 管理器
管理所有用户子Bot的生命周期：创建、启动、停止、删除
每个用户Bot拥有独立的 Application 实例和完整的 FileID 功能
"""
import asyncio
import logging
from typing import Dict, Optional, List

from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters
)

from database import get_all_active_user_bots
from config import (
    API_READ_TIMEOUT, API_WRITE_TIMEOUT, API_CONNECT_TIMEOUT,
    BOT_MODE, WEBHOOK_HOST, WEBHOOK_PATH, WEBHOOK_PORT, WEBHOOK_SECRET,
    ALLOW_GROUP
)

# 启动并发数限制，避免同时发起过多 Telegram API 请求
MAX_CONCURRENT_STARTS = 5

logger = logging.getLogger(__name__)


async def _auto_stop_revoked_bot(bot_username: str, bot_data: dict):
    """自动停止 Token 被撤销的 Bot，并通知主 Bot 通知用户"""
    await asyncio.sleep(3)

    bot_record = bot_data.get('bot_record')
    if not bot_record:
        return

    owner_id = bot_record.get('owner_id')
    bot_db_id = bot_record.get('id')

    # 更新数据库状态
    from database import update_user_bot_status
    update_user_bot_status(bot_db_id, 'revoked')

    # 通过主 Bot 通知用户
    try:
        import __main__
        bot_manager = getattr(__main__, 'bot_manager', None)
        if bot_manager:
            await bot_manager.stop_bot(bot_db_id)

        from config import BOT_TOKEN
        import httpx
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={
                    "chat_id": owner_id,
                    "text": (
                        f"⚠️ <b>Bot 已失效</b>\n\n"
                        f"🤖 @{bot_username} 的 Token 已被撤销或 Bot 已被删除。\n"
                        f"系统已自动停止该 Bot。\n\n"
                        f"如需重新使用，请先 /delbot 删除后重新创建。"
                    ),
                    "parse_mode": "HTML"
                }
            )
        logger.info("已通知用户 %s Bot @%s 被撤销", owner_id, bot_username)
    except Exception as e:
        logger.error("通知 Bot 撤销失败: %s", e)


class BotManager:
    """
    用户Bot管理器

    负责：
    - 注册 / 注销用户Bot
    - 为每个用户Bot创建独立的 Application 和消息处理器
    - 管理用户Bot的运行状态
    - 动态添加/移除Bot
    """

    def __init__(self):
        self._apps: Dict[int, Application] = {}  # bot_db_id -> Application
        self.master_bot_username: str = ""  # 主Bot用户名，由 main.py 设置

    def _create_user_bot_app(self, token: str) -> Application:
        """为用户Bot创建 Application 实例，注册所有 FileID 处理器"""
        from handlers_commands import (
            start_command, create_collection_cmd, done_collection_cmd,
            cancel_collection_cmd, get_id_command, my_collections_cmd,
            delete_collection_cmd
        )
        from handlers_messages import (
            handle_attachment, handle_text, handle_forward,
            handle_group_media, handle_forwarded_media
        )
        from handlers_callbacks import button_callback

        application = (
            ApplicationBuilder()
            .token(token)
            .read_timeout(API_READ_TIMEOUT)
            .write_timeout(API_WRITE_TIMEOUT)
            .connect_timeout(API_CONNECT_TIMEOUT)
            .pool_timeout(API_CONNECT_TIMEOUT)
            .build()
        )

        # 聊天类型过滤：默认仅私聊，ALLOW_GROUP=True 时允许群组
        chat_filter = filters.ChatType.PRIVATE if not ALLOW_GROUP else filters.ALL

        # 注册命令处理器
        application.add_handler(CommandHandler("start", start_command, filters=chat_filter))
        application.add_handler(CommandHandler("help", start_command, filters=chat_filter))
        application.add_handler(CommandHandler("create", create_collection_cmd, filters=chat_filter))
        application.add_handler(CommandHandler("done", done_collection_cmd, filters=chat_filter))
        application.add_handler(CommandHandler("cancel", cancel_collection_cmd, filters=chat_filter))
        application.add_handler(CommandHandler("getid", get_id_command, filters=chat_filter))
        application.add_handler(CommandHandler("mycol", my_collections_cmd, filters=chat_filter))
        application.add_handler(CommandHandler("delcol", delete_collection_cmd, filters=chat_filter))

        # 转发的图片消息
        application.add_handler(MessageHandler(
            chat_filter & filters.FORWARDED & filters.PHOTO,
            handle_forwarded_media
        ))

        # 转发的其他媒体消息
        application.add_handler(MessageHandler(
            chat_filter & filters.FORWARDED & (filters.VIDEO | filters.Document.ALL | filters.AUDIO | filters.VOICE),
            handle_forwarded_media
        ))

        # 转发的非媒体消息
        application.add_handler(MessageHandler(
            chat_filter & filters.FORWARDED & filters.TEXT & ~filters.COMMAND,
            handle_forward
        ))

        # 图片处理
        application.add_handler(MessageHandler(
            chat_filter & filters.PHOTO,
            handle_group_media
        ))

        # 其他媒体处理
        application.add_handler(MessageHandler(
            chat_filter & (filters.VIDEO | filters.Document.ALL | filters.AUDIO | filters.VOICE),
            handle_group_media
        ))

        # 文本消息
        application.add_handler(MessageHandler(
            chat_filter & filters.TEXT & ~filters.COMMAND,
            handle_text
        ))

        # 回调按钮
        application.add_handler(CallbackQueryHandler(button_callback))

        # 错误处理（含 Token 撤销检测）
        async def user_bot_error_handler(update: object, context):
            error_str = str(context.error)
            error_type = type(context.error).__name__

            # 可忽略的错误：权限不足、消息未修改、消息删除等
            silent_errors = (
                "Not enough rights",           # Bot 在群组中没有发言权限
                "message is not modified",      # 消息内容未变化
                "message to delete not found",  # 消息已删除
                "Bad Request: chat not found",  # 聊天不存在
                "have no rights to send",       # 无发送权限
            )
            if any(err in error_str for err in silent_errors):
                logger.info("用户Bot @%s 权限/状态错误（已忽略）: %s", context.bot.username, error_str[:100])
                return

            # 检测 Token 被撤销 / Bot 被删除
            if "Unauthorized" in error_str or "401" in error_str or "bot was blocked" in error_str.lower():
                logger.warning("用户Bot @%s Token 已失效，自动停止", context.bot.username)
                asyncio.create_task(_auto_stop_revoked_bot(context.bot.username, context.bot_data))
                return

            # 其他错误：记录日志
            logger.error("用户Bot @%s 错误: %s", context.bot.username, error_str, exc_info=True)

            # 尝试通知用户（仅私聊中）
            if update and hasattr(update, 'effective_message') and update.effective_message:
                chat_type = update.effective_message.chat.type if update.effective_message.chat else ""
                if chat_type == "private":
                    try:
                        await update.effective_message.reply_text("❌ 处理请求时发生内部错误，请稍后重试。")
                    except Exception:
                        pass

        application.add_error_handler(user_bot_error_handler)

        return application

    def _get_webhook_url(self, bot_db_id: int) -> str:
        """生成 Bot 的 webhook URL"""
        return f"https://{WEBHOOK_HOST}{WEBHOOK_PATH}/{bot_db_id}"

    def _get_webhook_url_for_master(self) -> str:
        """生成主 Bot 的 webhook URL"""
        return f"https://{WEBHOOK_HOST}{WEBHOOK_PATH}/master"

    async def start_bot(self, bot_record: dict, max_retries: int = 2) -> bool:
        """创建并启动一个用户Bot（网络错误时自动重试，支持 polling/webhook 模式）"""
        bot_db_id = bot_record['id']
        if bot_db_id in self._apps:
            logger.info("Bot @%s 已在运行，跳过", bot_record.get('bot_username', 'unknown'))
            return True

        name = bot_record.get('bot_username', 'unknown')
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                app = self._create_user_bot_app(bot_record['bot_token'])
                app.bot_data['bot_record'] = bot_record

                await app.initialize()
                await app.start()

                # 手动注册Bot命令（post_init 在手动启动时不会自动触发）
                commands = [
                    ("start", "开始使用 / 查看帮助"),
                    ("help", "查看帮助"),
                    ("create", "创建集合 create 名称"),
                    ("done", "完成集合"),
                    ("cancel", "取消当前操作"),
                    ("getid", "回复消息获取文件ID"),
                    ("mycol", "查看我的集合"),
                    ("delcol", "删除集合 delcol 代码"),
                ]
                try:
                    await app.bot.set_my_commands(commands)
                    logger.info("用户Bot @%s 已注册 %d 个命令", app.bot.username, len(commands))
                except Exception as cmd_err:
                    logger.warning("用户Bot @%s 注册命令失败: %s", app.bot.username, cmd_err)

                if BOT_MODE == 'webhook':
                    # Webhook 模式：注册 webhook URL，不启动 polling
                    webhook_url = self._get_webhook_url(bot_db_id)
                    await app.bot.set_webhook(
                        url=webhook_url,
                        secret_token=WEBHOOK_SECRET or None,
                        allowed_updates=Update.ALL_TYPES,
                        drop_pending_updates=True,
                    )
                    logger.info("用户Bot @%s webhook 已设置: %s", name, webhook_url)
                else:
                    # Polling 模式
                    await app.updater.start_polling(
                        drop_pending_updates=True,
                        allowed_updates=Update.ALL_TYPES
                    )

                self._apps[bot_db_id] = app
                logger.info("用户Bot @%s 启动成功 (db_id=%s, mode=%s)", name, bot_db_id, BOT_MODE)
                return True

            except Exception as e:
                last_error = e
                error_str = str(e)

                # Token 无效，不重试，直接标记 revoked
                if "InvalidToken" in type(e).__name__ or ("token" in error_str.lower() and "rejected" in error_str.lower()):
                    logger.warning("用户Bot @%s Token 无效，标记为 revoked", name)
                    from database import update_user_bot_status
                    update_user_bot_status(bot_db_id, 'revoked')
                    return False

                # 网络错误，可重试
                if attempt < max_retries:
                    delay = 3 * (2 ** attempt)  # 3s, 6s
                    logger.warning("用户Bot @%s 启动失败 (第%d次)，%.1f秒后重试: %s",
                                   name, attempt + 1, delay, error_str[:100])
                    await asyncio.sleep(delay)
                else:
                    logger.error("用户Bot @%s 启动失败（已重试%d次）: %s", name, max_retries, error_str)

        return False

    async def stop_bot(self, bot_db_id: int) -> bool:
        """停止一个用户Bot"""
        app = self._apps.pop(bot_db_id, None)
        if not app:
            return False

        try:
            if BOT_MODE == 'webhook':
                # Webhook 模式：删除 webhook 再关闭
                try:
                    await app.bot.delete_webhook()
                except Exception:
                    pass
            else:
                await app.updater.stop()
            await app.stop()
            await app.shutdown()
            logger.info("用户Bot (db_id=%s) 已停止", bot_db_id)
            return True
        except Exception as e:
            logger.error("停止用户Bot失败: %s", e)
            return False

    async def handle_webhook_update(self, bot_db_id: int, update_data: dict) -> bool:
        """处理收到的 webhook 更新，分发给对应的 Bot"""
        app = self._apps.get(bot_db_id)
        if not app:
            logger.warning("收到未知 bot_db_id=%s 的 webhook 更新", bot_db_id)
            return False

        try:
            update = Update.de_json(update_data, app.bot)
            await app.process_update(update)
            return True
        except Exception as e:
            logger.error("处理 webhook 更新失败 (bot_db_id=%s): %s", bot_db_id, e)
            return False

    async def load_all(self) -> int:
        """从数据库加载所有活跃的用户Bot（限制并发数启动）"""
        bots = get_all_active_user_bots()
        logger.info("从数据库加载 %d 个用户Bot（最大并发 %d）", len(bots), MAX_CONCURRENT_STARTS)

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_STARTS)

        async def _start_one(bot):
            async with semaphore:
                success = await self.start_bot(bot)
            name = bot.get('bot_username', 'unknown')
            if success:
                logger.info("  ✅ @%s 已加载", name)
            else:
                logger.error("  ❌ @%s 加载失败", name)
            return success

        results = await asyncio.gather(*[_start_one(bot) for bot in bots])
        loaded = sum(1 for r in results if r)

        return loaded

    async def stop_all(self):
        """停止所有用户Bot"""
        for bot_db_id in list(self._apps.keys()):
            await self.stop_bot(bot_db_id)

    def get_all_apps(self) -> Dict[int, Application]:
        """获取所有用户Bot的Application实例"""
        return self._apps

    @property
    def active_count(self) -> int:
        """当前活跃Bot数量"""
        return len(self._apps)