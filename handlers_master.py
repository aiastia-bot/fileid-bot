"""主Bot管理命令处理器 - 处理用户Bot的添加、查看、删除等操作"""
import html
import io
import json
import logging
import urllib.parse
from datetime import datetime

import httpx

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationHandlerStop, ContextTypes, ConversationHandler, MessageHandler, filters
)

from database import (
    add_user_bot, get_user_bots_by_owner, get_user_bot_by_id,
    get_all_owner_ids,
    get_user_bot_by_token, get_user_bot_by_telegram_id,
    delete_user_bot as db_delete_user_bot,
    update_user_bot_status, get_platform_stats,
    get_platform_bot_details, get_platform_export_data,
    get_files_by_bot_username,
    add_to_blacklist, remove_from_blacklist, is_user_blacklisted,
    get_blacklist, get_blacklist_count,
    get_user_bots_by_owner as get_all_owner_bots,
)

logger = logging.getLogger(__name__)

# Conversation states for /newbot (备用，手动输入)
INPUT_BOT_USERNAME, INPUT_BOT_NAME, INPUT_BOT_TOKEN = range(3)


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
    # https://core.telegram.org/bots/api#getmanagedbottoken
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


# ==================== /newbot 交互式创建 ====================

async def new_bot_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/newbot 开始交互式创建 Bot"""
    user_id = update.effective_user.id

    from config import MAX_BOTS_PER_USER
    user_bots = get_user_bots_by_owner(user_id)
    if len(user_bots) >= MAX_BOTS_PER_USER:
        existing = user_bots[0]
        await update.message.reply_text(
            f"⚠️ 你已有一个 Bot：@{escape(existing['bot_username'])}\n\n"
            f"请先使用 /delbot 删除后再创建新 Bot。"
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

    existing = get_user_bot_by_token(token)
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

    existing_by_id = get_user_bot_by_telegram_id(bot_info.id)
    if existing_by_id:
        await status_msg.edit_text(
            f"⚠️ Bot @{escape(bot_info.username)} 已被添加。",
            parse_mode="HTML"
        )
        context.user_data.pop('new_bot_username', None)
        context.user_data.pop('new_bot_name', None)
        return ConversationHandler.END

    from config import MAX_BOTS_PER_USER
    user_bots = get_user_bots_by_owner(user_id)
    if len(user_bots) >= MAX_BOTS_PER_USER:
        await status_msg.edit_text(
            f"⚠️ 每个用户最多添加 {MAX_BOTS_PER_USER} 个 Bot。\n\n"
            f"请先使用 /delbot 删除已有 Bot。"
        )
        context.user_data.pop('new_bot_username', None)
        context.user_data.pop('new_bot_name', None)
        return ConversationHandler.END

    record_id = add_user_bot(
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
        bot_record = get_user_bot_by_id(record_id)
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
    from config import MAX_BOTS_PER_USER
    user_bots = get_user_bots_by_owner(user_id)
    if len(user_bots) >= MAX_BOTS_PER_USER:
        existing = user_bots[0]
        await update.message.reply_text(
            f"⚠️ 你已有一个 Bot：@{escape(existing['bot_username'])}\n\n"
            f"请先使用 /delbot 删除后再添加新 Bot。"
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

    existing = get_user_bot_by_token(token)
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

    existing_by_id = get_user_bot_by_telegram_id(bot_info.id)
    if existing_by_id:
        await status_msg.edit_text(
            f"⚠️ Bot @{escape(bot_info.username)} 已被添加。",
            parse_mode="HTML"
        )
        return

    from config import MAX_BOTS_PER_USER
    user_bots = get_user_bots_by_owner(user_id)
    if len(user_bots) >= MAX_BOTS_PER_USER:
        await status_msg.edit_text(
            f"⚠️ 每个用户最多添加 {MAX_BOTS_PER_USER} 个 Bot。\n\n"
            f"请先使用 /delbot 删除已有 Bot。"
        )
        return

    record_id = add_user_bot(
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
        bot_record = get_user_bot_by_id(record_id)
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


# ==================== 其他命令 ====================

async def my_bots_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mybots 查看用户的Bot列表"""
    user_id = update.effective_user.id
    bots = get_user_bots_by_owner(user_id)

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

    bots = get_user_bots_by_owner(user_id)
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

    db_delete_user_bot(target_bot['id'])

    await update.message.reply_text(
        f"✅ Bot @{escape(target_bot['bot_username'])} 已删除。"
    )
    logger.info("用户 %s 删除了 Bot @%s", user_id, target_bot['bot_username'])


