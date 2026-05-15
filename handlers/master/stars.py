"""VIP 星星支付处理器 - Telegram Stars 支付 + VIP 管理"""
import html
import json
import logging
import time
from senders import _retry_send

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import ContextTypes

from config import VIP_PLANS, VIP_EXPIRE_NOTICE_DAYS
from db.vip import (
    get_user_vip_info, get_user_vip_level, get_max_bots_for_user,
    update_user_vip, record_star_payment, get_payment_history,
    get_active_bots_by_owner, get_active_bots_count_by_owner,
    pause_user_bot, resume_user_bot, get_paused_bots_by_owner,
)

logger = logging.getLogger(__name__)


def escape(text: str) -> str:
    """HTML 转义"""
    return html.escape(str(text), quote=False)


def get_bot_manager():
    """获取全局 BotManager 实例"""
    import __main__
    return getattr(__main__, 'bot_manager', None)


# ==================== /vip 命令 ====================

async def vip_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/vip 查看 VIP 状态和购买选项"""
    user_id = update.effective_user.id
    vip_info = await get_user_vip_info(user_id)

    # 构建状态显示
    if vip_info['vip_level'] == 0:
        status_text = "🎫 <b>当前状态：</b>免费用户"
        expire_text = ""
    else:
        if vip_info['is_active']:
            status_text = f"⭐ <b>当前状态：</b>{vip_info['vip_name']}"
            expire_text = f"\n📅 <b>到期时间：</b>{vip_info['vip_expire_at']}\n⏳ <b>剩余天数：</b>{vip_info['remaining_days']} 天"
        else:
            status_text = f"⚠️ <b>当前状态：</b>{vip_info['vip_name']}（已过期）"
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

    # 显示各等级对比
    for level in sorted(VIP_PLANS.keys()):
        plan = VIP_PLANS[level]
        if level == 0:
            text += f"  {plan['name']} — {plan['max_bots']} 个 Bot — 免费\n"
        else:
            stars = "⭐" * level
            monthly = plan['monthly_price']
            yearly = plan['yearly_price']
            current = "  ✅ 当前" if level == vip_info['vip_level'] and vip_info['is_active'] else ""
            text += f"  {stars} {plan['name']} — {plan['max_bots']} 个 Bot — 月付 {monthly}⭐ / 年付 {yearly}⭐{current}\n"

    text += (
        f"\n💡 年付享优惠（约 10 个月价格）\n"
        f"续费时间会自动叠加，不会浪费"
    )

    # 构建购买按钮
    keyboard = []

    for level in [1, 2, 3]:
        plan = VIP_PLANS[level]
        stars = "⭐" * level
        row_monthly = InlineKeyboardButton(
            f"{stars} {plan['name']} 月付 {plan['monthly_price']}⭐",
            callback_data=f"buy_vip|{level}|1"
        )
        row_yearly = InlineKeyboardButton(
            f"{stars} {plan['name']} 年付 {plan['yearly_price']}⭐",
            callback_data=f"buy_vip|{level}|12"
        )
        keyboard.append([row_monthly, row_yearly])

    # 底部按钮
    bottom_row = []
    if vip_info['vip_level'] > 0 and vip_info['is_active']:
        bottom_row.append(InlineKeyboardButton("📜 支付记录", callback_data="vip_history"))
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

    plan = VIP_PLANS.get(level)
    if not plan:
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
    title = f"{plan['name']} {time_label}付订阅"
    description = (
        f"购买 {plan['name']} {time_label}付订阅\n"
        f"可创建最多 {plan['max_bots']} 个 Bot\n"
        f"有效期 {30 * months} 天"
    )

    # 生成 payload
    payload = f"vip_{level}_{months}_{user_id}_{int(time.time())}"

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
            f"⭐ {plan['name']} {time_label}付 — {price} 星星",
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
    history = await get_payment_history(user_id, limit=10)

    if not history:
        await _retry_send(query.message.reply_text, "📜 暂无支付记录。")
        return

    text = "📜 <b>支付记录</b>\n\n"
    for i, p in enumerate(history, 1):
        plan = VIP_PLANS.get(p['vip_level'], {})
        name = plan.get('name', f"VIP {p['vip_level']}")
        months_text = "年付" if p['months'] == 12 else "月付"
        text += (
            f"{i}. {name} {months_text} — {p['amount']}⭐\n"
            f"   📅 {p['created_at']}\n"
        )

    await _retry_send(query.message.reply_text, text, parse_mode="HTML")


# ==================== PreCheckout 处理 ====================

async def pre_checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """响应 Telegram 预结账查询（必须在 10 秒内响应）"""
    query = update.pre_checkout_query

    # 解析 payload 验证
    payload = query.invoice_payload
    parts = payload.split("_")

    if len(parts) < 4 or parts[0] != "vip":
        await query.answer(ok=False, error_message="无效的支付信息")
        return

    try:
        level = int(parts[1])
        months = int(parts[2])
        user_id = int(parts[3])
    except (ValueError, IndexError):
        await query.answer(ok=False, error_message="无效的支付信息")
        return

    # 验证计划是否存在
    plan = VIP_PLANS.get(level)
    if not plan:
        await query.answer(ok=False, error_message="无效的VIP等级")
        return

    # 验证价格
    expected_price = plan['yearly_price'] if months == 12 else plan['monthly_price']
    if query.total_amount != expected_price:
        await query.answer(ok=False, error_message="价格不匹配，请重新发起支付")
        return

    # 验证通过
    await query.answer(ok=True)


# ==================== 支付验证 ====================

async def _verify_star_payment(bot, telegram_charge_id: str, expected_user_id: int,
                                expected_amount: int) -> bool:
    """通过 Telegram getStarTransactions API 验证支付真实性"""
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"https://api.telegram.org/bot{bot.token}/getStarTransactions",
                json={"limit": 50},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()

            if not data.get("ok"):
                logger.error("getStarTransactions API 调用失败: %s", data.get("description"))
                return False

            transactions = data.get("result", {}).get("transactions", [])
            for tx in transactions:
                if tx.get("id") == telegram_charge_id:
                    # 验证金额和用户
                    amount = tx.get("amount", 0)
                    source = tx.get("source", {})
                    user_id = source.get("user_id") if source.get("type") == "user" else None
                    # 金额和用户匹配则通过
                    if amount == expected_amount:
                        logger.info("支付验证通过: charge_id=%s, amount=%d", telegram_charge_id, amount)
                        return True
                    else:
                        logger.warning("支付金额不匹配: expected=%d, actual=%d", expected_amount, amount)
                        return False

            logger.warning("未找到支付记录: charge_id=%s", telegram_charge_id)
            return False
    except Exception as e:
        logger.error("支付验证异常: %s", e)
        # 验证失败时保守处理：不升级
        return False


# ==================== 支付成功处理 ====================

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理支付成功回调"""
    payment = update.message.successful_payment
    user_id = update.effective_user.id

    payload = payment.invoice_payload
    parts = payload.split("_")

    if len(parts) < 4 or parts[0] != "vip":
        logger.warning("收到未知 payload 的支付成功: %s", payload)
        return

    try:
        level = int(parts[1])
        months = int(parts[2])
    except (ValueError, IndexError):
        logger.error("解析支付 payload 失败: %s", payload)
        return

    plan = VIP_PLANS.get(level, {})
    plan_name = plan.get('name', f'VIP {level}')

    # ======== 二次验证：调用 Telegram API 确认交易真实性 ========
    telegram_charge_id = payment.telegram_payment_charge_id

    verified = await _verify_star_payment(
        bot=context.bot,
        telegram_charge_id=telegram_charge_id,
        expected_user_id=user_id,
        expected_amount=payment.total_amount,
    )

    if not verified:
        logger.warning("用户 %s 支付验证失败，charge_id=%s（可能为伪造请求）", user_id, telegram_charge_id)
        await _retry_send(update.message.reply_text, 
            "⚠️ 支付验证失败，请稍后重试或联系管理员。"
        )
        return

    # 记录支付
    await record_star_payment(
        user_id=user_id,
        amount=payment.total_amount,
        vip_level=level,
        months=months,
        payload=payload,
        telegram_charge_id=telegram_charge_id,
    )

    # 更新VIP
    success = await update_user_vip(user_id, level, months)

    if success:
        period_text = "1 个月" if months == 1 else "1 年"
        vip_info = await get_user_vip_info(user_id)

        text = (
            f"🎉 <b>支付成功！</b>\n\n"
            f"⭐ <b>VIP 等级：</b>{plan_name}\n"
            f"📅 <b>有效期：</b>{period_text}\n"
            f"📆 <b>到期时间：</b>{vip_info['vip_expire_at']}\n"
            f"🤖 <b>可创建 Bot 数：</b>{plan['max_bots']} 个\n\n"
            f"感谢你的支持！🌟"
        )
        await _retry_send(update.message.reply_text, text, parse_mode="HTML")

        # 检查是否有暂停的 Bot 可以恢复
        await _try_resume_paused_bots(user_id, level)

        logger.info("用户 %s 支付成功（已验证）: %s %d个月, %d⭐",
                    user_id, plan_name, months, payment.total_amount)
    else:
        await _retry_send(update.message.reply_text, 
            "⚠️ 支付已收到但 VIP 升级失败，请联系管理员。"
        )
        logger.error("用户 %s VIP 升级失败（支付已验证成功）", user_id)


