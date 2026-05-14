"""
MTProto 异常行为检测器（独立中间件插件）

检测原理：
  当有人通过第三方 MTProto 客户端使用 Bot Token 发消息时，
  Bot 会通过 Webhook 收到自己发送的消息。
  正常情况下，Webhook 永远不会收到 Bot 自身发的消息。
  一旦收到 = 有人在用 MTProto 客户端操作该 Bot Token。

  注意：用户转发 Bot 的消息给 Bot 不会触发误报，
  因为 from_user 是转发者（用户），不是 Bot 自身。

双模式：
  strict（严格模式，默认，推荐）：
    任何来自 Bot 自身的消息都触发。
    累积 3 次触发后执行封禁（停止 Bot + 标记 compromised）。
    每次触发都会通知管理员（包含当前计数）。

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

# 严格模式触发阈值
STRICT_THRESHOLD = 3


class MTProtoDetector:
    """MTProto 异常行为检测器"""

    def __init__(self):
        self._keywords_cache: List[str] = []
        self._cache_loaded: bool = False
        # 严格模式触发计数器 {bot_db_id: count}
        self._strike_counts: dict[int, int] = {}

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

    def get_strike_count(self, bot_db_id: int) -> int:
        """获取指定 Bot 的当前触发计数"""
        return self._strike_counts.get(bot_db_id, 0)

    def reset_strike_count(self, bot_db_id: int):
        """重置指定 Bot 的触发计数（管理员恢复 Bot 时调用）"""
        self._strike_counts.pop(bot_db_id, None)

    async def check(self, bot_db_id: int, app, update_data: dict) -> bool:
        """
        检查 webhook update 是否触发 MTProto 异常检测。

        返回 True 表示达到封禁阈值，调用方应执行封禁。

        检测逻辑：
        1. 严格模式：消息来自 Bot 自身就计数，累积 3 次封禁
        2. 关键词模式：Bot 自身消息 + 关键词匹配立即封禁

        注意：用户转发 Bot 的消息不会触发（from_user 是用户不是 Bot）
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

        # 5. 严格模式：累积计数
        if mode == 'strict':
            current_count = self._strike_counts.get(bot_db_id, 0) + 1
            self._strike_counts[bot_db_id] = current_count

            bot_username = getattr(app.bot, 'username', '?')
            logger.warning(
                "🚨 MTProto 严格模式触发！Bot @%s (db_id=%s) 收到自身消息 "
                "(%d/%d)",
                bot_username, bot_db_id, current_count, STRICT_THRESHOLD
            )

            # 每次触发都通知管理员
            await self._notify_admin_strike(
                bot_username, bot_db_id, bot_record.get('owner_id', '?'),
                update_data, current_count, STRICT_THRESHOLD
            )

            # 达到阈值才执行封禁
            if current_count >= STRICT_THRESHOLD:
                logger.warning(
                    "🚨 Bot @%s (db_id=%s) 已达 %d 次触发阈值，执行封禁！",
                    bot_username, bot_db_id, STRICT_THRESHOLD
                )
                return True
            return False

        # 6. 关键词模式：需匹配关键词（立即封禁，不累积）
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

        # 2. 重置计数器
        self.reset_strike_count(bot_db_id)

        # 3. 停止 Bot
        if bot_manager:
            await bot_manager.stop_bot(bot_db_id)

        # 4. 通知管理员 - 最终封禁通知
        await self._notify_admin_banned(bot_username, bot_db_id, owner_id, update_data, mode)

    async def _notify_admin_strike(self, bot_username: str, bot_db_id: int, owner_id,
                                     update_data: dict, current: int, threshold: int):
        """每次严格模式触发都通知管理员"""
        try:
            from config import BOT_TOKEN, ADMIN_IDS
            import httpx

            message_data = update_data.get('message', {})
            sample_text = (message_data.get('text', '') or '')[:100]

            remaining = threshold - current
            if remaining > 0:
                level = "⚠️ 警告" if current == 1 else "🟠 严重警告" if current == 2 else "🔴"
            else:
                level = "🔴 即将封禁"

            async with httpx.AsyncClient() as client:
                for admin_id in ADMIN_IDS:
                    await client.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                        json={
                            "chat_id": admin_id,
                            "text": (
                                f"{level} <b>MTProto 异常行为 [{current}/{threshold}]</b>\n\n"
                                f"🤖 Bot：@{bot_username} (db_id: {bot_db_id})\n"
                                f"👤 所有者：<code>{owner_id}</code>\n"
                                f"🔍 模式：严格模式\n\n"
                                f"📝 消息样本：\n<code>{sample_text}</code>\n\n"
                                f"{'还差 ' + str(remaining) + ' 次触发将自动封禁。' if remaining > 0 else '已达阈值，即将执行封禁！'}"
                            ),
                            "parse_mode": "HTML",
                        }
                    )
        except Exception as e:
            logger.error("通知管理员 MTProto 触发失败: %s", e)

    async def _notify_admin_banned(self, bot_username: str, bot_db_id: int, owner_id,
                                    update_data: dict, mode: str = 'strict'):
        """最终封禁通知"""
        try:
            from config import BOT_TOKEN, ADMIN_IDS
            import httpx

            message_data = update_data.get('message', {})
            sample_text = (message_data.get('text', '') or '')[:100]
            mode_text = "严格模式（累积触发）" if mode == 'strict' else "关键词模式"

            async with httpx.AsyncClient() as client:
                for admin_id in ADMIN_IDS:
                    await client.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                        json={
                            "chat_id": admin_id,
                            "text": (
                                f"🚨 <b>MTProto 检测 — Bot 已封禁！</b>\n\n"
                                f"🤖 Bot：@{bot_username} (db_id: {bot_db_id})\n"
                                f"👤 所有者：<code>{owner_id}</code>\n"
                                f"🔍 检测模式：{mode_text}\n\n"
                                f"📝 最后消息样本：\n<code>{sample_text}</code>\n\n"
                                f"⚠️ 该 Bot 已自动停止并标记为 <b>compromised</b>。\n"
                                f"使用 <code>/startbot @{bot_username}</code> 可手动恢复。"
                            ),
                            "parse_mode": "HTML",
                        }
                    )
            logger.info("已通知管理员 Bot @%s 的 MTProto 封禁", bot_username)
        except Exception as e:
            logger.error("通知管理员 MTProto 封禁失败: %s", e)

    def get_keywords(self) -> List[str]:
        """获取当前缓存的关键词列表"""
        return list(self._keywords_cache)

    @property
    def keywords_count(self) -> int:
        """当前关键词数量"""
        return len(self._keywords_cache)