"""
Bot 管理器
管理所有用户子Bot的生命周期：创建、启动、停止、删除
每个用户Bot拥有独立的 Application 实例和完整的 FileID 功能
"""
import asyncio
import logging
from typing import Dict, Optional

from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters
)

from db import (
    get_all_active_user_bots,
    update_user_bot_status
)
from senders import _retry_send
from config import (
    API_READ_TIMEOUT, API_WRITE_TIMEOUT, API_CONNECT_TIMEOUT,
    BOT_MODE, WEBHOOK_HOST, WEBHOOK_PATH, WEBHOOK_PORT, WEBHOOK_SECRET,
    ALLOW_GROUP, ROLE,
    WEBHOOK_UPDATE_TIMEOUT, PER_BOT_CONCURRENCY,
    USER_BOT_COMMANDS,
)

# 启动并发数限制，避免同时发起过多 Telegram API 请求
MAX_CONCURRENT_STARTS = 5

logger = logging.getLogger(__name__)


async def _auto_stop_revoked_bot(bot_username: str, bot_data: dict):
    """自动停止 Token 被撤销的 Bot，并通知管理员"""
    await asyncio.sleep(3)

    bot_record = bot_data.get('bot_record')
    if not bot_record:
        return

    owner_id = bot_record.get('owner_id')
    bot_db_id = bot_record.get('id')

    # 更新数据库状态
    from db import update_user_bot_status
    await update_user_bot_status(bot_db_id, 'revoked')

    # 停止 Bot
    try:
        import __main__
        bot_manager = getattr(__main__, 'bot_manager', None)
        if bot_manager:
            await bot_manager.stop_bot(bot_db_id)
    except Exception as e:
        logger.error("停止 Bot 失败: %s", e)

    # 通知管理员
    try:
        from config import BOT_TOKEN, ADMIN_IDS
        import httpx
        async with httpx.AsyncClient() as client:
            for admin_id in ADMIN_IDS:
                await client.post(
                    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                    json={
                        "chat_id": admin_id,
                        "text": (
                            f"⚠️ Bot 已失效\n\n"
                            f"🤖 @{bot_username} Token 已被撤销\n"
                            f"👤 所有者: {owner_id}\n"
                            f"系统已自动停止该 Bot。"
                        ),
                    }
                )
        logger.info("已通知管理员 Bot @%s 被撤销", bot_username)
    except Exception as e:
        logger.error("通知管理员失败: %s", e)


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
        self._webhook_monitor_task: Optional[asyncio.Task] = None  # webhook 监控任务
        # 防雪崩：每个 Bot 独立的并发信号量
        self._bot_semaphores: Dict[int, asyncio.Semaphore] = {}
        self._loading = False  # Bot 正在加载中，期间对未知 Bot 返回 503 让 Telegram 重试


    def _create_user_bot_app(self, token: str) -> Application:
        """为用户Bot创建 Application 实例，注册所有 FileID 处理器"""
        from handlers.commands import (
            start_command, create_collection_cmd, done_collection_cmd,
            cancel_collection_cmd, get_id_command, my_collections_cmd,
            delete_collection_cmd, stop_command, ex_command,
            pack_command
        )
        from handlers.messages import (
            handle_attachment, handle_text, handle_forward,
            handle_group_media, handle_forwarded_media
        )
        from handlers.callbacks import button_callback

        application = (
            ApplicationBuilder()
            .token(token)
            .concurrent_updates(True)
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
        application.add_handler(CommandHandler("pack", pack_command, filters=chat_filter))
        application.add_handler(CommandHandler("done", done_collection_cmd, filters=chat_filter))
        application.add_handler(CommandHandler("cancel", cancel_collection_cmd, filters=chat_filter))
        application.add_handler(CommandHandler("getid", get_id_command, filters=chat_filter))
        application.add_handler(CommandHandler("mycol", my_collections_cmd, filters=chat_filter))
        application.add_handler(CommandHandler("delcol", delete_collection_cmd, filters=chat_filter))
        application.add_handler(CommandHandler("stop", stop_command, filters=chat_filter, block=False))
        # /ex 隐藏命令：管理员专用，不注册到命令列表
        application.add_handler(CommandHandler("ex", ex_command, filters=chat_filter))

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

            # Flood control / 限流：仅记录 warning，不需要额外处理
            from telegram.error import RetryAfter
            if isinstance(context.error, RetryAfter) or "Flood control" in error_str or "RetryAfter" in error_type:
                retry_secs = context.error.retry_after if hasattr(context.error, 'retry_after') else '未知'
                logger.warning("用户Bot @%s 触发限流 (RetryAfter %ss)，已忽略", context.bot.username, retry_secs)
                return

            # 可忽略的错误：权限不足、消息未修改、消息删除等
            silent_errors = (
                "Not enough rights",           # Bot 在群组中没有发言权限
                "message is not modified",      # 消息内容未变化
                "message to delete not found",  # 消息已删除
                "Bad Request: chat not found",  # 聊天不存在
                "have no rights to send",       # 无发送权限
                "bot was blocked",             # 用户拉黑了 Bot
            )
            if any(err in error_str for err in silent_errors):
                logger.info("用户Bot @%s 权限/状态错误（已忽略）: %s", context.bot.username, error_str[:100])
                return

            # 检测 Token 被撤销 / Bot 被删除
            if "Unauthorized" in error_str or "401" in error_str:
                logger.warning("用户Bot @%s Token 已失效，自动停止", context.bot.username)
                asyncio.create_task(_auto_stop_revoked_bot(context.bot.username, context.bot_data))
                return

            # 检测 Bot 被冻结/注销 (Frozen_method_invalid)
            if 'Frozen_method' in error_str or 'frozen' in error_str.lower():
                logger.warning("用户Bot @%s 运行中被冻结/注销，标记为 frozen", context.bot.username)
                bot_record = context.bot_data.get('bot_record')
                if bot_record:
                    await update_user_bot_status(bot_record.get('id'), 'frozen')
                import __main__
                bot_manager = getattr(__main__, 'bot_manager', None)
                if bot_manager and bot_record:
                    asyncio.create_task(bot_manager.stop_bot(bot_record.get('id')))
                return

            # 其他错误：记录日志
            logger.error("用户Bot @%s 错误: %s", context.bot.username, error_str, exc_info=True)

            # 尝试通知用户（仅私聊中）
            if update and hasattr(update, 'effective_message') and update.effective_message:
                chat_type = update.effective_message.chat.type if update.effective_message.chat else ""
                if chat_type == "private":
                    try:
                        from senders import _retry_send
                        await _retry_send(update.effective_message.reply_text, "❌ 处理请求时发生内部错误，请稍后重试。")
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
                try:
                    await app.bot.set_my_commands(USER_BOT_COMMANDS)
                    logger.info("用户Bot @%s 已注册 %d 个命令", app.bot.username, len(USER_BOT_COMMANDS))
                except Exception as cmd_err:
                    cmd_err_str = str(cmd_err)
                    # Frozen_method_invalid = Bot 已被冻结/注销
                    if 'Frozen_method' in cmd_err_str or 'frozen' in cmd_err_str.lower():
                        logger.warning("用户Bot @%s 已被冻结/注销 (Frozen)，标记为 frozen", app.bot.username)
                        try:
                            await app.stop()
                            await app.shutdown()
                        except Exception:
                            pass
                        await update_user_bot_status(bot_db_id, 'frozen')
                        return False
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
                    await update_user_bot_status(bot_db_id, 'revoked')
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

    def _get_semaphore(self, bot_db_id: int) -> asyncio.Semaphore:
        """获取或创建每个 Bot 的并发信号量"""
        if bot_db_id not in self._bot_semaphores:
            self._bot_semaphores[bot_db_id] = asyncio.Semaphore(PER_BOT_CONCURRENCY)
        return self._bot_semaphores[bot_db_id]

    async def handle_webhook_update(self, bot_db_id: int, update_data: dict) -> bool:
        """处理收到的 webhook 更新，带并发限制和超时保护

        
        - 并发限制：每个 Bot 同时最多 PER_BOT_CONCURRENCY 个处理，超出排队等待
        - 超时保护：单个更新处理超过 WEBHOOK_UPDATE_TIMEOUT 秒自动取消
        - 失败时返回 False，由 main.py 返回 503 让 Telegram 自动重试
        """
        app = self._apps.get(bot_db_id)
        if not app:
            if self._loading:
                # Bot 正在加载中，返回 503 让 Telegram 自动重试，不丢失消息
                logger.info("收到 bot_db_id=%s 的 webhook 更新，但 Bot 正在加载中，返回 503 触发重试", bot_db_id)
                return False  # 503 → Telegram 自动重试
            # Bot 不在内存中（可能已被封/停止/未加载）
            # 尝试通过数据库获取 Token 并删除 webhook，阻止 Telegram 继续推送
            logger.warning("收到未知 bot_db_id=%s 的 webhook 更新，尝试清理 webhook", bot_db_id)
            asyncio.create_task(self._cleanup_orphan_webhook(bot_db_id))
            return True  # 返回 200 停止 Telegram 重试


        sem = self._get_semaphore(bot_db_id)
        async with sem:
            try:
                update = Update.de_json(update_data, app.bot)
                # 超时保护：单个更新处理不超过指定时间
                await asyncio.wait_for(
                    app.process_update(update),
                    timeout=WEBHOOK_UPDATE_TIMEOUT
                )
                return True
            except asyncio.TimeoutError:
                logger.error("Bot (db_id=%s) 更新处理超时 (%.0fs)，已取消", bot_db_id, WEBHOOK_UPDATE_TIMEOUT)
                return False
            except Exception as e:
                logger.error("处理 webhook 更新失败 (bot_db_id=%s): %s", bot_db_id, e)
                return False

    async def load_all(self) -> int:
        """从数据库加载所有活跃的用户Bot（限制并发数启动）"""
        self._loading = True
        try:
            bots = await get_all_active_user_bots()
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
        finally:
            self._loading = False
            logger.info("Bot 加载完成，共加载 %d 个，_loading=False", loaded)

    async def stop_all(self):
        """停止所有用户Bot"""
        # 停止 webhook 监控
        if self._webhook_monitor_task and not self._webhook_monitor_task.done():
            self._webhook_monitor_task.cancel()
            try:
                await self._webhook_monitor_task
            except asyncio.CancelledError:
                pass
            self._webhook_monitor_task = None
            logger.info("Webhook 监控已停止")

        for bot_db_id in list(self._apps.keys()):
            await self.stop_bot(bot_db_id)

    def get_all_apps(self) -> Dict[int, Application]:
        """获取所有用户Bot的Application实例"""
        return self._apps

    @property
    def active_count(self) -> int:
        """当前活跃Bot数量"""
        return len(self._apps)

    async def _cleanup_orphan_webhook(self, bot_db_id: int):
        """清理孤立 webhook：Bot 不在内存但 Telegram 仍在推送
        
        通过数据库获取 Token，直接调用 Telegram API 删除 webhook。
        添加防重复机制，避免对同一个 bot 反复清理。
        """
        if not hasattr(self, '_cleaned_orphans'):
            self._cleaned_orphans = set()

        if bot_db_id in self._cleaned_orphans:
            return  # 已清理过，跳过

        self._cleaned_orphans.add(bot_db_id)

        try:
            from db import get_user_bot_by_id
            bot_record = await get_user_bot_by_id(bot_db_id)
            if not bot_record:
                logger.debug("孤立 webhook 清理: bot_db_id=%s 不在数据库中", bot_db_id)
                return

            token = bot_record.get('bot_token')
            if not token:
                return

            # 直接调用 Telegram API 删除 webhook（不需要 Application 实例）
            import httpx
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{token}/deleteWebhook",
                    json={"drop_pending_updates": True}
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get('ok'):
                        logger.info("✅ 孤立 webhook 已清理: bot_db_id=%s @%s",
                                    bot_db_id, bot_record.get('bot_username', '?'))
                    else:
                        # Token 无效（Bot 被封），标记状态
                        err_desc = data.get('description', '')
                        if 'Unauthorized' in err_desc or '401' in str(data.get('error_code', '')):
                            logger.warning("孤立 webhook 清理: bot_db_id=%s Token 已失效，标记 revoked", bot_db_id)
                            await update_user_bot_status(bot_db_id, 'revoked')
                        else:
                            logger.warning("孤立 webhook 清理失败: bot_db_id=%s %s", bot_db_id, err_desc)
                else:
                    logger.warning("孤立 webhook 清理: bot_db_id=%s HTTP %s", bot_db_id, resp.status_code)
        except Exception as e:
            logger.error("清理孤立 webhook 异常 (bot_db_id=%s): %s", bot_db_id, e)

    async def _update_redis_status(self):
        """更新 Bot 状态到 Redis（供多节点共享）"""
        try:
            from redis_manager import get_redis
            r = await get_redis()
            status = {
                'active_bots': len(self._apps),
                'bot_ids': list(self._apps.keys()),
                'node_id': ROLE,
            }
            await r.set_bot_status(0, status, ttl=120)  # 0 = 本节点总状态
        except Exception:
            pass  # Redis 不可用时忽略

    async def start_webhook_monitor(self, interval: int = 300):
        """启动 webhook 定期验证，防止用户在别处使用 Bot Token"""
        if BOT_MODE != 'webhook':
            return

        async def _monitor_loop():
            logger.info("🔍 Webhook 监控已启动（间隔 %d 秒）", interval)
            while True:
                try:
                    await asyncio.sleep(interval)
                    await self._verify_all_webhooks()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error("Webhook 监控异常: %s", e)

        self._webhook_monitor_task = asyncio.create_task(_monitor_loop())

    async def _verify_all_webhooks(self):
        """验证所有用户 Bot：getMe 心跳检查 Token 有效性 + webhook 地址校验"""
        if not WEBHOOK_HOST:
            return

        checked = 0
        recovered = 0
        revoked_bots = []

        for bot_db_id, app in list(self._apps.items()):
            try:
                # 1. getMe 心跳检查 Token 是否有效
                try:
                    await app.bot.get_me()
                except Exception as e:
                    err_str = str(e)
                    if "Unauthorized" in err_str or "401" in err_str:
                        logger.warning("💔 心跳检测: Bot @%s (db_id=%s) Token 已失效", app.bot.username, bot_db_id)
                        revoked_bots.append((bot_db_id, app.bot.username, app.bot_data))
                        continue
                    else:
                        logger.error("心跳检测 Bot (db_id=%s) getMe 异常: %s", bot_db_id, err_str)
                        continue

                # 2. webhook 地址验证
                info = await app.bot.get_webhook_info()
                expected_url = self._get_webhook_url(bot_db_id)

                if info.url != expected_url:
                    logger.warning(
                        "⚠️ Bot @%s (db_id=%s) 的 webhook 被篡改！当前: %s，期望: %s",
                        app.bot.username, bot_db_id, info.url, expected_url
                    )
                    # 恢复正确的 webhook
                    await app.bot.set_webhook(
                        url=expected_url,
                        secret_token=WEBHOOK_SECRET or None,
                        allowed_updates=Update.ALL_TYPES,
                        drop_pending_updates=True,
                    )
                    recovered += 1
                    logger.info("✅ Bot @%s (db_id=%s) 的 webhook 已恢复", app.bot.username, bot_db_id)
                checked += 1
            except Exception as e:
                logger.error("验证 Bot (db_id=%s) 失败: %s", bot_db_id, e)

        # 处理被 revoked 的 Bot
        for bot_db_id, bot_username, bot_data in revoked_bots:
            await _auto_stop_revoked_bot(bot_username, bot_data)

        if checked > 0 or revoked_bots:
            logger.info("🔍 Webhook + 心跳验证完成：检查 %d 个 Bot，恢复 %d 个 webhook，revoked %d 个", checked, recovered, len(revoked_bots))