async def bot_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/botstatus 查看Bot运行状态"""
    user_id = update.effective_user.id
    bots = get_user_bots_by_owner(user_id)

    if not bots:
        await update.message.reply_text(
            "📭 你没有 Bot。使用 /newbot 创建！"
        )
        return

    mgr = get_bot_manager()
    text = "🚀 <b>Bot 运行状态：</b>\n\n"
    for bot in bots:
        is_running = mgr and bot['id'] in mgr.get_all_apps()
        status = "🟢 运行中" if is_running else "🔴 已停止"
        text += f"- @{escape(bot['bot_username'])}: {status}\n"

    await update.message.reply_text(text, parse_mode="HTML")


async def platform_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/platform 管理员查看平台统计和 Bot 详情"""
    from config import ADMIN_IDS
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 此命令仅限管理员使用。")
        return

    # 检查是否有参数，如 /platform bots 显示详细 Bot 列表
    args = context.args or []
    show_bots = args and args[0] in ('bots', 'bot', 'detail', 'details')

    stats = get_platform_stats()
    mgr = get_bot_manager()
    running = mgr.active_count if mgr else 0
    bl_count = get_blacklist_count()

    # 总览信息
    text = (
        f"📊 <b>平台统计</b>\n\n"
        f"🤖 活跃 Bot 数: {stats['bot_count']} (运行中: {running})\n"
        f"👥 Bot 所有者数: {stats['owner_count']}\n"
        f"📁 总文件数: {stats['file_count']}\n"
        f"📦 总集合数: {stats['col_count']}\n"
        f"🚫 黑名单用户: {bl_count}\n"
    )

    # 如果指定了 bots 参数，显示每个 Bot 的详细信息
    if show_bots:
        bot_details = get_platform_bot_details()
        if not bot_details:
            text += "\n📭 暂无 Bot。"
        else:
            text += f"\n{'='*20}\n"
            text += f"🤖 <b>Bot 详细列表</b> (共 {len(bot_details)} 个)\n\n"
            for i, bot in enumerate(bot_details, 1):
                is_running = mgr and bot['id'] in mgr.get_all_apps()
                status = "🟢" if is_running else ("🔴" if bot['status'] == 'active' else "⚠️")
                text += (
                    f"{i}. {status} <b>{escape(bot['bot_firstname'])}</b>\n"
                    f"   📌 @{escape(bot['bot_username'])}\n"
                    f"   🆔 Bot ID: <code>{bot['bot_id']}</code>\n"
                    f"   👤 所有者: <code>{bot['owner_id']}</code>\n"
                    f"   📁 文件: {bot['file_count']} | 📦 集合: {bot['col_count']} | 👥 用户: {bot['user_count']}\n"
                    f"   📅 创建: {bot['created_at']}\n\n"
                )

            # 分页提示
            text += (
                f"\n💡 提示: 使用 /export 导出完整数据\n"
                f"使用 /blacklist 管理黑名单"
            )
    else:
        # 默认只显示摘要，提示可以查看详情
        text += (
            f"\n💡 使用 <code>/platform bots</code> 查看每个 Bot 的详细信息"
        )

    # Telegram 消息长度限制为 4096 字符，需要分段发送
    if len(text) > 4000:
        # 分段发送
        parts = []
        current = ""
        for line in text.split('\n'):
            if len(current) + len(line) + 1 > 3900:
                parts.append(current)
                current = line + '\n'
            else:
                current += line + '\n'
        if current:
            parts.append(current)

        for i, part in enumerate(parts):
            if i == 0:
                await update.message.reply_text(part, parse_mode="HTML")
            else:
                await context.bot.send_message(
                    chat_id=update.message.chat_id,
                    text=part,
                    parse_mode="HTML"
                )
    else:
        await update.message.reply_text(text, parse_mode="HTML")


# ==================== 黑名单管理 ====================