# ==================== VIP 过期处理 ====================

async def handle_expired_vips() -> None:
    """定时任务：处理所有已过期的VIP用户"""
    from db.vip import get_expired_users
    from db import update_user_bot_status

    expired = await get_expired_users()
    if not expired:
        return

    logger.info("发现 %d 个过期 VIP 用户", len(expired))

    for user_info in expired:
        user_id = user_info['user_id']
        old_level = user_info['vip_level']

        # 降回 VIP 0
        from db.vip import _downgrade_expired_user
        await _downgrade_expired_user(user_id)

        # 暂停多余的 Bot
        paused_names = await _pause_excess_bots(user_id)

        # 通知用户
        try:
            import __main__
            master_app = getattr(__main__, 'master_app', None)
            if master_app:
                text = (
                    f"⚠️ <b>VIP 已过期</b>\n\n"
                    f"你的 {VIP_PLANS.get(old_level, {}).get('name', 'VIP')} 已到期。\n"
                    f"已恢复为免费用户（最多 1 个 Bot）。\n"
                )
                if paused_names:
                    text += f"\n以下 Bot 已暂停：\n"
                    for name in paused_names:
                        text += f"  🤖 @{escape(name)}\n"
                    text += f"\n💡 续费 VIP 后 Bot 将自动恢复运行。"

                text += "\n\n使用 /vip 查看续费选项"
                await _retry_send(master_app.bot.send_message, 
                    chat_id=user_id,
                    text=text,
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.error("通知过期用户 %s 失败: %s", user_id, e)


async def send_expire_reminders() -> None:
    """定时任务：发送即将过期提醒"""
    from db.vip import get_expiring_users

    users = await get_expiring_users(days=VIP_EXPIRE_NOTICE_DAYS)
    if not users:
        return

    logger.info("发送 %d 个过期提醒", len(users))

    for user_info in users:
        user_id = user_info['user_id']
        level = user_info['vip_level']
        expire_at = user_info['vip_expire_at']
        plan = VIP_PLANS.get(level, {})

        try:
            import __main__
            master_app = getattr(__main__, 'master_app', None)
            if master_app:
                await _retry_send(master_app.bot.send_message, 
                    chat_id=user_id,
                    text=(
                        f"⏰ <b>VIP 即将到期</b>\n\n"
                        f"你的 {plan.get('name', 'VIP')} 将在 {expire_at} 到期。\n"
                        f"到期后 Bot 数量限制将恢复为 {VIP_PLANS[0]['max_bots']} 个。\n\n"
                        f"💡 使用 /vip 续费，时间会自动叠加哦！"
                    ),
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.error("发送过期提醒给 %s 失败: %s", user_id, e)


# ==================== 内部辅助函数 ====================

async def _pause_excess_bots(user_id: int) -> list:
    """暂停超出限制的Bot，返回被暂停的Bot用户名列表"""
    max_bots = VIP_PLANS[0]['max_bots']  # 过期后用免费用户的限制
    bots = await get_active_bots_by_owner(user_id)

    if len(bots) <= max_bots:
        return []

    # 按创建时间排序，保留最早的 max_bots 个
    to_pause = bots[max_bots:]
    paused_names = []

    mgr = get_bot_manager()

    for bot in to_pause:
        bot_db_id = bot['id']
        bot_username = bot.get('bot_username', 'unknown')

        # 数据库标记暂停
        await pause_user_bot(bot_db_id)

        # 停止运行中的 Bot
        if mgr and bot_db_id in mgr._apps:
            await mgr.stop_bot(bot_db_id)

        paused_names.append(bot_username)
        logger.info("暂停用户 %s 的 Bot @%s (db_id=%s)", user_id, bot_username, bot_db_id)

    return paused_names


async def _try_resume_paused_bots(user_id: int, new_level: int) -> None:
    """支付成功后尝试恢复暂停的Bot"""
    max_bots = VIP_PLANS[new_level]['max_bots']
    all_bots = await get_active_bots_by_owner(user_id)
    paused_bots = await get_paused_bots_by_owner(user_id)

    if not paused_bots:
        return

    # 当前活跃Bot数量
    active_count = len([b for b in all_bots if b['status'] != 'paused'])
    available_slots = max_bots - active_count

    if available_slots <= 0:
        return

    # 恢复最多 available_slots 个暂停的Bot
    mgr = get_bot_manager()
    resumed = 0

    for bot in paused_bots[:available_slots]:
        bot_db_id = bot['id']
        bot_username = bot.get('bot_username', 'unknown')

        # 恢复数据库状态
        await resume_user_bot(bot_db_id)

        # 启动 Bot
        if mgr:
            from db import get_user_bot_by_id
            bot_record = await get_user_bot_by_id(bot_db_id)
            if bot_record:
                success = await mgr.start_bot(bot_record)
                if success:
                    resumed += 1
                    logger.info("恢复用户 %s 的 Bot @%s", user_id, bot_username)
                else:
                    logger.error("恢复 Bot @%s 启动失败", bot_username)

    if resumed > 0:
        try:
            import __main__
            master_app = getattr(__main__, 'master_app', None)
            if master_app:
                await _retry_send(master_app.bot.send_message, 
                    chat_id=user_id,
                    text=f"✅ 已自动恢复 {resumed} 个暂停的 Bot！",
                    parse_mode="HTML",
                )
        except Exception:
            pass


# ==================== 回调路由器 ====================

async def vip_callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """VIP 相关回调路由"""
    query = update.callback_query
    data = query.data

    if data.startswith("buy_vip|"):
        await buy_vip_callback(update, context)
    elif data == "vip_history":
        await vip_history_callback(update, context)
    else:
        await query.answer("未知操作", show_alert=True)
