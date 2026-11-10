"""管理员星星资产管理 - 查看 Bot 星星余额、交易记录、发送礼物"""
import logging
import time
from senders import _retry_send

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config import ADMIN_IDS, PREMIUM_GIFT_PRICES
from handlers.master._utils import escape

logger = logging.getLogger(__name__)


# Telegram Premium 赠送方案（基础价来自 config.PREMIUM_GIFT_PRICES，单一数据源）
_PREMIUM_PLANS = [
    {"months": m, "stars": s} for m, s in sorted(PREMIUM_GIFT_PRICES.items())
]


# ==================== API 辅助函数 ====================

async def _get_star_transactions(bot, limit=50, offset=0):
    """调用 Telegram API 获取星星交易记录"""
    try:
        result = (await bot.get_star_transactions(limit=limit, offset=offset)).to_dict()
        return result.get('transactions', []), result.get('pending_transactions', [])
    except TelegramError as error:
        logger.warning('getStarTransactions failed: %s', type(error).__name__)
        return None, None


async def _get_bot_star_balance(bot):
    try:
        return (await bot.get_my_star_balance()).amount
    except TelegramError:
        return None


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
            InlineKeyboardButton("💎 赠送 Premium", callback_data="stars_premium_list"),
        ],
        [
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

    try:
        gifts = (await bot.get_available_gifts()).to_dict().get('gifts', [])
        _gifts_cache = gifts
        _gifts_cache_time = now
        return gifts
    except TelegramError as error:
        logger.warning('getAvailableGifts failed: %s', type(error).__name__)
        return None


async def _get_recent_paying_users(bot, limit=8):
    """获取近期付费用户列表（礼物 / Premium 赠送选择接收者时共用）"""
    try:
        transactions, _ = await _get_star_transactions(bot, limit=50)
        if not transactions:
            return []
        recent = []
        seen = set()
        for tx in transactions:
            if tx.get("amount", 0) > 0:
                source = tx.get("source", {})
                if source.get("type") == "user":
                    uid = source.get("user_id")
                    if uid and uid not in seen:
                        seen.add(uid)
                        recent.append({
                            "user_id": uid,
                            "first_name": source.get("first_name", f"用户{uid}"),
                        })
                        if len(recent) >= limit:
                            break
        return recent
    except Exception:
        return []


def _build_recipient_keyboard(recent_users, user_cb_prefix, input_cb, back_cb, back_text="⬅️ 返回"):
    """构建接收者选择键盘（礼物与 Premium 共用）"""
    keyboard = []
    if recent_users:
        row = []
        for u in recent_users:
            row.append(InlineKeyboardButton(
                f"👤 {u['first_name'][:10]}",
                callback_data=f"{user_cb_prefix}|{u['user_id']}"
            ))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
    keyboard.append([InlineKeyboardButton("✏️ 手动输入用户 ID", callback_data=input_cb)])
    keyboard.append([InlineKeyboardButton(back_text, callback_data=back_cb)])
    return InlineKeyboardMarkup(keyboard)


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
    # 是否支持升级为 unique：Gift 对象有 upgrade_star_count 字段才可升级
    upgradeable = "upgrade_star_count" in gift_info

    # 存储到 user_data
    context.user_data['selected_gift_id'] = gift_id
    context.user_data['selected_gift_name'] = gift_name
    context.user_data['selected_gift_stars'] = star_count
    context.user_data['selected_gift_upgradeable'] = upgradeable

    # 获取近期付费用户
    recent_users = await _get_recent_paying_users(context.bot)

    text = (
        f"🎁 <b>已选择礼物：</b>{escape(gift_name)}（{star_count}⭐）\n\n"
        f"请选择接收礼物的用户："
    )
    if recent_users:
        text += "\n\n<b>近期付费用户：</b>\n"

    keyboard = _build_recipient_keyboard(
        recent_users,
        user_cb_prefix="stars_gift_user",
        input_cb="stars_gift_input",
        back_cb="stars_gift_list|0",
        back_text="⬅️ 返回礼物列表",
    )
    # 在「手动输入」和「返回」之间插入一行：生成领取链接
    rows = keyboard.inline_keyboard
    link_row = [InlineKeyboardButton("🔗 生成领取链接", callback_data="stars_gift_link")]
    # 插在倒数第二行（返回按钮之前）
    rows.insert(-1, link_row)
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows))


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
    context.user_data['gift_flow'] = 'gift'

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
    """执行礼物发送（使用 PTB 原生 bot.send_gift）"""
    gift_id = context.user_data.get('selected_gift_id', '')
    gift_name = context.user_data.get('selected_gift_name', '未知礼物')
    star_count = context.user_data.get('selected_gift_stars', 0)
    upgradeable = context.user_data.get('selected_gift_upgradeable', False)

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

    # 仅当礼物支持升级为 unique 时才传 pay_for_upgrade=True，
    # 否则 Telegram 会报 STARGIFT_UPGRADE_UNAVAILABLE。
    send_kwargs = {"gift_id": gift_id, "user_id": target_user_id}
    if upgradeable:
        send_kwargs["pay_for_upgrade"] = True

    try:
        await context.bot.send_gift(**send_kwargs)
        text = (
            f"✅ <b>礼物发送成功！</b>\n\n"
            f"🎁 <b>礼物：</b>{escape(gift_name)}\n"
            f"⭐ <b>花费：</b>{star_count} 星星"
            + ("（已含升级为 unique 的费用）" if upgradeable else "")
            + f"\n👤 <b>接收者 ID：</b><a href=\"tg://user?id={target_user_id}\">{target_user_id}</a>"
        )
        logger.info("管理员发送礼物 %s (%d⭐, upgrade=%s) 给用户 %s",
                    gift_name, star_count, upgradeable, target_user_id)
    except TelegramError as e:
        text = (
            f"❌ <b>礼物发送失败</b>\n\n"
            f"🎁 {escape(gift_name)} -> 用户 {target_user_id}\n"
            f"错误：{escape(str(e)[:200])}"
        )
        logger.error("发送礼物失败: %s", e)

    # 清理 user_data（gift_flow 与 waiting_gift_user_id 为礼物/Premium 共用字段）
    for key in ('selected_gift_id', 'selected_gift_name', 'selected_gift_stars',
                'selected_gift_upgradeable', 'waiting_gift_user_id', 'gift_flow'):
        context.user_data.pop(key, None)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎁 继续发送", callback_data="stars_gift_list|0"),
        InlineKeyboardButton("⬅️ 返回", callback_data="stars_refresh"),
    ]])
    await send_msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


