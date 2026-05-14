"""
MTProto 异常行为检测器 — 消息 ID 连续性分析

检测原理：
  当有人通过 MTProto 客户端使用 Bot Token 时，MTProto Bot 也会收到
  用户消息并发送回复。这些回复消息会占用聊天中的 message_id 序号。
  
  正常情况下，用户发给 Bot 的消息 message_id 应该是连续递增的。
  如果中间出现较大跳跃（gap > 阈值），说明有第三方通过 MTProto
  在同一聊天中发送了消息，占用了 message_id。

  例如：
    用户消息 msg_id=10 → 我们处理
    MTProto Bot 回复 msg_id=11, 12 → 我们看不到
    用户消息 msg_id=13 → 我们收到，发现 10→13 跳跃了 3

触发机制：
  累积 3 次可疑跳跃后执行封禁（停止 Bot + 标记 compromised）
  每次触发都会通知管理员（含当前计数和跳跃详情）

启用方式：
  环境变量 MTPROTO_DETECTION=true 开启（默认关闭）

管理命令：
  /mtproto — 查看状态
  /mtproto on/off — 开关
  /mtproto threshold 数字 — 设置跳跃阈值（默认 2）
"""
import logging
import time
from typing import List, Dict, Tuple

logger = logging.getLogger(__name__)

# 默认封禁阈值（累积触发次数）
DEFAULT_STRIKE_THRESHOLD = 3
# 默认跳跃阈值（message_id 间隙超过此值视为可疑）
DEFAULT_GAP_THRESHOLD = 2


