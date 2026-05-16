"""管理员星星资产管理 - 查看 Bot 星星余额、交易记录、发送礼物"""
import logging
import time
from senders import _retry_send

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import ADMIN_IDS
from handlers.master._utils import escape

logger = logging.getLogger(__name__)


# ==================== API 辅助函数 ====================

async def _get_star_transactions(bot, limit=50, offset=0):
    """调用 Telegram API 获取星星交易记录"""
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"https://api.telegram.org/bot{bot.token}/getStarTransactions",
            json={"limit": limit, "offset": offset},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json()
        if not data.get("ok"):
            logger.error("getStarTransactions API 调用失败: %s", data.get("description"))
            return None, None
        result = data.get("result", {})
        return result.get("transactions", []), result.get("pending_transactions", [])


async def _get_bot_star_balance(bot):
    """获取 Bot 的星星余额"""
    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"https://api.telegram.org/bot{bot.token}/getStarTransactions",
            json={"limit": 1},
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json()
        if not data.get("ok"):
            return None
        return data.get("result", {}).get("star_count", None)


# ==================== 交易记录格式化 ====================

def _format_transaction(i: int, tx: dict) -> str:
    """格式化单条交易记录"""
    amount = tx.get("amount", 0)
    date_str = tx.get("date", "")

    source = tx.get("source", {})
    source_type = source.get("type", "unknown")

    if amount > 0:
        if source_type == "user":
            user_info = source.get("first_name", f"用户{source.get('user_id', '?')}")
            line = f"{i}. <b>+{amount}⭐</b> ← {escape(user_info)}"
        elif source_type == "telegram_ad":
            line = f"{i}. <b>+{amount}⭐</b> ← Telegram Ads"
        else:
            line = f"{i}. <b>+{amount}⭐</b> ← {source_type}"
    else:
        receiver = tx.get("receiver", {})
        recv_type = receiver.get("type", "unknown")
        if recv_type == "user":
            recv_name = receiver.get("first_name", f"用户{receiver.get('user_id', '?')}")
            line = f"{i}. <b>-{abs(amount)}⭐</b> → {escape(recv_name)}"
        elif recv_type == "telegram_ad":
            line = f"{i}. <b>-{abs(amount)}⭐</b> → Telegram Ads"
        else:
            line = f"{i}. <b>-{abs(amount)}⭐</b> → {recv_type}"

    if date_str:
        line += f"  <i>({date_str})</i>"
    return line


def _build_stars_main_keyboard():
    """构建星星资产页面的按钮"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎁 发送礼物", callback_data="stars_gift_list|0"),
            InlineKeyboardButton("🔄 刷新", callback_data="stars_refresh"),
        ],
        [
            InlineKeyboardButton("📜 查看更多交易", callback_data="stars_tx_more|20"),
        ],
    ])


# ==================== /mystars 命令 ====================

async def mystars_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mystars 管理员查看 Bot 收到的星星"""
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await _retry_send(update.message.reply_text, "⛔ 此命令仅限管理员使用。")
        return

    status_msg = await _retry_send(update.message.reply_text, "⏳ 正在查询星星信息...")

    try:
        balance = await _get_bot_star_balance(context.bot)
        transactions, pending = await _get_star_transactions(context.bot, limit=20)

        total_income = sum(tx.get("amount", 0) for tx in (transactions or []) if tx.get("amount", 0) > 0)
        total_spend = sum(abs(tx.get("amount", 0)) for tx in (transactions or []) if tx.get("amount", 0) < 0)

        text = "⭐ <b>Bot 星星资产</b>\n\n"
        if balance is not None:
            text += f"💰 <b>当前余额：</b>{balance:,} ⭐\n"
        text += (
            f"📊 <b>总收入：</b>{total_income:,} ⭐\n"
            f"📤 <b>总支出：</b>{total_spend:,} ⭐\n"
        )

        if transactions:
            text += f"\n━━━━━━━━━━━━━━━━━━\n"
            text += f"📋 <b>近期交易（最近 {len(transactions)} 条）</b>\n\n"
            for i, tx in enumerate(transactions[:15], 1):
                text += _format_transaction(i, tx) + "\n"
        else:
            text += "\n📭 暂无交易记录。"

        if pending:
            text += f"\n⏳ <b>待处理交易：</b>{len(pending)} 笔\n"

        await status_msg.edit_text(text, parse_mode="HTML", reply_markup=_build_stars_main_keyboard())

    except Exception as e:
        logger.error("查询星星信息失败: %s", e, exc_info=True)
        await status_msg.edit_text(f"❌ 查询失败：{escape(str(e)[:100])}")


