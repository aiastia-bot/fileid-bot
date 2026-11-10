"""用户购买 Telegram Premium 会员 - 用 ✨ 星星支付，Bot 赠送官方 Premium 订阅。

价格 = 官方基础价（PREMIUM_GIFT_PRICES）+ 服务费（PREMIUM_USER_MARKUP）。
Bot 收到用户支付的星星后，调用 giftPremiumSubscription 为用户开通 Premium，
成本为基础价，服务费部分为 Bot 收益。若开通失败则自动退款。
"""
import logging
import time
from senders import _retry_send

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from config import PREMIUM_GIFT_PRICES, PREMIUM_USER_MARKUP
from db.vip import record_star_payment
from handlers.master._utils import escape
# 复用 VIP 支付的二次验证逻辑（stars.py 不在模块级反向 import 本模块，无循环依赖）


logger = logging.getLogger(__name__)


def _premium_user_price(months: int) -> int:
    """用户购买价 = 官方基础价 + 服务费"""
    return PREMIUM_GIFT_PRICES[months] + PREMIUM_USER_MARKUP


async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/premium 查看 Telegram Premium 会员方案并购买"""
    markup = PREMIUM_USER_MARKUP
    text = (
        "💎 <b>购买 Telegram Premium 会员</b>\n\n"
        "用 ✨ 星星支付，Bot 将为你开通官方 Telegram Premium 会员。\n"
        f"价格 = 官方基础价 + {markup}⭐ 服务费。\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
    )

    keyboard = []
    for months in sorted(PREMIUM_GIFT_PRICES.keys()):
        base = PREMIUM_GIFT_PRICES[months]
        price = base + markup
        text += f"⏱ <b>{months} 个月</b>：{price}⭐（含 {markup}⭐ 服务费）\n"
        keyboard.append([InlineKeyboardButton(
            f"💎 {months} 个月  {price}⭐",
            callback_data=f"buy_premium|{months}",
        )])

    text += (
        "\n💡 Premium 会员享受：无广告、4G 文件上传、专属贴纸/表情、更快的下载速度等。"
    )

    await _retry_send(update.message.reply_text, text, parse_mode="HTML",
                      reply_markup=InlineKeyboardMarkup(keyboard))


async def buy_premium_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """发起 Premium 购买支付（发送 Invoice）"""
    query = update.callback_query
    parts = query.data.split("|")
    if len(parts) != 2:
        await query.answer("❌ 参数错误", show_alert=True)
        return

    try:
        months = int(parts[1])
    except ValueError:
        await query.answer("❌ 参数错误", show_alert=True)
        return

    if months not in PREMIUM_GIFT_PRICES:
        await query.answer("❌ 无效的方案", show_alert=True)
        return

    await query.answer()

    user_id = update.effective_user.id
    price = _premium_user_price(months)
    base = PREMIUM_GIFT_PRICES[months]

    title = f"Telegram Premium {months} 个月"
    description = (
        f"购买 Telegram Premium 会员 {months} 个月\n"
        f"由 Bot 赠送官方 Premium 订阅\n"
        f"基础价 {base}⭐ + 服务费 {PREMIUM_USER_MARKUP}⭐ = {price}⭐"
    )
    # payload: premium_{months}_{user_id}_{timestamp}
    from services.payments import create_order
    order = await create_order(user_id, 'premium', months)
    payload = order['payload']
    prices = [LabeledPrice(label=title, amount=price)]

    try:
        await context.bot.send_invoice(
            chat_id=user_id,
            title=title,
            description=description,
            payload=payload,
            provider_token="",  # Telegram Stars 支付留空
            currency="XTR",
            prices=prices,
        )
    except TelegramError as e:
        logger.error("发送 Premium Invoice 失败: %s", e)
        await _retry_send(query.message.reply_text,
                          f"❌ 发起支付失败：{escape(str(e)[:100])}\n请稍后重试。")


async def handle_premium_payment_success(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from handlers.master.stars import successful_payment_handler
    await successful_payment_handler(update, context)
