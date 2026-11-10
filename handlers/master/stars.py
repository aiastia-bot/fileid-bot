"""VIP 星星支付处理器 - Telegram Stars 支付 + VIP 管理"""
import json
import logging
import time
from datetime import datetime
from senders import _retry_send

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import ContextTypes

from config import (VIP_PLANS, VIP_FEATURES, VIP_EXPIRE_NOTICE_DAYS,
                    PREMIUM_GIFT_PRICES, PREMIUM_USER_MARKUP, BASIC_LEVEL)
from db.vip import (
    get_or_create_user, get_user_vip_info, get_user_vip_level, get_max_bots_for_user,
    update_user_vip, record_star_payment, get_payment_history,
    get_active_bots_by_owner, get_active_bots_count_by_owner,
    pause_user_bot, resume_user_bot, get_paused_bots_by_owner,
    is_free_registration_closed,
)
from db.migrations import create_migration, claim_migration
from handlers.master._utils import get_bot_manager, escape

logger = logging.getLogger(__name__)


# ==================== /vip 命令 ====================

async def vip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/vip 查看 VIP 状态和购买选项"""
    user_id = update.effective_user.id
    vip_info = await get_user_vip_info(user_id)
    from services.membership import get_plans
    plans = await get_plans()
    free_closed = await is_free_registration_closed()

    # 构建状态显示
    if vip_info['vip_level'] == 0:
        status_text = "🎫 <b>当前状态：</b>免费用户" + ("（老用户保留）" if free_closed else "")
        expire_text = ""
    else:
        if vip_info['is_active']:
            status_text = f"⭐ <b>当前状态：</b>{escape(vip_info['vip_name'])}"
            expire_text = f"\n📅 <b>到期时间：</b>{vip_info['vip_expire_at']}\n⏳ <b>剩余天数：</b>{vip_info['remaining_days']} 天"
        else:
            status_text = f"⚠️ <b>当前状态：</b>{escape(vip_info['vip_name'])}（已过期）"
            expire_text = ""

    bots_count = await get_active_bots_count_by_owner(user_id)
    max_bots = vip_info['max_bots']

    text = (
        f"🌟 <b>VIP 会员中心</b>\n\n"
        f"{status_text}\n"
        f"🤖 <b>Bot 数量：</b>{bots_count}/{max_bots}"
        f"{expire_text}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>VIP 等级对比</b>\n\n"
    )

    if free_closed:
        text += "ℹ️ 免费注册已停止，新用户可购买基础版使用。\n\n"

    # 显示各等级对比：免费关闭时用「基础版」替代「免费」入口
    display_levels = [0, BASIC_LEVEL, 1, 2, 3] if free_closed else [0, 1, 2, 3]

    for level in display_levels:
        plan = plans[level]
        # 拼接该等级的特权标注（目前只有转发保护）
        features = plan
        feature_tags = []
        if features.get('forward_mode'):
            feature_tags.append("🔒 含转发保护")
        feature_text = f"  {'  '.join(feature_tags)}" if feature_tags else ""

        current = "  ✅ 当前" if level == vip_info['vip_level'] and vip_info['is_active'] else ""

        if level == 0:
            text += f"  {escape(plan['name'])} — {plan['max_bots']} 个 Bot — 免费{feature_text}\n"
        elif level == BASIC_LEVEL:
            text += f"  💎 {escape(plan['name'])} — {plan['max_bots']} 个 Bot — 月付 {plan['monthly_price']}⭐ / 年付 {plan['yearly_price']}⭐{current}{feature_text}\n"
        else:
            stars = "⭐" * level
            monthly = plan['monthly_price']
            yearly = plan['yearly_price']
            text += f"  {stars} {escape(plan['name'])} — {plan['max_bots']} 个 Bot — 月付 {monthly}⭐ / 年付 {yearly}⭐{current}{feature_text}\n"

    text += (
        f"\n💡 年付享优惠（约 10 个月价格）\n"
        f"同等级续费叠加；更换等级从购买日起重新计算有效期。\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔒 <b>转发保护</b>（VIP 1+ 独有）\n"
        f"控制你 Bot 发送的图片/视频是否允许被接收者转发或保存。\n\n"
        f"<b>如何设置：</b>\n"
        f"1. 发送 /mybots 打开 Bot 列表\n"
        f"2. 点击对应 Bot 下方的「🔒 @botusername - 模式」按钮\n"
        f"3. 选择转发模式：\n"
        f"   ✅ 允许转发 — 默认，接收者可转发/保存\n"
        f"   🚫 禁止转发 — 接收者无法转发或保存\n"
        f"   👤 用户自选 — 由每位接收者自己决定\n"
        f"4. 设置即时生效，仅对新发送的媒体有效\n\n"
        f"<b>👤 用户自选模式下，接收者如何设置：</b>\n"
        f"接收者在跟 Bot 的私聊里发送 /settings 命令：\n"
        f"• 会看到当前保护状态和一个切换按钮\n"
        f"• 点按钮即可在自己这里 开启/关闭 保护\n"
        f"• 每位接收者独立设置，互不影响\n"
        f"（Bot 主人选了「允许/禁止」时，/settings 只显示规则，无切换按钮）"
    )

    # 构建购买按钮（免费关闭时增加基础版入口）
    buy_levels = [BASIC_LEVEL, 1, 2, 3] if free_closed else [1, 2, 3]
    keyboard = []

    for level in buy_levels:
        plan = plans[level]
        if not plan['enabled']:
            continue
        icon = "💎" if level == BASIC_LEVEL else "⭐" * level
        row_monthly = InlineKeyboardButton(
            f"{icon} {escape(plan['name'])} 月付 {plan['monthly_price']}⭐",
            callback_data=f"buy_vip|{level}|1"
        )
        row_yearly = InlineKeyboardButton(
            f"{icon} {escape(plan['name'])} 年付 {plan['yearly_price']}⭐",
            callback_data=f"buy_vip|{level}|12"
        )
        keyboard.append([row_monthly, row_yearly])

    # 底部按钮
    bottom_row = []
    if vip_info['vip_level'] > 0 and vip_info['is_active']:
        bottom_row.append(InlineKeyboardButton("📜 支付记录", callback_data="vip_history"))
        bottom_row.append(InlineKeyboardButton("🔄 迁移会员", callback_data="vip_migrate"))
    bottom_row = bottom_row or [InlineKeyboardButton("📜 支付记录", callback_data="vip_history")]
    keyboard.append(bottom_row)

    reply_markup = InlineKeyboardMarkup(keyboard)

    await _retry_send(update.message.reply_text, text, parse_mode="HTML", reply_markup=reply_markup)


# ==================== 购买回调 ====================

async def buy_vip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理购买VIP按钮回调"""
    query = update.callback_query
    await query.answer()

    data = query.data  # buy_vip|level|months
    parts = data.split("|")
    if len(parts) != 3:
        await query.answer("❌ 参数错误", show_alert=True)
        return

    try:
        level = int(parts[1])
        months = int(parts[2])
    except (ValueError, IndexError):
        await query.answer("❌ 参数错误", show_alert=True)
        return

    from services.membership import get_plan
    plan = await get_plan(level)
    if level not in (1, 2, 3, BASIC_LEVEL):
        await query.answer("❌ 无效的VIP等级", show_alert=True)
        return

    # 确定价格
    if months == 12:
        price = plan['yearly_price']
        period_text = "年"
    elif months == 1:
        price = plan['monthly_price']
        period_text = "月"
    else:
        await query.answer("❌ 无效的时长", show_alert=True)
        return

    user_id = update.effective_user.id
    time_label = period_text
    title = f"{escape(plan['name'])} {time_label}付订阅"
    description = (
        f"购买 {escape(plan['name'])} {time_label}付订阅\n"
        f"可创建最多 {plan['max_bots']} 个 Bot\n"
        f"有效期 {30 * months} 天"
    )

    # 生成 payload
    from services.payments import create_order
    try:
        order = await create_order(user_id, 'vip', months, level)
    except ValueError as exc:
        await _retry_send(query.message.reply_text, str(exc))
        return
    payload = order['payload']
    price = order['amount']

    # 发送 Invoice（Telegram Stars 使用 XTR 货币）
    prices = [LabeledPrice(label=title, amount=price)]

    try:
        await context.bot.send_invoice(
            chat_id=user_id,
            title=title,
            description=description,
            payload=payload,
            provider_token="",  # Stars 支付留空
            currency="XTR",
            prices=prices,
        )
        await _retry_send(query.message.reply_text, 
            f"💳 正在发起支付...\n"
            f"⭐ {escape(plan['name'])} {time_label}付 — {price} 星星",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error("发送 Invoice 失败: %s", e)
        await _retry_send(query.message.reply_text, 
            f"❌ 发起支付失败：{escape(str(e)[:100])}\n请稍后重试。"
        )


# ==================== 支付历史回调 ====================

async def vip_history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """查看支付历史"""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    from services.payments import list_orders
    history = await list_orders(user_id, limit=10)
    known_payloads = {p['payload'] for p in history}
    legacy = await get_payment_history(user_id, limit=10)
    history = (history + [p for p in legacy if p['payload'] not in known_payloads])[:10]

    if not history:
        await _retry_send(query.message.reply_text, "📜 暂无支付记录。")
        return

    text = "📜 <b>支付记录</b>\n\n"
    for i, p in enumerate(history, 1):
        from services.membership import get_plans
        plan = (await get_plans()).get(p.get('level', p.get('vip_level', 0)), {})
        name = 'Telegram Premium' if p.get('product') == 'premium' else plan.get('name', '会员')
        months_text = "年付" if p['months'] == 12 else "月付"
        text += (
            f"{i}. {name} {months_text} — {p['amount']}⭐\n"
            f"   📅 {p['created_at']} | 状态：{p.get('status', '历史订单')}\n"
        )

    await _retry_send(query.message.reply_text, text, parse_mode="HTML")


# ==================== PreCheckout 处理 ====================

async def pre_checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from services.payments import checkout_error
    query = update.pre_checkout_query
    error = await checkout_error(query.invoice_payload, query.from_user.id, query.total_amount, query.currency)
    await query.answer(ok=error is None, error_message=error)


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from services.payments import receive_payment, fulfill_order
    payment = update.message.successful_payment
    try:
        status = await receive_payment(payment.invoice_payload, update.effective_user.id,
            payment.total_amount, payment.currency, payment.telegram_payment_charge_id)
    except ValueError as exc:
        await _retry_send(update.message.reply_text, str(exc))
        return
    if status in ('received', 'retry'):
        # Persisted receipt is sufficient for webhook acknowledgement. Processing
        # runs independently and is resumed by the periodic order worker.
        context.application.create_task(fulfill_order(context.bot, payment.invoice_payload))
    await _retry_send(update.message.reply_text,
        '✅ 已收到支付记录，正在核验和发放。可在 /vip 的支付记录中查看进度。'
        if status not in ('completed', 'refunded') else '该笔订单已处理，无需重复支付。')


# ==================== VIP 过期处理 ====================

async def handle_expired_vips() -> None:
    from db.vip import get_expired_users, get_user_vip_level
    from services.notices import enqueue
    from services.entitlements import reconcile_user
    for user in await get_expired_users():
        await enqueue(f"expired:{user['user_id']}:{user['vip_expire_at']}", user['user_id'],
            '⚠️ 你的会员已到期，Bot 运行额度已按当前方案调整。续费后可自动恢复因会员到期暂停的 Bot。使用 /vip 查看。')
        await get_user_vip_level(user['user_id'])
        await reconcile_user(user['user_id'])


async def send_expire_reminders() -> None:
    from db.vip import get_expiring_users
    from services.notices import enqueue
    for user in await get_expiring_users(days=VIP_EXPIRE_NOTICE_DAYS):
        await enqueue(f"reminder:{user['user_id']}:{user['vip_expire_at']}", user['user_id'],
            f"⏰ 你的会员将在 {user['vip_expire_at']} 到期。使用 /vip 查看方案和续费。")


# ==================== 内部辅助函数 ====================

async def _pause_excess_bots(user_id: int, keep=None) -> list:
    from services.entitlements import reconcile_user
    await reconcile_user(user_id)
    return []


async def _try_resume_paused_bots(user_id: int, new_level: int) -> None:
    from services.entitlements import reconcile_user
    await reconcile_user(user_id)


# ==================== 会员迁移 ====================

async def vip_migrate_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理「🔄 迁移会员」按钮：生成一次性迁移码"""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    user = await get_or_create_user(user_id)

    if not user or (user.get('vip_level') or 0) <= 0:
        await query.answer("当前没有可迁移的会员", show_alert=True)
        return

    expire_at = user.get('vip_expire_at')
    if expire_at:
        try:
            if datetime.now() > datetime.strptime(expire_at, "%Y-%m-%d %H:%M:%S"):
                await query.answer("会员已过期，无法迁移", show_alert=True)
                return
        except ValueError:
            pass

    if user.get('vip_migrated'):
        await query.answer("该会员是通过迁移获得的，不能再迁移", show_alert=True)
        return

    mig = await create_migration(user_id, user['vip_level'], expire_at)
    if not mig:
        await _retry_send(query.message.reply_text, "❌ 生成迁移码失败，请稍后重试。")
        return

    from services.membership import get_plan
    plan = await get_plan(user['vip_level'])
    link = f"https://t.me/{context.bot.username}?start={mig['code']}"
    text = (
        f"🔄 <b>会员迁移码已生成</b>\n\n"
        f"👑 当前会员：{plan.get('name', 'VIP')}\n"
        f"📅 到期时间：{expire_at or '永久'}\n\n"
        f"🔑 <b>迁移码：</b><code>{escape(mig['code'])}</code>\n\n"
        f"<b>使用方法</b>：\n"
        f"1️⃣ 切换到要接收会员的新账号\n"
        f"2️⃣ 点击链接直接领取：\n{link}\n"
        f"（或让新账号向本 Bot 发送 <code>/start {escape(mig['code'])}</code>）\n\n"
        f"⚠️ <b>注意</b>：\n"
        f"• 迁移码有效期与会员有效期一致，过期失效\n"
        f"• 只能迁移一次，领取后新账号不能再继续迁移\n"
        f"• 迁移后当前账号会员清零，超出免费额度的 Bot 会被暂停\n"
        f"• 重新生成迁移码会使旧码作废"
    )
    await _retry_send(query.message.reply_text, text, parse_mode="HTML",
                      disable_web_page_preview=True)


async def handle_vip_migration_claim(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                     code: str) -> None:
    """处理 /start mig_xxx：领取会员迁移码"""
    user_id = update.effective_user.id
    status, info = await claim_migration(code, user_id)

    if status == 'ok':
        from services.membership import get_plan
        plan = await get_plan(info['vip_level'])
        await _retry_send(update.message.reply_text,
            f"🎉 <b>会员迁移成功！</b>\n\n"
            f"👑 会员等级：{plan.get('name', 'VIP')}\n"
            f"📅 到期时间：{info['vip_expire_at'] or '永久'}\n"
            f"🤖 可创建 Bot 数：{plan.get('max_bots', VIP_PLANS[0]['max_bots'])} 个\n\n"
            f"⚠️ 该会员仅可迁移一次（当前账号已锁定，不可再迁出）。",
            parse_mode="HTML"
        )
        # 新账号：恢复因额度暂停的 Bot
        await _try_resume_paused_bots(user_id, info['vip_level'])
        # 旧账号：降为免费后暂停超额 Bot
        await _pause_excess_bots(info['from_user_id'])
        # 通知旧账号
        try:
            import __main__
            master_app = getattr(__main__, 'master_app', None)
            if master_app:
                await _retry_send(master_app.bot.send_message,
                    chat_id=info['from_user_id'],
                    text=(
                        f"📤 <b>会员已迁移</b>\n\n"
                        f"你的 {plan.get('name', 'VIP')} 已成功迁移到新账号。\n"
                        f"当前账号已恢复为免费用户。"
                    ),
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.error("通知迁移源用户 %s 失败: %s", info['from_user_id'], e)
        return

    error_text = {
        'invalid': "❌ 迁移码无效或已被使用。",
        'expired': "❌ 该迁移码对应的会员已过期，迁移码已失效。",
        'self': "❌ 不能迁移到自己的账号。",
        'target_has_vip': (
            "❌ 当前账号已有有效会员，无法接收迁移的会员。\n\n"
            "💡 请先等待当前会员到期后再领取。"
        ),
    }.get(status, "❌ 迁移失败，请稍后重试。")
    await _retry_send(update.message.reply_text, error_text, parse_mode="HTML")


# ==================== 回调路由器 ====================

async def vip_callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """VIP 相关回调路由"""
    query = update.callback_query
    data = query.data

    if data.startswith("buy_vip|"):
        await buy_vip_callback(update, context)
    elif data == "vip_history":
        await vip_history_callback(update, context)
    elif data == "vip_migrate":
        await vip_migrate_callback(update, context)
    else:
        await query.answer("未知操作", show_alert=True)
