"""Bot 管理命令 - /mybots, /delbot, /botstatus, 重启, 更新Token"""
import html
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from database import (
    get_user_bots_by_owner, get_user_bot_by_id,
    delete_user_bot as db_delete_user_bot,
    update_user_bot_status, update_user_bot_token,
    get_user_bot_by_token, get_user_bot_by_telegram_id,
)

logger = logging.getLogger(__name__)


def get_bot_manager():
    """获取全局 BotManager 实例"""
    import __main__
    return getattr(__main__, 'bot_manager', None)


def escape(text: str) -> str:
    """HTML 转义"""
    return html.escape(str(text), quote=False)


async def my_bots_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mybots 查看用户的Bot列表"""
    user_id = update.effective_user.id
    bots = await get_user_bots_by_owner(user_id)

    if not bots:
        await update.message.reply_text(
            "📭 你还没有添加任何 Bot。\n\n使用 /newbot 一键创建！"
        )
        return

    mgr = get_bot_manager()
    text = "📋 <b>我的 Bot 列表：</b>\n\n"
    for i, bot in enumerate(bots, 1):
        is_running = mgr and bot['id'] in mgr.get_all_apps()
        status_emoji = "🟢" if is_running else "🔴"
        text += (
            f"{i}. {status_emoji} <b>{escape(bot['bot_firstname'])}</b>\n"
            f"   @{escape(bot['bot_username'])} | ID: <code>{bot['bot_id']}</code>\n\n"
        )

    text += f"共 {len(bots)} 个 Bot"
    await update.message.reply_text(text, parse_mode="HTML")


async def delete_bot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/delbot 删除用户Bot"""
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "请提供 Bot 的用户名或编号。\n"
            "用法：<code>/delbot @用户名</code> 或 <code>/delbot 编号</code>\n\n"
            "使用 /mybots 查看你的 Bot 列表。",
            parse_mode="HTML"
        )
        return

    bots = await get_user_bots_by_owner(user_id)
    if not bots:
        await update.message.reply_text("📭 你没有可删除的 Bot。")
        return

    arg = context.args[0].strip()
    target_bot = None

    try:
        idx = int(arg) - 1
        if 0 <= idx < len(bots):
            target_bot = bots[idx]
    except ValueError:
        pass

    if not target_bot:
        username = arg.lstrip('@')
        for bot in bots:
            if bot['bot_username'].lower() == username.lower():
                target_bot = bot
                break

    if not target_bot:
        await update.message.reply_text(
            "❌ 未找到指定的 Bot。使用 /mybots 查看列表。",
            parse_mode="HTML"
        )
        return

    mgr = get_bot_manager()
    if mgr:
        await mgr.stop_bot(target_bot['id'])

    await db_delete_user_bot(target_bot["id"])

    await update.message.reply_text(
        f"✅ Bot @{escape(target_bot['bot_username'])} 已删除。"
    )
    logger.info("用户 %s 删除了 Bot @%s", user_id, target_bot['bot_username'])