class MTProtoDetector:
    """基于消息 ID 连续性的 MTProto 异常检测器"""

    def __init__(self):
        # 消息 ID 跟踪：(bot_db_id, chat_id) -> last_message_id
        self._last_msg_ids: Dict[Tuple[int, int], int] = {}
        # 触发计数器：bot_db_id -> count
        self._strike_counts: Dict[int, int] = {}
        # 最近触发详情：bot_db_id -> list of (gap, chat_id, timestamp)
        self._strike_details: Dict[int, list] = {}

    async def is_enabled(self) -> bool:
        """检查检测是否开启（环境变量 + 数据库设置双重控制）"""
        from config import MTPROTO_DETECTION
        if not MTPROTO_DETECTION:
            return False
        from database import get_platform_setting
        db_setting = await get_platform_setting('mtproto_detection', 'on')
        return db_setting == 'on'

    async def get_gap_threshold(self) -> int:
        """获取跳跃阈值"""
        from database import get_platform_setting
        val = await get_platform_setting('mtproto_gap_threshold', str(DEFAULT_GAP_THRESHOLD))
        try:
            return max(1, int(val))
        except (ValueError, TypeError):
            return DEFAULT_GAP_THRESHOLD

    def get_strike_count(self, bot_db_id: int) -> int:
        return self._strike_counts.get(bot_db_id, 0)

    def reset_strike_count(self, bot_db_id: int):
        self._strike_counts.pop(bot_db_id, None)
        self._strike_details.pop(bot_db_id, None)
        # 清理该 Bot 的消息 ID 跟踪
        keys_to_remove = [k for k in self._last_msg_ids if k[0] == bot_db_id]
        for k in keys_to_remove:
            del self._last_msg_ids[k]

    async def check(self, bot_db_id: int, app, update_data: dict) -> bool:
        """
        检查消息 ID 连续性。
        返回 True 表示达到封禁阈值。
        """
        if not await self.is_enabled():
            return False

        # 获取消息
        message = update_data.get('message')
        if not message:
            return False

        # 只检查私聊
        chat = message.get('chat', {})
        if chat.get('type') != 'private':
            return False

        chat_id = chat.get('id')
        msg_id = message.get('message_id')
        if not chat_id or not msg_id:
            return False

        # 检查是否已 compromised
        bot_record = app.bot_data.get('bot_record')
        if bot_record and bot_record.get('status') == 'compromised':
            return False

        key = (bot_db_id, chat_id)
        last_id = self._last_msg_ids.get(key, 0)

        # 更新跟踪（始终记录最新的 message_id）
        self._last_msg_ids[key] = max(last_id, msg_id)

        # 没有基线，跳过
        if last_id == 0:
            return False

        # 计算间隙（gap=1 是正常的连续消息）
        gap = msg_id - last_id
        gap_threshold = await self.get_gap_threshold()

        # 间隙超过阈值 → 可疑
        if gap > gap_threshold:
            bot_username = getattr(app.bot, 'username', '?')
            current_count = self._strike_counts.get(bot_db_id, 0) + 1
            self._strike_counts[bot_db_id] = current_count

            # 记录详情
            if bot_db_id not in self._strike_details:
                self._strike_details[bot_db_id] = []
            self._strike_details[bot_db_id].append({
                'gap': gap,
                'chat_id': chat_id,
                'last_id': last_id,
                'current_id': msg_id,
                'time': time.strftime('%H:%M:%S'),
            })

            logger.warning(
                "🚨 MTProto 消息ID跳跃检测！Bot @%s (db_id=%s) "
                "聊天 %s: %d → %d (gap=%d, 阈值=%d) [%d/%d]",
                bot_username, bot_db_id, chat_id, last_id, msg_id,
                gap, gap_threshold, current_count, DEFAULT_STRIKE_THRESHOLD
            )

            # 每次触发都通知管理员
            await self._notify_admin_strike(
                bot_username, bot_db_id,
                bot_record.get('owner_id', '?') if bot_record else '?',
                chat_id, last_id, msg_id, gap, gap_threshold,
                current_count, DEFAULT_STRIKE_THRESHOLD
            )

            # 达到封禁阈值
            if current_count >= DEFAULT_STRIKE_THRESHOLD:
                return True

        return False

    async def handle_compromised(self, bot_db_id: int, app, update_data: dict, bot_manager=None):
        """处理封禁：停止 Bot + 标记 compromised + 通知管理员"""
        from database import update_user_bot_status

        bot_record = app.bot_data.get('bot_record', {})
        bot_username = getattr(app.bot, 'username', 'unknown')
        owner_id = bot_record.get('owner_id', 'unknown')
        details = self._strike_details.get(bot_db_id, [])

        logger.warning("🚨 Bot @%s 已达 %d 次消息ID跳跃触发，执行封禁！", bot_username, DEFAULT_STRIKE_THRESHOLD)

        await update_user_bot_status(bot_db_id, 'compromised')
        self.reset_strike_count(bot_db_id)

        if bot_manager:
            await bot_manager.stop_bot(bot_db_id)

        await self._notify_admin_banned(bot_username, bot_db_id, owner_id, details)

    # ========== 通知方法 ==========

    async def _notify_admin_strike(self, bot_username, bot_db_id, owner_id,
                                    chat_id, last_id, msg_id, gap, gap_threshold,
                                    current, threshold):
        try:
            from config import BOT_TOKEN, ADMIN_IDS
            import httpx

            remaining = threshold - current
            levels = {1: "⚠️ 警告", 2: "🟠 严重警告", 3: "🔴 即将封禁"}
            level = levels.get(current, "🔴")

            async with httpx.AsyncClient() as client:
                for admin_id in ADMIN_IDS:
                    await client.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                        json={
                            "chat_id": admin_id,
                            "text": (
                                f"{level} <b>消息ID跳跃 [{current}/{threshold}]</b>\n\n"
                                f"🤖 Bot：@{bot_username} (db_id: {bot_db_id})\n"
                                f"👤 所有者：<code>{owner_id}</code>\n"
                                f"💬 聊天：<code>{chat_id}</code>\n\n"
                                f"📊 message_id: {last_id} → {msg_id}\n"
                                f"📏 跳跃: {gap}（阈值: >{gap_threshold}）\n\n"
                                f"{'还差 ' + str(remaining) + ' 次将自动封禁。' if remaining > 0 else '已达阈值，即将执行封禁！'}"
                            ),
                            "parse_mode": "HTML",
                        }
                    )
        except Exception as e:
            logger.error("通知管理员失败: %s", e)

    async def _notify_admin_banned(self, bot_username, bot_db_id, owner_id, details):
        try:
            from config import BOT_TOKEN, ADMIN_IDS
            import httpx

            details_text = ""
            for i, d in enumerate(details[-5:], 1):  # 最近 5 条
                details_text += f"  {i}. 聊天 {d['chat_id']}: {d['last_id']}→{d['current_id']} (gap={d['gap']}) {d['time']}\n"

            async with httpx.AsyncClient() as client:
                for admin_id in ADMIN_IDS:
                    await client.post(
                        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                        json={
                            "chat_id": admin_id,
                            "text": (
                                f"🚨 <b>消息ID跳跃检测 — Bot 已封禁！</b>\n\n"
                                f"🤖 Bot：@{bot_username} (db_id: {bot_db_id})\n"
                                f"👤 所有者：<code>{owner_id}</code>\n\n"
                                f"📋 触发详情：\n{details_text}\n"
                                f"⚠️ 该 Bot 已自动停止并标记为 <b>compromised</b>。\n"
                                f"使用 <code>/startbot @{bot_username}</code> 可手动恢复。"
                            ),
                            "parse_mode": "HTML",
                        }
                    )
        except Exception as e:
            logger.error("通知管理员封禁失败: %s", e)