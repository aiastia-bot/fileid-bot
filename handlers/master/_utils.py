"""Master handlers 共享工具函数"""
import html
import logging

logger = logging.getLogger(__name__)


def get_bot_manager():
    """获取全局 BotManager 实例"""
    from services import runtime
    return runtime.manager


def escape(text: str) -> str:
    """HTML 转义"""
    return html.escape(str(text), quote=False)


async def build_free_block_reply():
    """构造「无法免费创建 Bot」的提示，返回 (text, reply_markup)
    区分两种被拦原因：免费注册已关闭（引导购买基础版）/ 免费名额已满（引导升级 VIP）
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from db.vip import is_free_registration_closed
    from config import VIP_PLANS, BASIC_LEVEL

    if await is_free_registration_closed():
        from services.membership import get_plan
        plan = await get_plan(BASIC_LEVEL)
        text = (
            "⚠️ 免费注册已停止。\n\n"
            f"💡 购买基础版（{plan['monthly_price']}⭐/月）即可创建 {plan['max_bots']} 个 Bot，"
            "或升级 VIP 解锁更多权益。"
        )
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                f"💎 基础版 月付 {plan['monthly_price']}⭐",
                callback_data=f"buy_vip|{BASIC_LEVEL}|1",
            ),
        ]])
    else:
        text = (
            "⚠️ 系统资源紧张，当前暂不接受新用户创建 Bot。\n\n"
            "💡 请升级 VIP 即可继续使用：/vip"
        )
        markup = None
    return text, markup


async def build_basic_expired_reply():
    """基础版已过期（max_bots=0）的续费提示，返回 (text, reply_markup)"""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    from config import VIP_PLANS, BASIC_LEVEL

    from services.membership import get_plan
    plan = await get_plan(BASIC_LEVEL)
    text = (
        "⚠️ 你的基础版已过期，Bot 已暂停。\n\n"
        "💡 续费后 Bot 将自动恢复运行。"
    )
    markup = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            f"💎 续费基础版 {plan['monthly_price']}⭐/月",
            callback_data=f"buy_vip|{BASIC_LEVEL}|1",
        ),
    ]])
    return text, markup