async def bot_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/botstatus 查看Bot运行状态，支持重启已停止的Bot"""
    user_id = update.effective_user.id
    bots = await get_user_bots_by_owner(user_id)

    if not bots:
        await update.message.reply_text(
            "📭 你没有 Bot。使用 /newbot 创建！"
        )
        return

    mgr = get_bot_manager()
    text = "🚀 <b>Bot 运行状态：</b>\n\n"
    stopped_buttons = []

    for bot in bots:
        is_running = mgr and bot['id'] in mgr.get_all_apps()
        if is_running:
            status = "🟢 运行中"
        elif bot['status'] == 'revoked':
            status = "⚠️ Token已失效"
        elif bot['status'] == 'banned':
            status = "🚫 已封禁"
        else:
            status = "🔴 已停止"
        text += f"- @{escape(bot['bot_username'])}: {status}\n"

        # 已停止且非 banned 的Bot：提供重启按钮
        if not is_running and bot['status'] not in ('banned',):
            stopped_buttons.append(
                InlineKeyboardButton(
                    f"🔄 重启 @{bot['bot_username']}",
                    callback_data=f"restart_bot|{bot['id']}"
                )
            )

    # 添加操作按钮
    keyboard = []
    if stopped_buttons:
        text += "\n💡 点击下方按钮操作。"
        for btn in stopped_buttons:
            keyboard.append([btn])

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=reply_markup)


async def restart_bot_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理用户重启Bot的回调按钮"""
    query = update.callback_query
    if not query or not query.data:
        return

    user_id = query.from_user.id
    data = query.data

    if not data.startswith("restart_bot|"):
        return

    try:
        bot_db_id = int(data.split("|", 1)[1])
    except (ValueError, IndexError):
        await query.answer("❌ 数据错误", show_alert=True)
        return

    # 验证这个Bot属于该用户
    bot_record = await get_user_bot_by_id(bot_db_id)
    if not bot_record or bot_record['owner_id'] != user_id:
        await query.answer("❌ 无权操作此 Bot", show_alert=True)
        return

    # 检查是否被封禁
    if bot_record['status'] == 'banned':
        await query.answer("🚫 此 Bot 已被封禁，无法重启", show_alert=True)
        return

    await query.answer("⏳ 正在重启...")
    await query.edit_message_text(f"⏳ 正在重启 @{escape(bot_record['bot_username'])}...")

    # 更新数据库状态为 active
    await update_user_bot_status(bot_db_id, 'active')

    # 尝试启动
    mgr = get_bot_manager()
    if not mgr:
        await query.edit_message_text("❌ 系统错误，请联系管理员。")
        return

    # 先停止旧实例（如果存在）
    await mgr.stop_bot(bot_db_id)

    # 重新获取记录并启动
    bot_record = await get_user_bot_by_id(bot_db_id)
    success = await mgr.start_bot(bot_record)

    if success:
        await query.edit_message_text(
            f"✅ <b>Bot 重启成功！</b>\n\n"
            f"🤖 @{escape(bot_record['bot_username'])} 已恢复运行。\n"
            f"现在可以向它发送文件了。",
            parse_mode="HTML"
        )
        logger.info("用户 %s 重启了 Bot @%s", user_id, bot_record['bot_username'])
    else:
        await query.edit_message_text(
            f"❌ <b>重启失败</b>\n\n"
            f"Bot @{escape(bot_record['bot_username'])} 启动失败。\n"
            f"可能原因：Token 已失效或网络问题。\n\n"
            f"💡 请尝试：\n"
            f"1. 稍后再试一次 /botstatus 重启\n"
            f"2. 如果反复失败，可能是 Token 失效，使用 /updatetoken 更新\n"
            f"3. 或使用 /delbot 删除后用 /newbot 重新创建",
            parse_mode="HTML"
        )


