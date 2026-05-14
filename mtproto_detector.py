"""
MTProto 异常行为检测器（独立中间件插件）

检测原理：
  当有人通过第三方 MTProto 客户端使用 Bot Token 发消息时，
  Bot 会通过 Webhook 收到自己发送的消息。
  正常情况下，Webhook 永远不会收到 Bot 自身发的消息。
  一旦收到 = 有人在用 MTProto 客户端操作该 Bot Token。

双模式：
  strict（严格模式，默认，推荐）：
    任何来自 Bot 自身的消息都触发封禁，不依赖关键词。
    最强检测，MTProto 一活跃就立刻发现。

  keyword（关键词模式）：
    Bot 自身消息需包含预设关键词才触发。
    用于只想检测特定行为的场景。

启用方式：
  环境变量 MTPROTO_DETECTION=true 开启（默认关闭）

管理命令：
  /mtproto — 查看状态
  /mtproto on/off — 开关
  /mtproto mode strict/keyword — 切换模式
  /mtproto add 关键词 — 添加关键词
  /mtproto del 编号 — 删除关键词
"""
import logging
from typing import Optional, List

logger = logging.getLogger(__name__)


class MTProtoDetector:
    """MTProto 异常行为检测器"""

    def __init__(self):
        self._keywords_cache: List[str] = []
        self._cache_loaded: bool = False

    async def _ensure_cache(self):
        """确保关键词缓存已加载"""
        if not self._cache_loaded:
            await self.reload_cache()

    async def reload_cache(self):
        """从数据库重新加载关键词缓存"""
        from database import get_platform_setting
        keywords_raw = await get_platform_setting('mtproto_keywords', '')
        self._keywords_cache = [k.strip() for k in keywords_raw.split('||') if k.strip()]
        self._cache_loaded = True
        logger.info("MTProto 检测器：已加载 %d 个关键词", len(self._keywords_cache))

    async def is_enabled(self) -> bool:
        """检查检测是否开启（环境变量 + 数据库设置双重控制）"""
        from config import MTPROTO_DETECTION
        if not MTPROTO_DETECTION:
            return False

        from database import get_platform_setting
        db_setting = await get_platform_setting('mtproto_detection', 'on')
        return db_setting == 'on'

    async def get_mode(self) -> str:
        """获取当前检测模式：strict（严格）或 keyword（关键词）"""
        from database import get_platform_setting
        mode = await get_platform_setting('mtproto_mode', 'strict')
        return mode if mode in ('strict', 'keyword') else 'strict'

    async def check(self, bot_db_id: int, app, update_data: dict) -> bool:
        """
        检查 webhook update 是否触发 MTProto 异常检测。

        返回 True 表示检测到异常，调用方应执行封禁。

        检测逻辑：
        1. 严格模式：只要消息来自 Bot 自身就触发（最强）
        2. 关键词模式：Bot 自身消息 + 关键词匹配才触发
        """
        # 1. 检查是否已开启
        if not await self.is_enabled():
            return False

        # 2. 获取消息数据
        message_data = update_data.get('message')
        if not message_data:
            return False

        # 3. 检查消息是否来自 Bot 自身
        from_user = message_data.get('from')
        if not from_user:
            return False

        bot_record = app.bot_data.get('bot_record')
        if not bot_record:
            return False

        from_user_id = from_user.get('id')
        bot_telegram_id = bot_record.get('bot_id')
        if from_user_id != bot_telegram_id:
            return False

        # 4. 检查该 Bot 是否已经是 compromised 状态（避免重复报警）
        if bot_record.get('status') == 'compromised':
            return False

        # ========= 至此确认：消息来自 Bot 自身 =========
        # 正常情况下 Webhook 永远不会收到 Bot 自己发的消息
        # 收到 = 有人在用 MTProto 客户端

        mode = await self.get_mode()

        # 5. 严格模式：直接触发
        if mode == 'strict':
            logger.warning(
                "🚨 MTProto 严格模式触发！Bot @%s (db_id=%s) 收到自身消息（MTProto 登录）",
                getattr(app.bot, 'username', '?'), bot_db_id
            )
            return True

        # 6. 关键词模式：需匹配关键词
        text = message_data.get('text', '') or ''
        caption = message_data.get('caption', '') or ''
        full_text = f"{text} {caption}".strip()

        if not full_text:
            return False

        await self._ensure_cache()
        matched_keyword = None
        for keyword in self._keywords_cache:
            if keyword in full_text:
                matched_keyword = keyword
                break

        if matched_keyword:
            logger.warning(
                "🚨 MTProto 关键词模式触发！Bot @%s (db_id=%s) 自身消息包含关键词: %s",
                getattr(app.bot, 'username', '?'), bot_db_id, matched_keyword[:30]
            )
            return True

        # 关键词模式下没匹配到，但仍然记录一条 warning
        logger.warning(
            "⚠️ Bot @%s (db_id=%s) 收到自身消息但关键词未匹配（模式: keyword）",
            getattr(app.bot, 'username', '?'), bot_db_id
        )
        return False

    async def handle_compromised(self, bot_db_id: int, app, update_data: dict, bot_manager=None):
        """处理被检测到 MTProto 异常的 Bot：停止 + 标记 + 通知管理员"""
        from database import update_user_bot_status

        bot_record = app.bot_data.get('bot_record', {})
        bot_username = getattr(app.bot, 'username', 'unknown')
        owner_id = bot_record.get('owner_id', 'unknown')
        mode = await self.get_mode()

        logger.warning(
            "🚨 Bot @%s (db_id=%s) 检测到 MTProto 异常行为（%s模式），正在停止...",
            bot_username, bot_db_id, mode
        )

        # 1. 更新数据库状态为 compromised
        await update_user_bot_status(bot_db_id, 'compromised')

        # 2. 停止 Bot
        if bot_manager:
            await bot_manager.stop_bot(bot_db_id)

        # 3. 通知管理员
        await self._notify_admins(bot_username, bot_db_id, owner_id, update_data, mode)

    async def _notify_admins(self, bot_username: str, bot_db_id: int, owner_id, update_data: dict, mode: str = 'strict'):
        """通知管理员检测到异常"""
        try:
            from config import BOT_TOKEN, ADMIN_IDS
            import httpx

            message_data = update_data.get('message', {})
            sample_text = (message_data.get('text', '') or '')[:100]
            mode_text = "严格模式（任何自身消息）" if mode == 'strict' else "关键词模式"

            async with httpx.AsyncClient() as client:
                for admin_id in ADMIN_IDS:
                    await client.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                        json={
                            "chat_id": admin_id,
                            "text": (
                                f"🚨 <b>MTProto 异常行为检测触发！</b>\n\n"
                                f"🤖 Bot：@{bot_username} (db_id: {bot_db_id})\n"
                                f"👤 所有者：<code>{owner_id}</code>\n"
                                f"🔍 检测模式：{mode_text}\n\n"
                                f"📝 消息样本：\n<code>{sample_text}</code>\n\n"
                                f"⚠️ 该 Bot 已自动停止并标记为 <b>compromised</b>。\n"
                                f"使用 <code>/startbot @{bot_username}</code> 可手动恢复。"
                            ),
                            "parse_mode": "HTML",
                        }
                    )
            logger.info("已通知管理员 Bot @%s 的 MTProto 异常", bot_username)
        except Exception as e:
            logger.error("通知管理员 MTProto 检测失败: %s", e)

    def get_keywords(self) -> List[str]:
        """获取当前缓存的关键词列表"""
        return list(self._keywords_cache)

    @property
    def keywords_count(self) -> int:
        """当前关键词数量"""
        return len(self._keywords_cache)