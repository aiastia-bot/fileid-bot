"""/newbot 交互式创建和 /addbot 命令处理"""
import html
import logging
import urllib.parse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler

from database import (
    add_user_bot, get_user_bots_by_owner,
    get_user_bot_by_token, get_user_bot_by_telegram_id,
    get_user_bot_by_id,
)
from db_vip import get_max_bots_for_user

logger = logging.getLogger(__name__)

# Conversation states for /newbot
INPUT_BOT_USERNAME, INPUT_BOT_NAME, INPUT_BOT_TOKEN = range(3)


def get_bot_manager():
    """获取全局 BotManager 实例"""
    import __main__
    return getattr(__main__, 'bot_manager', None)


def escape(text: str) -> str:
    """HTML 转义"""
    return html.escape(str(text), quote=False)


async def new_bot_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/newbot 开始交互式创建 Bot"""
    user_id = update.effective_user.id

    max_bots = await get_max_bots_for_user(user_id)
    user_bots = await get_user_bots_by_owner(user_id)
    if len(user_bots) >= max_bots:
        await update.message.reply_text(
            f"⚠️ 你已达到 Bot 数量上限（{max_bots} 个）。\n\n"
            f"💡 使用 /vip 升级 VIP 可创建更多 Bot，或使用 /delbot 删除已有 Bot。"
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "🤖 <b>创建新 Bot</b>\n\n"
        "请输入 Bot 的 <b>用户名</b>（必须以 <code>bot</code> 结尾）\n\n"
        "例如：<code>myfile_bot</code>\n\n"
        "💡 输入 /cancel 取消操作",
        parse_mode="HTML"
    )
    return INPUT_BOT_USERNAME


async def new_bot_input_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """接收 Bot 用户名"""
    username = update.message.text.strip().lstrip('@')

    if not username.lower().endswith('bot'):
        await update.message.reply_text(
            "❌ Bot 用户名必须以 <code>bot</code> 结尾，请重新输入。\n\n"
            "例如：<code>myfile_bot</code>",
            parse_mode="HTML"
        )
        return INPUT_BOT_USERNAME

    context.user_data['new_bot_username'] = username

    await update.message.reply_text(
        f"✅ Bot 用户名：<code>@{escape(username)}</code>\n\n"
        f"请输入 Bot 的 <b>显示名称</b>：\n\n"
        f"例如：<code>我的文件Bot</code>",
        parse_mode="HTML"
    )
    return INPUT_BOT_NAME


async def new_bot_input_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """接收 Bot 显示名称，生成 BotFather 创建链接，等待 Token"""
    bot_name = update.message.text.strip()
    bot_username = context.user_data.get('new_bot_username', '')

    if not bot_username:
        await update.message.reply_text("❌ 出错了，请重新使用 /newbot 开始。")
        return ConversationHandler.END

    context.user_data['new_bot_name'] = bot_name
    master_username = context.bot.username

    # 生成 BotFather newbot 深度链接（Managed Bot）
    encoded_name = urllib.parse.quote(bot_name, safe='')
    create_link = f"https://t.me/newbot/{master_username}/{bot_username}?name={encoded_name}"

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 一键创建 Bot", url=create_link)],
    ])

    text = (
        f"✅ <b>创建信息确认</b>\n\n"
        f"Bot 名称：<code>{escape(bot_name)}</code>\n"
        f"Bot 用户名：<code>@{escape(bot_username)}</code>\n\n"
        f"👇 <b>下一步：</b>\n"
        f"1. 点击上方按钮创建 Bot\n"
        f"2. BotFather 会自动创建并返回 Token\n"
        f"3. 系统会 <b>自动获取 Token 并启动</b> 你的 Bot\n"
        f"4. 如果未自动启动，请把 Token 发到这里\n\n"
        f"💡 输入 /cancel 取消操作"
    )

    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)
    return INPUT_BOT_TOKEN


async def new_bot_input_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """接收 Token 并自动注册启动 Bot（备用，如果自动获取失败）"""
    token = update.message.text.strip()
    user_id = update.effective_user.id

    if ":" not in token or len(token) < 10:
        await update.message.reply_text(
            "❌ 这不像是一个有效的 Token，请重新输入。\n\n"
            "Token 格式类似：<code>123456789:ABCdefGHIjklMNOpqrS</code>",
            parse_mode="HTML"
        )
        return INPUT_BOT_TOKEN

    existing = await get_user_bot_by_token(token)
    if existing:
        await update.message.reply_text(
            f"⚠️ Bot @{escape(existing['bot_username'])} 已经添加过了。"
        )
        context.user_data.pop('new_bot_username', None)
        context.user_data.pop('new_bot_name', None)
        return ConversationHandler.END

    status_msg = await update.message.reply_text("⏳ 正在校验 Token 并启动 Bot...")

    from telegram import Bot
    test_bot = None
    try:
        test_bot = Bot(token=token)
        bot_info = await test_bot.get_me()
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Token 校验失败：{escape(str(e)[:100])}\n\n请重新输入正确的 Token。"
        )
        return INPUT_BOT_TOKEN
    finally:
        if test_bot:
            try:
                await test_bot.shutdown()
            except Exception:
                pass

    existing_by_id = await get_user_bot_by_telegram_id(bot_info.id)
    if existing_by_id:
        await status_msg.edit_text(
            f"⚠️ Bot @{escape(bot_info.username)} 已被添加。",
            parse_mode="HTML"
        )
        context.user_data.pop('new_bot_username', None)
        context.user_data.pop('new_bot_name', None)
        return ConversationHandler.END

    max_bots = await get_max_bots_for_user(user_id)
    user_bots = await get_user_bots_by_owner(user_id)
    if len(user_bots) >= max_bots:
        await status_msg.edit_text(
            f"⚠️ 你已达到 Bot 数量上限（{max_bots} 个）。\n\n"
            f"💡 使用 /vip 升级 VIP 可创建更多 Bot，或使用 /delbot 删除已有 Bot。"
        )
        context.user_data.pop('new_bot_username', None)
        context.user_data.pop('new_bot_name', None)
        return ConversationHandler.END

    record_id = await add_user_bot(
        owner_id=user_id,
        bot_token=token,
        bot_id=bot_info.id,
        bot_username=bot_info.username or "",
        bot_firstname=bot_info.first_name,
    )

    if not record_id:
        await status_msg.edit_text("❌ 添加失败，请重试。")
        return INPUT_BOT_TOKEN

    mgr = get_bot_manager()
    if mgr:
        bot_record = await get_user_bot_by_id(record_id)
        success = await mgr.start_bot(bot_record)
        if success:
            await status_msg.edit_text(
                f"✅ <b>Bot 创建成功并已启动！</b>\n\n"
                f"🤖 名称：{escape(bot_info.first_name)}\n"
                f"📌 用户名：@{escape(bot_info.username)}\n"
                f"🆔 Bot ID：<code>{bot_info.id}</code>\n\n"
                f"现在直接向 @{escape(bot_info.username)} 发送文件即可使用！",
                parse_mode="HTML"
            )
        else:
            await status_msg.edit_text(
                f"⚠️ Bot 已保存但启动失败，请联系管理员。",
                parse_mode="HTML"
            )
    else:
        await status_msg.edit_text("❌ BotManager 未初始化，请联系管理员。")

    context.user_data.pop('new_bot_username', None)
    context.user_data.pop('new_bot_name', None)
    return ConversationHandler.END


async def new_bot_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """取消创建"""
    context.user_data.pop('new_bot_username', None)
    context.user_data.pop('new_bot_name', None)
    await update.message.reply_text("❌ 已取消创建 Bot。")
    return ConversationHandler.END


# ==================== /addbot ====================

async def add_bot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/addbot 添加用户Bot"""
    user_id = update.effective_user.id

    # 检查已有 Bot 数量
    max_bots = await get_max_bots_for_user(user_id)
    user_bots = await get_user_bots_by_owner(user_id)
    if len(user_bots) >= max_bots:
        await update.message.reply_text(
            f"⚠️ 你已达到 Bot 数量上限（{max_bots} 个）。\n\n"
            f"💡 使用 /vip 升级 VIP 可创建更多 Bot，或使用 /delbot 删除已有 Bot。"
        )
        return

    if not context.args:
        await update.message.reply_text(
            "🔑 <b>添加 Bot</b>\n\n"
            "请使用以下命令格式：\n"
            "<code>/addbot &lt;Token&gt;</code>\n\n"
            "例如：<code>/addbot 123456:ABCdefGHIjklMNOpqrS</code>\n\n"
            "💡 不知道怎么获取 Token？使用 /newbot 一键创建！",
            parse_mode="HTML"
        )
        return

    token = context.args[0].strip()

    if ":" not in token:
        await update.message.reply_text("❌ Token 格式不正确，请检查后重试。")
        return

    existing = await get_user_bot_by_token(token)
    if existing:
        await update.message.reply_text(
            f"⚠️ Bot @{escape(existing['bot_username'])} 已经添加过了。"
        )
        return

    status_msg = await update.message.reply_text("⏳ 正在校验 Token...")

    from telegram import Bot
    test_bot = None
    try:
        test_bot = Bot(token=token)
        bot_info = await test_bot.get_me()
    except Exception as e:
        await status_msg.edit_text(f"❌ Token 校验失败：{escape(str(e)[:100])}\n\n请检查Token是否正确。")
        return
    finally:
        if test_bot:
            try:
                await test_bot.shutdown()
            except Exception:
                pass

    existing_by_id = await get_user_bot_by_telegram_id(bot_info.id)
    if existing_by_id:
        await status_msg.edit_text(
            f"⚠️ Bot @{escape(bot_info.username)} 已被添加。",
            parse_mode="HTML"
        )
        return

    max_bots = await get_max_bots_for_user(user_id)
    user_bots = await get_user_bots_by_owner(user_id)
    if len(user_bots) >= max_bots:
        await status_msg.edit_text(
            f"⚠️ 你已达到 Bot 数量上限（{max_bots} 个）。\n\n"
            f"💡 使用 /vip 升级 VIP 可创建更多 Bot，或使用 /delbot 删除已有 Bot。"
        )
        return

    record_id = await add_user_bot(
        owner_id=user_id,
        bot_token=token,
        bot_id=bot_info.id,
        bot_username=bot_info.username or "",
        bot_firstname=bot_info.first_name,
    )

    if not record_id:
        await status_msg.edit_text("❌ 添加失败，请重试。")
        return

    mgr = get_bot_manager()
    if mgr:
        bot_record = await get_user_bot_by_id(record_id)
        success = await mgr.start_bot(bot_record)
        if success:
            await status_msg.edit_text(
                f"✅ <b>Bot 添加成功！</b>\n\n"
                f"🤖 名称：{escape(bot_info.first_name)}\n"
                f"📌 用户名：@{escape(bot_info.username)}\n"
                f"🆔 Bot ID：<code>{bot_info.id}</code>\n\n"
                f"现在直接向 @{escape(bot_info.username)} 发送文件即可使用！",
                parse_mode="HTML"
            )
            logger.info("用户 %s 添加了 Bot @%s", user_id, bot_info.username)
        else:
            await status_msg.edit_text(
                f"⚠️ Bot 已保存但启动失败，请联系管理员。",
                parse_mode="HTML"
            )
    else:
        await status_msg.edit_text("❌ BotManager 未初始化，请联系管理员。")