async def update_token_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理用户点击更新Token按钮的回调"""
    query = update.callback_query
    if not query or not query.data:
        return

    user_id = query.from_user.id
    data = query.data

    if not data.startswith("update_token|"):
        return

    try:
        bot_db_id = int(data.split("|", 1)[1])
    except (ValueError, IndexError):
        await query.answer("❌ 数据错误", show_alert=True)
        return

    # 验证这个Bot属于该用户
    bot_record = await get_user_bot_by_id(bot_db_id)
    if not bot_record or bot_record['owner_id'] != user_id:
        await query.answer("❌ 无权操作此 Bot", show_alert=True)
        return

    # 检查是否被封禁
    if bot_record['status'] == 'banned':
        await query.answer("🚫 此 Bot 已被封禁，无法操作", show_alert=True)
        return

    # 保存到 user_data，等待用户发送新 Token
    context.user_data['update_token_bot_id'] = bot_db_id

    await query.answer()
    await query.edit_message_text(
        f"🔑 <b>更新 Bot Token</b>\n\n"
        f"Bot：@{escape(bot_record['bot_username'])}\n\n"
        f"当前 Token 已失效，请发送新的 Token：\n\n"
        f"💡 如何获取新 Token：\n"
        f"1. 前往 @BotFather\n"
        f"2. 发送 <code>/token</code>\n"
        f"3. 选择 @{escape(bot_record['bot_username'])}\n"
        f"4. 将返回的新 Token 发到这里\n\n"
        f"或使用命令：<code>/updatetoken <新Token></code>",
        parse_mode="HTML"
    )


async def update_token_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/updatetoken 更新已失效Bot的Token"""
    user_id = update.effective_user.id

    # 检查是否通过按钮进入等待状态（直接发送Token文本）
    if not context.args:
        bot_db_id = context.user_data.get('update_token_bot_id')
        if bot_db_id and update.message and update.message.text:
            token = update.message.text.strip()
        else:
            await update.message.reply_text(
                "🔑 <b>更新 Bot Token</b>\n\n"
                "用法：<code>/updatetoken <新Token></code>\n\n"
                "用于更新已失效（revoked）Bot 的 Token。\n"
                "使用 /botstatus 查看哪些 Bot 需要更新。",
                parse_mode="HTML"
            )
            return
    else:
        token = context.args[0].strip()
        bot_db_id = context.user_data.get('update_token_bot_id')

    # 如果没有通过按钮指定 bot，让用户指定
    if not bot_db_id:
        await update.message.reply_text(
            "❌ 请先使用 /botstatus 点击「更新Token」按钮，\n"
            "或使用 <code>/updatetoken <新Token></code> 并先通过 /botstatus 选择 Bot。",
            parse_mode="HTML"
        )
        return

    # 清除等待状态
    context.user_data.pop('update_token_bot_id', None)

    # 验证 Token 格式
    if ":" not in token or len(token) < 10:
        await update.message.reply_text(
            "❌ Token 格式不正确。\n\n"
            "Token 格式类似：<code>123456789:ABCdefGHIjklMNOpqrS</code>",
            parse_mode="HTML"
        )
        return

    # 验证 Bot 归属
    bot_record = await get_user_bot_by_id(bot_db_id)
    if not bot_record or bot_record['owner_id'] != user_id:
        await update.message.reply_text("❌ 无权操作此 Bot。")
        return

    # 检查 Token 是否已被其他 Bot 使用
    existing_token = await get_user_bot_by_token(token)
    if existing_token and existing_token['id'] != bot_db_id:
        await update.message.reply_text(
            f"⚠️ 该 Token 已被 Bot @{escape(existing_token['bot_username'])} 使用。"
        )
        return

    status_msg = await update.message.reply_text("⏳ 正在校验新 Token...")

    # 校验新 Token
    from telegram import Bot
    test_bot = None
    try:
        test_bot = Bot(token=token)
        bot_info = await test_bot.get_me()
    except Exception as e:
        await status_msg.edit_text(
            f"❌ Token 校验失败：{escape(str(e)[:100])}\n\n请检查 Token 是否正确。"
        )
        return
    finally:
        if test_bot:
            try:
                await test_bot.shutdown()
            except Exception:
                pass

    # 检查新 Token 对应的 Bot ID 是否被其他记录占用
    existing_by_id = await get_user_bot_by_telegram_id(bot_info.id)
    if existing_by_id and existing_by_id['id'] != bot_db_id:
        await status_msg.edit_text(
            f"⚠️ 该 Token 对应的 Bot 已被其他记录使用。"
        )
        return

    # 更新数据库
    success = await update_user_bot_token(bot_db_id, token, bot_info.id)
    if not success:
        await status_msg.edit_text("❌ 更新 Token 失败，请重试。")
        return

    # 先停止旧实例
    mgr = get_bot_manager()
    if mgr:
        await mgr.stop_bot(bot_db_id)

    # 重新获取记录并启动
    bot_record = await get_user_bot_by_id(bot_db_id)
    started = False
    if mgr:
        started = await mgr.start_bot(bot_record)

    if started:
        await status_msg.edit_text(
            f"✅ <b>Token 更新成功，Bot 已重启！</b>\n\n"
            f"🤖 @{escape(bot_record['bot_username'])}\n"
            f"📌 Bot 名称：{escape(bot_info.first_name)}\n"
            f"🆔 Bot ID：<code>{bot_info.id}</code>\n\n"
            f"现在可以向它发送文件了。",
            parse_mode="HTML"
        )
        logger.info("用户 %s 更新了 Bot @%s 的 Token 并重启", user_id, bot_record['bot_username'])
    else:
        await status_msg.edit_text(
            f"⚠️ Token 已更新，但 Bot 启动失败。\n\n"
            f"请使用 /botstatus 查看状态，或联系管理员。",
            parse_mode="HTML"
        )