async def blacklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/blacklist 管理黑名单"""
    from config import ADMIN_IDS
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 此命令仅限管理员使用。")
        return

    if not context.args:
        # 显示黑名单列表
        bl = get_blacklist()
        text = f"🚫 <b>黑名单管理</b>\n\n"
        text += f"当前黑名单用户数: {len(bl)}\n\n"
        text += "<b>用法：</b>\n"
        text += "• <code>/blacklist add <用户ID> [原因]</code> — 添加到黑名单\n"
        text += "• <code>/blacklist del <用户ID></code> — 从黑名单移除\n"
        text += "• <code>/blacklist list</code> — 查看黑名单列表\n"
        text += "• <code>/blacklist check <用户ID></code> — 检查用户状态\n\n"

        if bl:
            text += "<b>当前黑名单：</b>\n"
            for entry in bl[:20]:  # 最多显示20条
                reason = f" ({escape(entry['reason'])})" if entry['reason'] else ""
                text += f"• <code>{entry['user_id']}</code>{reason} — {entry['created_at']}\n"
            if len(bl) > 20:
                text += f"\n... 还有 {len(bl) - 20} 条记录"
        else:
            text += "📭 黑名单为空。"

        await update.message.reply_text(text, parse_mode="HTML")
        return

    action = context.args[0].lower()

    if action == 'add':
        if len(context.args) < 2:
            await update.message.reply_text(
                "用法：<code>/blacklist add <用户ID> [原因]</code>",
                parse_mode="HTML"
            )
            return
        try:
            target_id = int(context.args[1])
        except ValueError:
            await update.message.reply_text("❌ 用户ID必须是数字。")
            return

        # 不能封禁管理员
        if target_id in ADMIN_IDS:
            await update.message.reply_text("❌ 不能将管理员加入黑名单。")
            return

        reason = ' '.join(context.args[2:]) if len(context.args) > 2 else ''
        if add_to_blacklist(target_id, reason):
            # 如果该用户有正在运行的 Bot，停止它们
            target_bots = get_user_bots_by_owner(target_id)
            mgr = get_bot_manager()
            stopped = 0
            for bot in target_bots:
                if mgr and bot['id'] in mgr.get_all_apps():
                    await mgr.stop_bot(bot['id'])
                    stopped += 1
                update_user_bot_status(bot['id'], 'banned')

            text = f"✅ 用户 <code>{target_id}</code> 已加入黑名单。"
            if reason:
                text += f"\n原因: {escape(reason)}"
            if stopped > 0:
                text += f"\n🛑 已停止 {stopped} 个 Bot。"
            if target_bots:
                text += f"\n⚠️ 该用户有 {len(target_bots)} 个 Bot 已被标记为 banned。"

            await update.message.reply_text(text, parse_mode="HTML")
            logger.info("管理员 %s 将用户 %s 加入黑名单 (原因: %s)", user_id, target_id, reason)
        else:
            await update.message.reply_text("❌ 添加黑名单失败。")

    elif action in ('del', 'remove', 'rm', 'delete'):
        if len(context.args) < 2:
            await update.message.reply_text(
                "用法：<code>/blacklist del <用户ID></code>",
                parse_mode="HTML"
            )
            return
        try:
            target_id = int(context.args[1])
        except ValueError:
            await update.message.reply_text("❌ 用户ID必须是数字。")
            return

        if remove_from_blacklist(target_id):
            # 恢复该用户的 Bot
            from database import get_db
            conn = get_db()
            try:
                conn.execute(
                    "UPDATE user_bots SET status = 'active' WHERE owner_id = ? AND status = 'banned'",
                    (target_id,)
                )
                conn.commit()
            finally:
                conn.close()

            await update.message.reply_text(
                f"✅ 用户 <code>{target_id}</code> 已从黑名单移除。\n"
                f"💡 如需重新启动其 Bot，请使用 /platform bots 查看，或让用户使用 /botstatus。",
                parse_mode="HTML"
            )
            logger.info("管理员 %s 将用户 %s 从黑名单移除", user_id, target_id)
        else:
            await update.message.reply_text("⚠️ 该用户不在黑名单中。")

    elif action == 'list':
        bl = get_blacklist()
        if not bl:
            await update.message.reply_text("📭 黑名单为空。")
            return

        text = f"🚫 <b>黑名单列表</b> (共 {len(bl)} 人)\n\n"
        for i, entry in enumerate(bl, 1):
            reason = f" — {escape(entry['reason'])}" if entry['reason'] else ""
            text += f"{i}. <code>{entry['user_id']}</code>{reason}\n    📅 {entry['created_at']}\n"

        # 分段发送
        if len(text) > 4000:
            parts = []
            current = ""
            for line in text.split('\n'):
                if len(current) + len(line) + 1 > 3900:
                    parts.append(current)
                    current = line + '\n'
                else:
                    current += line + '\n'
            if current:
                parts.append(current)
            for i, part in enumerate(parts):
                if i == 0:
                    await update.message.reply_text(part, parse_mode="HTML")
                else:
                    await context.bot.send_message(
                        chat_id=update.message.chat_id,
                        text=part,
                        parse_mode="HTML"
                    )
        else:
            await update.message.reply_text(text, parse_mode="HTML")

    elif action == 'check':
        if len(context.args) < 2:
            await update.message.reply_text(
                "用法：<code>/blacklist check <用户ID></code>",
                parse_mode="HTML"
            )
            return
        try:
            target_id = int(context.args[1])
        except ValueError:
            await update.message.reply_text("❌ 用户ID必须是数字。")
            return

        if is_user_blacklisted(target_id):
            # 获取详细信息
            bl_list = get_blacklist()
            entry = next((e for e in bl_list if e['user_id'] == target_id), None)
            text = f"🚫 用户 <code>{target_id}</code> <b>在黑名单中</b>。"
            if entry:
                text += f"\n📅 加入时间: {entry['created_at']}"
                if entry['reason']:
                    text += f"\n📝 原因: {escape(entry['reason'])}"
            # 显示该用户的 Bot
            target_bots = get_user_bots_by_owner(target_id)
            if target_bots:
                text += f"\n\n🤖 该用户的 Bot ({len(target_bots)} 个):"
                for bot in target_bots:
                    text += f"\n  • @{escape(bot['bot_username'])} — {bot['status']}"
            await update.message.reply_text(text, parse_mode="HTML")
        else:
            target_bots = get_user_bots_by_owner(target_id)
            text = f"✅ 用户 <code>{target_id}</code> 不在黑名单中。"
            if target_bots:
                text += f"\n🤖 该用户有 {len(target_bots)} 个 Bot。"
            await update.message.reply_text(text, parse_mode="HTML")

    else:
        await update.message.reply_text(
            "❓ 未知操作。可用操作: <code>add</code>, <code>del</code>, <code>list</code>, <code>check</code>",
            parse_mode="HTML"
        )


# ==================== 导出功能 ====================

async def export_data_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/export 管理员导出数据 - 支持指定Bot导出CSV文件代码"""
    from config import ADMIN_IDS
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 此命令仅限管理员使用。")
        return

    args = context.args or []

    # /export <bot_username> — 导出指定Bot的文件代码CSV
    if args and args[0] not in ('json', 'csv', 'bots', 'code'):
        bot_username = args[0].strip().lstrip('@')
        status_msg = await update.message.reply_text(f"⏳ 正在导出 @{escape(bot_username)} 的文件代码...")

        try:
            files = get_files_by_bot_username(bot_username)
            if not files:
                await status_msg.edit_text(f"📭 Bot @{escape(bot_username)} 没有文件记录。")
                return

            # 生成CSV（逗号分隔）
            output = io.StringIO()
            output.write("code,file_type,file_size,user_id,created_at\n")
            for f in files:
                output.write(f"{f['code']},{f['file_type']},{f['file_size']},{f['user_id']},{f['created_at']}\n")
            export_text = output.getvalue()
            filename = f"{bot_username}_files_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

            bytes_io = io.BytesIO(export_text.encode('utf-8'))
            await context.bot.send_document(
                chat_id=update.message.chat_id,
                document=bytes_io,
                filename=filename,
                caption=f"📁 @{escape(bot_username)} 文件代码导出，共 {len(files)} 条记录。",
            )
            await status_msg.delete()
            logger.info("管理员 %s 导出了 Bot @%s 的文件代码 (%d 条)", user_id, bot_username, len(files))
        except Exception as e:
            await status_msg.edit_text(f"❌ 导出失败: {escape(str(e))}")
            logger.error("导出Bot数据失败: %s", e, exc_info=True)
        return

    # 无参数或 help 显示帮助
    export_format = args[0] if args else 'help'

    if export_format == 'help':
        await update.message.reply_text(
            "📤 <b>数据导出命令</b>\n\n"
            "可用格式:\n"
            "• <code>/export json</code> — 完整 JSON 数据\n"
            "• <code>/export csv</code> — 全部文件列表 CSV\n"
            "• <code>/export bots</code> — Bot 列表 CSV\n"
            "• <code>/export @bot_username</code> — 指定Bot文件代码 CSV",
            parse_mode="HTML"
        )
        return

    status_msg = await update.message.reply_text("⏳ 正在准备导出数据...")

    try:
        data = get_platform_export_data()

        if export_format in ('json', 'code'):
            export_text = json.dumps(data, ensure_ascii=False, indent=2)
            filename = f"platform_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            caption = (
                f"📊 平台数据导出\n\n"
                f"🤖 Bot: {len(data['bots'])} 个\n"
                f"📁 文件: {len(data['files'])} 条\n"
                f"📦 集合: {len(data['collections'])} 个\n"
                f"🚫 黑名单: {len(data['blacklist'])} 人"
            )
        elif export_format == 'csv':
            output = io.StringIO()
            output.write("code,bot_username,file_type,file_size,user_id,created_at\n")
            for f in data['files']:
                output.write(f"{f['code']},{f.get('bot_username', '')},{f['file_type']},{f['file_size']},{f['user_id']},{f['created_at']}\n")
            export_text = output.getvalue()
            filename = f"files_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            caption = f"📁 文件数据导出，共 {len(data['files'])} 条记录。"
        elif export_format == 'bots':
            output = io.StringIO()
            output.write("id,owner_id,bot_id,bot_username,bot_firstname,status,created_at\n")
            for b in data['bots']:
                output.write(f"{b['id']},{b['owner_id']},{b['bot_id']},{b['bot_username']},{b['bot_firstname']},{b['status']},{b['created_at']}\n")
            export_text = output.getvalue()
            filename = f"bots_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            caption = f"🤖 Bot 列表导出，共 {len(data['bots'])} 条记录。"
        else:
            await status_msg.edit_text(
                "❓ 未知格式。\n\n"
                "可用格式:\n"
                "• <code>/export json</code> — 完整 JSON 数据（默认）\n"
                "• <code>/export csv</code> — 全部文件列表 CSV\n"
                "• <code>/export bots</code> — Bot 列表 CSV\n"
                "• <code>/export @bot_username</code> — 指定Bot文件代码 CSV",
                parse_mode="HTML"
            )
            return

        bytes_io = io.BytesIO(export_text.encode('utf-8'))
        await context.bot.send_document(
            chat_id=update.message.chat_id,
            document=bytes_io,
            filename=filename,
            caption=caption,
        )
        await status_msg.delete()
        logger.info("管理员 %s 导出了平台数据 (格式: %s)", user_id, export_format)
    except Exception as e:
        await status_msg.edit_text(f"❌ 导出失败: {escape(str(e))}")
        logger.error("导出数据失败: %s", e, exc_info=True)

# ==================== 黑名单检查 ====================

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/broadcast 管理员广播消息给所有Bot所有者"""
    from config import ADMIN_IDS
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 此命令仅限管理员使用。")
        return

    if not context.args:
        await update.message.reply_text(
            "📢 <b>广播命令</b>\n\n"
            "用法：<code>/broadcast 消息内容</code>\n\n"
            "消息将发送给所有 Bot 所有者。",
            parse_mode="HTML"
        )
        return

    text = " ".join(context.args)
    owner_ids = get_all_owner_ids()
    status_msg = await update.message.reply_text(f"⏳ 正在广播给 {len(owner_ids)} 位用户...")

    success = 0
    fail = 0
    for oid in owner_ids:
        try:
            await context.bot.send_message(chat_id=oid, text=text, parse_mode="HTML")
            success += 1
        except Exception:
            fail += 1

    await status_msg.edit_text(f"✅ 广播完成：成功 {success}/{len(owner_ids)}，失败 {fail}")


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