# ==================== 星星交易查看回调 ====================

async def stars_refresh_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """刷新星星信息"""
    query = update.callback_query
    await query.answer()

    try:
        balance = await _get_bot_star_balance(context.bot)
        transactions, pending = await _get_star_transactions(context.bot, limit=20)

        total_income = sum(tx.get("amount", 0) for tx in (transactions or []) if tx.get("amount", 0) > 0)
        total_spend = sum(abs(tx.get("amount", 0)) for tx in (transactions or []) if tx.get("amount", 0) < 0)

        text = "⭐ <b>Bot 星星资产</b>\n\n"
        if balance is not None:
            text += f"💰 <b>当前余额：</b>{balance:,} ⭐\n"
        text += (
            f"📊 <b>总收入：</b>{total_income:,} ⭐\n"
            f"📤 <b>总支出：</b>{total_spend:,} ⭐\n"
        )

        if transactions:
            text += f"\n━━━━━━━━━━━━━━━━━━\n"
            text += f"📋 <b>近期交易（最近 {len(transactions)} 条）</b>\n\n"
            for i, tx in enumerate(transactions[:15], 1):
                text += _format_transaction(i, tx) + "\n"
        else:
            text += "\n📭 暂无交易记录。"

        if pending:
            text += f"\n⏳ <b>待处理交易：</b>{len(pending)} 笔\n"

        await query.message.edit_text(text, parse_mode="HTML", reply_markup=_build_stars_main_keyboard())

    except Exception as e:
        logger.error("刷新星星信息失败: %s", e)
        await query.message.edit_text(f"❌ 刷新失败：{escape(str(e)[:100])}")


async def stars_tx_more_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """查看更多交易记录"""
    query = update.callback_query
    await query.answer()

    parts = query.data.split("|")
    offset = int(parts[1]) if len(parts) > 1 else 20

    try:
        transactions, _ = await _get_star_transactions(context.bot, limit=20, offset=offset)

        if not transactions:
            await query.answer("没有更多交易记录了", show_alert=True)
            return

        text = f"📋 <b>交易记录（偏移 {offset}）</b>\n\n"
        for i, tx in enumerate(transactions, offset + 1):
            text += _format_transaction(i, tx) + "\n"

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ 返回", callback_data="stars_refresh"),
            InlineKeyboardButton("➡️ 下一页", callback_data=f"stars_tx_more|{offset + 20}"),
        ]])
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        logger.error("查看更多交易失败: %s", e)
        await query.message.edit_text(f"❌ 查询失败：{escape(str(e)[:100])}")


# ==================== 礼物发送功能 ====================

_gifts_cache = None
_gifts_cache_time = 0
_GIFTS_CACHE_TTL = 300  # 5 分钟缓存


async def _get_available_gifts(bot, force_refresh=False):
    """获取可用礼物列表（带缓存）"""
    global _gifts_cache, _gifts_cache_time

    now = time.time()
    if not force_refresh and _gifts_cache and (now - _gifts_cache_time) < _GIFTS_CACHE_TTL:
        return _gifts_cache

    import aiohttp
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"https://api.telegram.org/bot{bot.token}/getAvailableGifts",
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json()
        if not data.get("ok"):
            logger.error("getAvailableGifts API 调用失败: %s", data.get("description"))
            return None

        gifts = data.get("result", {}).get("gifts", [])
        _gifts_cache = gifts
        _gifts_cache_time = now
        return gifts


