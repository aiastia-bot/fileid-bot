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

from database import (
    get_all_active_user_bots, get_best_worker_node, get_online_worker_nodes,
    register_worker_node, update_worker_heartbeat, set_worker_offline,
    update_user_bot_node, get_active_bots_by_node, get_user_bot_by_id,
    update_user_bot_status
)
from config import (
    API_READ_TIMEOUT, API_WRITE_TIMEOUT, API_CONNECT_TIMEOUT,
    BOT_MODE, WEBHOOK_HOST, WEBHOOK_PATH, WEBHOOK_PORT, WEBHOOK_SECRET,
    ALLOW_GROUP, ROLE, WORKER_SECRET, WORKER_WEBHOOK_HOST,
    MAX_BOTS_PER_WORKER
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
    from database import update_user_bot_status
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

    def _create_user_bot_app(self, token: str) -> Application:
        """为用户Bot创建 Application 实例，注册所有 FileID 处理器"""
        from handlers_commands import (
            start_command, create_collection_cmd, done_collection_cmd,
            cancel_collection_cmd, get_id_command, my_collections_cmd,
            delete_collection_cmd, stop_command
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
        application.add_handler(CommandHandler("stop", stop_command, filters=chat_filter))

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
                    ("stop", "停止所有发送任务"),
                    ("delcol", "删除集合 delcol 代码"),
                ]
                try:
                    await app.bot.set_my_commands(commands)
                    logger.info("用户Bot @%s 已注册 %d 个命令", app.bot.username, len(commands))
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


class MasterScheduler:
    """
    Master 调度器
    在 master 模式下，将用户 Bot 分配到 Worker 节点运行
    自己不直接运行任何用户 Bot（或作为 fallback 本地运行）
    """

    def __init__(self):
        self.master_bot_username: str = ""

    async def assign_bot_to_worker(self, bot_record: dict) -> Optional[str]:
        """将 Bot 分配到最空闲的 Worker 节点，返回 node_id"""
        import httpx

        node = await get_best_worker_node()
        if not node:
            logger.warning("没有可用的 Worker 节点")
            return None

        node_id = node['node_id']
        node_url = node['node_url']
        bot_db_id = bot_record['id']

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{node_url}/internal/start",
                    json={
                        'bot_db_id': bot_db_id,
                        'bot_token': bot_record['bot_token'],
                        'bot_id': bot_record.get('bot_id'),
                        'bot_username': bot_record.get('bot_username', ''),
                        'bot_firstname': bot_record.get('bot_firstname', ''),
                        'owner_id': bot_record.get('owner_id', 0),
                    },
                    headers={'X-Worker-Secret': WORKER_SECRET},
                    timeout=30.0
                )

                if resp.status_code == 200:
                    # 更新数据库中的节点分配
                    await update_user_bot_node(bot_db_id, node_id)
                    logger.info("Bot @%s 已分配到 Worker %s", bot_record.get('bot_username', ''), node_id)
                    return node_id
                else:
                    logger.error("Worker %s 启动 Bot 失败: %s", node_id, resp.text)
                    return None

        except Exception as e:
            logger.error("联系 Worker %s 失败: %s", node_id, e)
            await set_worker_offline(node_id)
            return None

    async def stop_bot_on_worker(self, bot_db_id: int) -> bool:
        """通知 Worker 停止一个 Bot"""
        import httpx

        bot = await get_user_bot_by_id(bot_db_id)
        if not bot:
            return False

        node_id = bot.get('node_id', 'local')
        if node_id == 'local':
            return True

        # 查找 Worker 节点
        from database import get_all_worker_nodes
        nodes = {n['node_id']: n for n in await get_all_worker_nodes()}
        node = nodes.get(node_id)
        if not node or node['status'] != 'online':
            logger.warning("Worker %s 不在线，无法停止 Bot", node_id)
            return True

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{node['node_url']}/internal/stop",
                    json={'bot_db_id': bot_db_id},
                    headers={'X-Worker-Secret': WORKER_SECRET},
                    timeout=15.0
                )
                return resp.status_code == 200
        except Exception as e:
            logger.error("通知 Worker %s 停止 Bot 失败: %s", node_id, e)
            return False

    async def load_all_to_workers(self) -> int:
        """将所有活跃 Bot 分配到 Worker 节点"""
        bots = await get_all_active_user_bots()
        logger.info("开始分配 %d 个 Bot 到 Worker 节点", len(bots))

        loaded = 0
        for bot in bots:
            node_id = bot.get('node_id', 'local')

            # 已有分配且 Worker 在线，重新启动
            if node_id and node_id != 'local':
                assigned = await self.assign_bot_to_worker(bot)
                if assigned:
                    loaded += 1
                    logger.info("  ✅ @%s → %s", bot.get('bot_username', ''), assigned)
                else:
                    logger.error("  ❌ @%s 分配失败", bot.get('bot_username', ''))
            else:
                # 尚未分配，分配到最空闲的 Worker
                assigned = await self.assign_bot_to_worker(bot)
                if assigned:
                    loaded += 1
                    logger.info("  ✅ @%s → %s", bot.get('bot_username', ''), assigned)
                else:
                    logger.error("  ❌ @%s 无可用 Worker", bot.get('bot_username', ''))

        return loaded

    async def handle_worker_register(self, node_id: str, node_url: str,
                                      webhook_host: str = '',
                                      max_bots: int = MAX_BOTS_PER_WORKER) -> bool:
        """处理 Worker 注册请求"""
        await register_worker_node(node_id, node_url, webhook_host, max_bots)
        logger.info("Worker [%s] 已注册 (url=%s, webhook=%s)", node_id, node_url, webhook_host)
        return True

    async def handle_worker_heartbeat(self, node_id: str, active_bots: int) -> bool:
        """处理 Worker 心跳"""
        await update_worker_heartbeat(node_id, active_bots)
        return True

    async def handle_worker_offline(self, node_id: str) -> bool:
        """处理 Worker 离线"""
        await set_worker_offline(node_id)
        logger.warning("Worker [%s] 已离线", node_id)
        return True

    async def get_worker_status(self) -> List[Dict]:
        """获取所有 Worker 状态"""
        from database import get_all_worker_nodes
        return await get_all_worker_nodes()
