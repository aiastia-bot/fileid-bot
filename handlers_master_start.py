"""主Bot入口处理 - start命令、managed_bot处理、黑名单检查中间件"""
import html
import logging

import httpx

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from database import (
    add_user_bot, get_user_bots_by_owner,
    get_user_bot_by_token, get_user_bot_by_telegram_id,
    get_user_bot_by_id,
    is_user_blacklisted,
)

logger = logging.getLogger(__name__)


def get_bot_manager():
    """获取全局 BotManager 实例"""
    import __main__
    return getattr(__main__, 'bot_manager', None)


def escape(text: str) -> str:
    """HTML 转义"""
    return html.escape(str(text), quote=False)


async def master_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """主Bot /start 命令"""
    text = (
        "🤖 <b>FileID Bot 托管平台</b>\n\n"
        "我可以帮你创建属于自己的 FileID Bot！\n"
        "每个 Bot 都有完整的文件ID互转功能。\n\n"
        "📌 <b>管理命令：</b>\n"
        "• /newbot — 一键创建你的 Bot\n"
        "• /addbot — 添加你的 Bot（提供 Token）\n"
        "• /mybots — 查看我的 Bot 列表\n"
        "• /delbot — 删除 Bot\n"
        "• /botstatus — 查看 Bot 运行状态\n\n"
        "💡 <b>使用方法：</b>\n"
        "1. 使用 /newbot 一键创建 Bot\n"
        "2. 或直接 /addbot 添加已有 Bot\n\n"
        "所有 Bot 共享服务器资源，你无需部署！"
    )
    await update.message.reply_text(text, parse_mode="HTML")


# ==================== Managed Bot 自动处理 ====================

async def handle_managed_bot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理 managed_bot 更新：自动获取 Token 并启动 Bot"""
    managed_info = update.api_kwargs.get('managed_bot')
    if not managed_info:
        return

    logger.info("收到 managed_bot 更新: %s", managed_info)

    owner = managed_info.get('user', {})
    bot_info = managed_info.get('bot', {})
    owner_id = owner.get('id')
    bot_id = bot_info.get('id')
    bot_username = bot_info.get('username', '')
    bot_name = bot_info.get('first_name', '')

    if not bot_id:
        logger.error("managed_bot 更新缺少 bot id: %s", managed_info)
        return

    # 检查用户 Bot 数量
    from config import MAX_BOTS_PER_USER
    user_bots = get_user_bots_by_owner(owner_id)
    if len(user_bots) >= MAX_BOTS_PER_USER:
        logger.warning("用户 %s 已达最大 Bot 数量", owner_id)
        return

    # 检查 Bot 是否已添加
    existing = get_user_bot_by_telegram_id(bot_id)
    if existing:
        logger.info("Bot @%s 已存在，跳过", bot_username)
        return

    # 通过 Telegram 官方 Bot API getManagedBotToken 获取 Token
    from config import BOT_TOKEN
    token = None
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getManagedBotToken",
                json={"user_id": bot_id},
                timeout=30
            )
            data = resp.json()
            if data.get("ok"):
                token = data.get("result")
                logger.info("成功获取 managed bot token for @%s", bot_username)
            else:
                logger.error("getManagedBotToken API 返回错误: %s", data.get("description"))
                return
    except Exception as e:
        logger.error("调用 getManagedBotToken API 失败: %s", e)
        return

    if not token:
        logger.error("获取到的 token 为空")
        return

    # 检查 Token 是否已存在
    existing_token = get_user_bot_by_token(token)
    if existing_token:
        logger.info("Token 已存在，跳过")
        return

    # 保存到数据库
    record_id = add_user_bot(
        owner_id=owner_id,
        bot_token=token,
        bot_id=bot_id,
        bot_username=bot_username,
        bot_firstname=bot_name,
    )

    if not record_id:
        logger.error("保存 managed bot 失败")
        return

    # 启动 Bot
    mgr = get_bot_manager()
    if mgr:
        bot_record = get_user_bot_by_id(record_id)
        success = await mgr.start_bot(bot_record)
        if success:
            logger.info("✅ Managed Bot @%s 自动启动成功 (owner=%s)", bot_username, owner_id)
            # 尝试通知用户
            try:
                await context.bot.send_message(
                    chat_id=owner_id,
                    text=(
                        f"✅ <b>Bot 创建成功并已启动！</b>\n\n"
                        f"🤖 名称：{escape(bot_name)}\n"
                        f"📌 用户名：@{escape(bot_username)}\n"
                        f"🆔 Bot ID：<code>{bot_id}</code>\n\n"
                        f"现在直接向 @{escape(bot_username)} 发送文件即可使用！"
                    ),
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.warning("通知用户失败: %s", e)
        else:
            logger.error("Managed Bot @%s 启动失败", bot_username)
    else:
        logger.error("BotManager 未初始化")


# ==================== 黑名单检查中间件 ====================

async def blacklist_check_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """黑名单检查中间件 - 在所有命令之前检查用户是否被封禁"""
    if not update.effective_user:
        return

    user_id = update.effective_user.id
    from config import ADMIN_IDS

    # 管理员不受限制
    if user_id in ADMIN_IDS:
        return

    # 检查黑名单
    if is_user_blacklisted(user_id):
        # 被封禁用户：静默忽略或发送提示
        if update.message:
            try:
                await update.message.reply_text(
                    "⛔ 你已被管理员禁止使用本平台。\n"
                    "如有疑问请联系管理员。"
                )
            except Exception:
                pass
        # 不继续处理后续 handler
        raise ApplicationHandlerStop()