async def stars_gift_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """取消礼物发送"""
    query = update.callback_query
    await query.answer("已取消")

    for key in ('selected_gift_id', 'selected_gift_name', 'selected_gift_stars',
                'selected_gift_upgradeable', 'waiting_gift_user_id', 'gift_flow'):
        context.user_data.pop(key, None)

    await stars_refresh_callback(update, context)


async def handle_gift_user_id_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理手动输入的用户 ID（礼物 / Premium 赠送共用）"""
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
    if context.user_data.get('gift_flow') == 'premium':
        await _do_send_premium(update, context, target_user_id)
    else:
        await _do_send_gift(update, context, target_user_id)


# ==================== 赠送 Premium 会员 ====================

async def stars_premium_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """显示 Premium 会员赠送方案"""
    query = update.callback_query
    await query.answer()

    balance = await _get_bot_star_balance(context.bot)
    text = (
        "💎 <b>赠送 Telegram Premium 会员</b>\n\n"
        "用 Bot 的星星余额为指定用户开通 Premium 会员：\n\n"
    )
    if balance is not None:
        text += f"💰 <b>当前星星余额：</b>{balance:,} ⭐\n\n"

    keyboard = []
    for plan in _PREMIUM_PLANS:
        months, stars = plan["months"], plan["stars"]
        affordable = (balance is None) or (balance >= stars)
        if affordable:
            label = f"⏱ {months} 个月  -  {stars}⭐"
            cb = f"stars_premium_sel|{months}"
        else:
            label = f"🚫 {months} 个月  -  {stars}⭐（余额不足）"
            cb = f"stars_premium_nostars|{months}"
        keyboard.append([InlineKeyboardButton(label, callback_data=cb)])

    keyboard.append([InlineKeyboardButton("⬅️ 返回星星资产", callback_data="stars_refresh")])
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def stars_premium_nostars_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """余额不足时的提示"""
    query = update.callback_query
    parts = query.data.split("|")
    months = parts[1] if len(parts) > 1 else "?"
    await query.answer(f"❌ 星星余额不足，无法赠送 {months} 个月 Premium", show_alert=True)


async def stars_premium_sel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """选择 Premium 时长后，选择接收者"""
    query = update.callback_query
    parts = query.data.split("|")
    try:
        months = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, TypeError):
        months = 0
    plan = next((p for p in _PREMIUM_PLANS if p["months"] == months), None)
    if not plan:
        await query.answer("❌ 方案信息错误", show_alert=True)
        return
    await query.answer()

    stars = plan["stars"]
    context.user_data['selected_premium_months'] = months
    context.user_data['selected_premium_stars'] = stars
    context.user_data['gift_flow'] = 'premium'

    recent_users = await _get_recent_paying_users(context.bot)
    text = (
        f"💎 <b>赠送 Premium 会员</b>（{months} 个月 / {stars}⭐）\n\n"
        f"请选择接收者："
    )
    if recent_users:
        text += "\n\n<b>近期付费用户：</b>\n"

    keyboard = _build_recipient_keyboard(
        recent_users,
        user_cb_prefix="stars_premium_user",
        input_cb="stars_premium_input",
        back_cb="stars_premium_list",
        back_text="⬅️ 返回方案选择",
    )
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


async def stars_premium_input_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """手动输入用户 ID 赠送 Premium"""
    query = update.callback_query
    await query.answer()

    months = context.user_data.get('selected_premium_months', 0)
    stars = context.user_data.get('selected_premium_stars', 0)
    if not months:
        await query.answer("❌ 请先选择赠送方案", show_alert=True)
        return

    context.user_data['waiting_gift_user_id'] = True
    context.user_data['gift_flow'] = 'premium'

    text = (
        f"💎 <b>赠送 Premium 会员：</b>{months} 个月（{stars}⭐）\n\n"
        f"请在下方消息中输入接收者的 <b>用户 ID</b>（数字）\n\n"
        f"💡 提示：可以让用户发送 /start 给主 Bot，从日志中获取其用户 ID。"
    )
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("❌ 取消", callback_data="stars_premium_cancel")]])
    await query.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


async def stars_premium_user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """选择用户后赠送 Premium"""
    query = update.callback_query
    parts = query.data.split("|")
    try:
        target_user_id = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, TypeError):
        target_user_id = 0
    if not target_user_id:
        await query.answer("❌ 用户信息错误", show_alert=True)
        return
    await query.answer()
    await _do_send_premium(update, context, target_user_id, query=query)


async def _do_send_premium(update, context, target_user_id, query=None):
    """执行 Premium 会员赠送（使用 PTB 原生 bot.gift_premium_subscription）"""
    months = context.user_data.get('selected_premium_months', 0)
    stars = context.user_data.get('selected_premium_stars', 0)

    if not months or not stars:
        msg = "❌ 请先选择赠送方案"
        if query:
            await query.answer(msg, show_alert=True)
        else:
            await _retry_send(update.message.reply_text, msg)
        return

    send_msg = await (query.message if query else update.message).reply_text(
        f"⏳ 正在赠送 Premium 会员（{months} 个月）给用户 {target_user_id}..."
    )

    try:
        await context.bot.gift_premium_subscription(
            user_id=target_user_id,
            month_count=months,
            star_count=stars,
        )
        text = (
            f"✅ <b>Premium 会员赠送成功！</b>\n\n"
            f"💎 <b>时长：</b>{months} 个月\n"
            f"⭐ <b>花费：</b>{stars} 星星\n"
            f"👤 <b>接收者 ID：</b><a href=\"tg://user?id={target_user_id}\">{target_user_id}</a>"
        )
        logger.info("管理员赠送 Premium %d 个月 (%d⭐) 给用户 %s", months, stars, target_user_id)
    except TelegramError as e:
        text = (
            f"❌ <b>Premium 赠送失败</b>\n\n"
            f"💎 {months} 个月 -> 用户 {target_user_id}\n"
            f"错误：{escape(str(e)[:200])}"
        )
        logger.error("赠送 Premium 失败: %s", e)

    for key in ('selected_premium_months', 'selected_premium_stars',
                'waiting_gift_user_id', 'gift_flow'):
        context.user_data.pop(key, None)

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("💎 继续赠送", callback_data="stars_premium_list"),
        InlineKeyboardButton("⬅️ 返回", callback_data="stars_refresh"),
    ]])
    await send_msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


async def stars_premium_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """取消 Premium 赠送（不单独 answer，交由 list 回调统一 answer，避免双重应答）"""
    for key in ('selected_premium_months', 'selected_premium_stars',
                'waiting_gift_user_id', 'gift_flow'):
        context.user_data.pop(key, None)
    await stars_premium_list_callback(update, context)


# ==================== 礼物领取链接（一码一人） ====================

async def stars_gift_link_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """管理员：为当前选中的礼物生成一个一次性领取链接"""
    query = update.callback_query
    await query.answer()

    gift_id = context.user_data.get('selected_gift_id', '')
    gift_name = context.user_data.get('selected_gift_name', '未知礼物')
    star_count = context.user_data.get('selected_gift_stars', 0)
    upgradeable = context.user_data.get('selected_gift_upgradeable', False)

    if not gift_id:
        await query.answer("❌ 请先选择礼物", show_alert=True)
        return

    # 星星余额预检：避免生成后无人能领（Bot 没钱发）
    balance = await _get_bot_star_balance(context.bot)
    if balance is not None and balance < star_count:
        await query.edit_message_text(
            f"❌ <b>余额不足，无法生成领取码</b>\n\n"
            f"🎁 礼物价格：{star_count}⭐\n"
            f"💰 当前余额：{balance}⭐\n\n"
            f"请先充值 Bot 星星余额。",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ 返回", callback_data="stars_gift_list|0"),
            ]]),
        )
        return

    # 生成领取码
    from db.gifts import create_claim
    claim = await create_claim(
        gift_id=gift_id,
        gift_name=gift_name,
        star_count=star_count,
        upgradeable=upgradeable,
        created_by=update.effective_user.id,
    )
    if not claim:
        await query.message.reply_text("❌ 生成领取码失败，请重试。")
        return

    # 拼接 t.me 链接：用主 Bot 用户名（领取在主 Bot 完成）
    bot_username = context.bot.username
    link = f"https://t.me/{bot_username}?start={claim['code']}"

    text = (
        f"🔗 <b>礼物领取链接已生成</b>\n\n"
        f"🎁 <b>礼物：</b>{escape(gift_name)}（{star_count}⭐）\n"
        f"📋 <b>状态：</b>待领取（一码一人，先到先得）\n\n"
        f"<b>领取链接：</b>\n<code>{link}</code>\n\n"
        f"💡 任何人点此链接打开本 Bot 即可领取，领完后链接自动失效。"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🎁 再生成一个", callback_data="stars_gift_link")],
        [InlineKeyboardButton("⬅️ 返回礼物列表", callback_data="stars_gift_list|0")],
    ])
    await query.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def handle_gift_claim(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str) -> None:
    """处理 /start claim_xxx：任意用户点击领取链接后的领取流程。

    流程：查码 → 原子认领（行锁防并发）→ 真正发礼物 → 失败则回滚状态。
    """
    user_id = update.effective_user.id
    from db.gifts import get_claim, try_claim

    # 1. 查码是否存在
    claim = await get_claim(code)
    if not claim:
        await _retry_send(update.message.reply_text,
            "❌ <b>领取链接无效</b>\n\n这个领取码不存在，可能链接已损坏。",
            parse_mode="HTML")
        return

    # 2. 已领取 → 提示来晚了
    if claim['status'] == 'claimed':
        claimed_by = claim.get('claimant_user_id')
        is_self = (claimed_by == user_id)
        if is_self:
            text = (
                f"ℹ️ <b>你已经领取过这个礼物了</b>\n\n"
                f"🎁 {escape(claim['gift_name'])}\n"
                f"领取时间：{claim.get('claimed_at', '未知')}"
            )
        else:
            text = "❌ <b>这个礼物已被领取</b>\n\n你来晚了一步，链接已失效。"
        await _retry_send(update.message.reply_text, text, parse_mode="HTML")
        return

    # 3. 状态异常（如已过期）
    if claim['status'] != 'pending':
        await _retry_send(update.message.reply_text,
            "❌ <b>这个领取链接已失效</b>", parse_mode="HTML")
        return

    # 4. 原子认领（行锁防并发，try_claim 内部保证 status=pending 才认领）
    claimed = await try_claim(code, user_id)
    if not claimed:
        # 并发竞争：被人抢先一步
        await _retry_send(update.message.reply_text,
            "❌ <b>手慢了！</b>\n\n这个礼物刚被别人领走，下次早点哦。",
            parse_mode="HTML")
        return

    # 5. 认领成功 → 真正发送礼物
    gift_id = claimed['gift_id']
    gift_name = claimed['gift_name']
    star_count = claimed['star_count']
    upgradeable = bool(claimed.get('upgradeable', 0))

    send_kwargs = {"gift_id": gift_id, "user_id": user_id}
    if upgradeable:
        send_kwargs["pay_for_upgrade"] = True

    try:
        await context.bot.send_gift(**send_kwargs)
        logger.info("领取码 %s 发货成功：礼物 %s 给用户 %s", code, gift_name, user_id)
        await _retry_send(update.message.reply_text,
            f"🎉 <b>礼物领取成功！</b>\n\n"
            f"🎁 <b>礼物：</b>{escape(gift_name)}\n"
            f"⭐ <b>价值：</b>{star_count} 星星\n\n"
            f"已发放到你的账户，可在 Telegram 个人主页 → 礼物中查看。",
            parse_mode="HTML")
    except TelegramError as e:
        # 发货失败：回滚状态为 pending，让别人还能领（或管理员处理）
        from db.gifts import GiftClaim
        from db.core import get_session
        from sqlalchemy import update as sa_update
        async with get_session() as session:
            await session.execute(
                sa_update(GiftClaim)
                .where(GiftClaim.code == code)
                .values(status='pending', claimant_user_id=None, claimed_at=None)
            )
            await session.commit()
        logger.error("领取码 %s 发货失败（已回滚）: %s", code, e)
        await _retry_send(update.message.reply_text,
            f"❌ <b>礼物领取失败</b>\n\n"
            f"错误：{escape(str(e)[:150])}\n\n"
            f"领取码已退回，可稍后重试或联系管理员。",
            parse_mode="HTML")


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
    elif data == "stars_gift_link":
        await stars_gift_link_callback(update, context)
    elif data.startswith("stars_gift_user|"):
        await stars_gift_user_callback(update, context)
    elif data == "stars_gift_cancel":
        await stars_gift_cancel_callback(update, context)
    elif data == "stars_premium_list":
        await stars_premium_list_callback(update, context)
    elif data.startswith("stars_premium_sel|"):
        await stars_premium_sel_callback(update, context)
    elif data.startswith("stars_premium_user|"):
        await stars_premium_user_callback(update, context)
    elif data.startswith("stars_premium_nostars|"):
        await stars_premium_nostars_callback(update, context)
    elif data == "stars_premium_input":
        await stars_premium_input_callback(update, context)
    elif data == "stars_premium_cancel":
        await stars_premium_cancel_callback(update, context)
    else:
        await query.answer("未知操作", show_alert=True)