async def stars_gift_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """显示可发送的礼物列表"""
    query = update.callback_query
    await query.answer()

    parts = query.data.split("|")
    page = int(parts[1]) if len(parts) > 1 else 0

    gifts = await _get_available_gifts(context.bot)
    if not gifts:
        await query.message.reply_text("❌ 获取礼物列表失败，请稍后重试。")
        return

    per_page = 6
    total = len(gifts)
    total_pages = (total + per_page - 1) // per_page
    page = min(page, max(total_pages - 1, 0))
    start = page * per_page
    end = min(start + per_page, total)

    text = f"🎁 <b>选择要发送的礼物</b>（第 {page + 1}/{total_pages} 页）\n\n"

    keyboard = []
    for i in range(start, end):
        gift = gifts[i]
        gift_id = gift.get("id", "")
        star_count = gift.get("star_count", 0)
        remaining = gift.get("remaining_count", -1)

        # Gift 对象没有 title 字段，用 sticker 信息构建显示名称
        sticker = gift.get("sticker", {})
        sticker_emoji = sticker.get("emoji", "🎁")
        set_name = sticker.get("set_name", "")
        # 用 emoji + ID 后缀作为标识
        gift_label = f"{sticker_emoji} Gift #{gift_id}" if gift_id else f"{sticker_emoji} 礼物{i+1}"

        text += f"  • {sticker_emoji} {escape(gift_label)} — {star_count}⭐"
        if remaining >= 0:
            text += f" （剩余 {remaining}）"
        text += "\n"

        btn_text = f"🚫 {gift_label}（已售罄）" if remaining == 0 else f"{sticker_emoji} {gift_label} {star_count}⭐"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"stars_gift_sel|{gift_id}")])

    # 翻页
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"stars_gift_list|{page - 1}"))
    nav_row.append(InlineKeyboardButton("🔄 刷新", callback_data="stars_gift_list|0"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("➡️ 下一页", callback_data=f"stars_gift_list|{page + 1}"))
    keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton("⬅️ 返回星星资产", callback_data="stars_refresh")])

    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def stars_gift_sel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """选择礼物后，选择接收者"""
    query = update.callback_query
    await query.answer()

    parts = query.data.split("|")
    gift_id = parts[1] if len(parts) > 1 else ""
    if not gift_id:
        await query.answer("❌ 礼物信息错误", show_alert=True)
        return

    # 查找礼物信息
    gifts = await _get_available_gifts(context.bot)
    gift_info = None
    if gifts:
        for g in gifts:
            if g.get("id") == gift_id:
                gift_info = g
                break

    if not gift_info:
        await query.answer("❌ 未找到该礼物，请刷新重试", show_alert=True)
        return

    sticker = gift_info.get("sticker", {})
    gift_name = f"{sticker.get('emoji', '🎁')} Gift #{gift_id}"
    star_count = gift_info.get("star_count", 0)

    # 存储到 user_data
    context.user_data['selected_gift_id'] = gift_id
    context.user_data['selected_gift_name'] = gift_name
    context.user_data['selected_gift_stars'] = star_count

    # 获取近期付费用户
    recent_users = []
    try:
        transactions, _ = await _get_star_transactions(context.bot, limit=50)
        if transactions:
            seen = set()
            for tx in transactions:
                if tx.get("amount", 0) > 0:
                    source = tx.get("source", {})
                    if source.get("type") == "user":
                        uid = source.get("user_id")
                        if uid and uid not in seen:
                            seen.add(uid)
                            recent_users.append({
                                "user_id": uid,
                                "first_name": source.get("first_name", f"用户{uid}"),
                            })
                            if len(recent_users) >= 8:
                                break
    except Exception:
        pass

    text = (
        f"🎁 <b>已选择礼物：</b>{escape(gift_name)}（{star_count}⭐）\n\n"
        f"请选择接收礼物的用户："
    )

    keyboard = []
    if recent_users:
        text += "\n\n<b>近期付费用户：</b>\n"
        row = []
        for u in recent_users:
            row.append(InlineKeyboardButton(
                f"👤 {u['first_name'][:10]}",
                callback_data=f"stars_gift_user|{u['user_id']}"
            ))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)

    keyboard.append([InlineKeyboardButton("✏️ 手动输入用户 ID", callback_data="stars_gift_input")])
    keyboard.append([InlineKeyboardButton("⬅️ 返回礼物列表", callback_data="stars_gift_list|0")])

    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def stars_gift_input_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """手动输入用户 ID 模式"""
    query = update.callback_query
    await query.answer()

    gift_id = context.user_data.get('selected_gift_id', '')
    gift_name = context.user_data.get('selected_gift_name', '未知礼物')
    star_count = context.user_data.get('selected_gift_stars', 0)

    if not gift_id:
        await query.answer("❌ 请先选择礼物", show_alert=True)
        return

    context.user_data['waiting_gift_user_id'] = True

    text = (
        f"🎁 <b>发送礼物：</b>{escape(gift_name)}（{star_count}⭐）\n\n"
        f"请在下方消息中输入接收者的 <b>用户 ID</b>（数字）\n\n"
        f"💡 提示：可以让用户发送 /start 给主 Bot，从日志中获取其用户 ID。"
    )

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ 取消", callback_data="stars_gift_cancel")]])
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


async def stars_gift_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """选择了用户后发送礼物"""
    query = update.callback_query
    await query.answer()

    parts = query.data.split("|")
    target_user_id = int(parts[1]) if len(parts) > 1 else 0
    if not target_user_id:
        await query.answer("❌ 用户信息错误", show_alert=True)
        return

    await _do_send_gift(update, context, target_user_id, query=query)


async def _do_send_gift(update, context, target_user_id, query=None):
    """执行礼物发送"""
    gift_id = context.user_data.get('selected_gift_id', '')
    gift_name = context.user_data.get('selected_gift_name', '未知礼物')
    star_count = context.user_data.get('selected_gift_stars', 0)

    if not gift_id:
        msg = "❌ 请先选择礼物"
        if query:
            await query.answer(msg, show_alert=True)
        else:
            await _retry_send(update.message.reply_text, msg)
        return

    send_msg = await (query.message if query else update.message).reply_text(
        f"⏳ 正在发送礼物 {escape(gift_name)} 给用户 {target_user_id}..."
    )

    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.telegram.org/bot{context.bot.token}/sendGift",
                json={
                    "gift_id": gift_id,
                    "user_id": target_user_id,
                    "pay_for_upgrade": True,
                },
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                result = await resp.json()

        if result.get("ok"):
            text = (
                f"✅ <b>礼物发送成功！</b>\n\n"
                f"🎁 <b>礼物：</b>{escape(gift_name)}\n"
                f"⭐ <b>花费：</b>{star_count} 星星\n"
                f"👤 <b>接收者 ID：</b><a href=\"tg://user?id={target_user_id}\">{target_user_id}</a>"
            )
            logger.info("管理员发送礼物 %s (%d⭐) 给用户 %s", gift_name, star_count, target_user_id)
        else:
            error_desc = result.get("description", "未知错误")
            text = (
                f"❌ <b>礼物发送失败</b>\n\n"
                f"🎁 {escape(gift_name)} → 用户 {target_user_id}\n"
                f"错误：{escape(error_desc)}"
            )
            logger.error("发送礼物失败: %s", error_desc)

        # 清理 user_data
        for key in ('selected_gift_id', 'selected_gift_name', 'selected_gift_stars', 'waiting_gift_user_id'):
            context.user_data.pop(key, None)

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🎁 继续发送", callback_data="stars_gift_list|0"),
            InlineKeyboardButton("⬅️ 返回", callback_data="stars_refresh"),
        ]])
        await send_msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)

    except Exception as e:
        logger.error("发送礼物异常: %s", e, exc_info=True)
        await send_msg.edit_text(f"❌ 发送礼物异常：{escape(str(e)[:100])}")


async def stars_gift_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """取消礼物发送"""
    query = update.callback_query
    await query.answer("已取消")

    for key in ('selected_gift_id', 'selected_gift_name', 'selected_gift_stars', 'waiting_gift_user_id'):
        context.user_data.pop(key, None)

    await stars_refresh_callback(update, context)


async def handle_gift_user_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理手动输入的用户 ID（发送礼物用）"""
    if not context.user_data.get('waiting_gift_user_id'):
        return

    text = update.message.text.strip()
    try:
        target_user_id = int(text)
    except ValueError:
        await _retry_send(update.message.reply_text, "❌ 请输入有效的用户 ID（纯数字），或点击取消。")
        return

    if target_user_id <= 0:
        await _retry_send(update.message.reply_text, "❌ 用户 ID 必须为正整数，请重新输入。")
        return

    context.user_data.pop('waiting_gift_user_id', None)
    await _do_send_gift(update, context, target_user_id)


# ==================== 回调路由器 ====================

async def stars_callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """星星/礼物相关回调路由（管理员）"""
    query = update.callback_query
    data = query.data

    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await query.answer("⛔ 仅限管理员", show_alert=True)
        return

    if data == "stars_refresh":
        await stars_refresh_callback(update, context)
    elif data.startswith("stars_tx_more|"):
        await stars_tx_more_callback(update, context)
    elif data.startswith("stars_gift_list|"):
        await stars_gift_list_callback(update, context)
    elif data.startswith("stars_gift_sel|"):
        await stars_gift_sel_callback(update, context)
    elif data == "stars_gift_input":
        await stars_gift_input_callback(update, context)
    elif data.startswith("stars_gift_user|"):
        await stars_gift_user_callback(update, context)
    elif data == "stars_gift_cancel":
        await stars_gift_cancel_callback(update, context)
    else:
        await query.answer("未知操作", show_alert=True)