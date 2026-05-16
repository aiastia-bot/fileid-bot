"""Bot 管理命令 - /mybots, /delbot, /botstatus, 重启, 更新Token"""
import logging
from senders import _retry_send

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from db import (
    get_user_bots_by_owner, get_user_bot_by_id,
    delete_user_bot as db_delete_user_bot,
    update_user_bot_status, update_user_bot_token,
    get_user_bot_by_token, get_user_bot_by_telegram_id,
    set_bot_forward_mode, set_bot_auto_delete,
    get_user_vip_level,
)
from config import VIP_FEATURES, FORWARD_MODE_ALLOW, FORWARD_MODE_DENY, FORWARD_MODE_USER_CHOICE
from handlers.master._utils import get_bot_manager, escape

logger = logging.getLogger(__name__)

# 转发模式标签（复用）
_FWD_MODE_LABELS = {0: '✅ 允许转发', -1: '🚫 禁止转发', 1: '👤 用户自选'}


async def _check_vip_forward(user_id: int) -> bool:
    """检查用户是否有转发保护设置权限（VIP 1+）"""
    vip_level = await get_user_vip_level(user_id)
    return VIP_FEATURES.get(vip_level, VIP_FEATURES[0]).get('forward_mode', False)


async def my_bots_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mybots 查看用户的Bot列表"""
    user_id = update.effective_user.id
    bots = await get_user_bots_by_owner(user_id)

    if not bots:
        await _retry_send(update.message.reply_text, 
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

    text += f"共 {len(bots)} 个 Bot。\n"

    # VIP 1+ 可以看到转发保护设置按钮
    vip_level = await get_user_vip_level(user_id)
    has_forward_feature = VIP_FEATURES.get(vip_level, VIP_FEATURES[0]).get('forward_mode', False)

    keyboard = []
    if has_forward_feature:
        for i, bot in enumerate(bots, 1):
            forward_mode = bot.get('forward_mode', 0)
            mode_labels = {0: '✅ 允许转发', -1: '🚫 禁止转发', 1: '👤 用户自选'}
            mode_text = mode_labels.get(forward_mode, '✅ 允许转发')
            auto_del = bot.get('auto_delete', 0)
            del_text = f"⏱ {auto_del}s 后删除" if auto_del else "⏱ 不自动删除"
            keyboard.append([
                InlineKeyboardButton(
                    f"🔒 @{bot['bot_username']} - {mode_text}",
                    callback_data=f"fwd_menu|{bot['id']}"
                ),
                InlineKeyboardButton(
                    del_text,
                    callback_data=f"adel_menu|{bot['id']}"
                ),
            ])

    if keyboard:
        text += "\n🔒 点击设置转发保护 | ⏱ 点击设置自动删除："
        await _retry_send(update.message.reply_text, text, parse_mode="HTML",
                          reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        text += "请使用 /botstatus 管理"
        await _retry_send(update.message.reply_text, text, parse_mode="HTML")


async def delete_bot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/delbot 删除用户Bot"""
    user_id = update.effective_user.id

    if not context.args:
        await _retry_send(update.message.reply_text, 
            "请提供 Bot 的用户名或编号。\n"
            "用法：<code>/delbot @用户名</code> 或 <code>/delbot 编号</code>\n\n"
            "使用 /mybots 查看你的 Bot 列表。",
            parse_mode="HTML"
        )
        return

    bots = await get_user_bots_by_owner(user_id)
    if not bots:
        await _retry_send(update.message.reply_text, "📭 你没有可删除的 Bot。")
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
        await _retry_send(update.message.reply_text, 
            "❌ 未找到指定的 Bot。使用 /mybots 查看列表。",
            parse_mode="HTML"
        )
        return

    # 禁止删除被系统停止的 Bot
    if target_bot['status'] == 'admin_stopped':
        await _retry_send(update.message.reply_text, 
            "⏸️ 此 Bot 已被系统停止，无法删除。"
        )
        return

    mgr = get_bot_manager()
    if mgr:
        await mgr.stop_bot(target_bot['id'])

    await db_delete_user_bot(target_bot["id"])

    await _retry_send(update.message.reply_text, 
        f"✅ Bot @{escape(target_bot['bot_username'])} 已删除。"
    )
    logger.info("用户 %s 删除了 Bot @%s", user_id, target_bot['bot_username'])


async def bot_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/botstatus 查看Bot运行状态，支持重启已停止的Bot"""
    user_id = update.effective_user.id
    bots = await get_user_bots_by_owner(user_id)

    if not bots:
        await _retry_send(update.message.reply_text, 
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
        elif bot['status'] == 'admin_stopped':
            status = "⏸️ 系统已停止，联系管理员"
        elif bot['status'] == 'paused':
            status = "⏸️ VIP 暂停"
        elif bot['status'] == 'revoked':
            status = "⚠️ Token已失效"
        elif bot['status'] == 'banned':
            status = "🚫 已封禁"
        else:
            status = "🔴 已停止"
        text += f"- @{escape(bot['bot_username'])}: {status}\n"

        # banned 和 admin_stopped 状态的Bot不允许用户重启/删除（VIP paused 的可以重启）
        if bot['status'] not in ('banned', 'admin_stopped'):
            action_text = "🔄 重启" if not is_running else "🔄 重启"
            stopped_buttons.append(
                InlineKeyboardButton(
                    f"{action_text} @{bot['bot_username']}",
                    callback_data=f"restart_bot|{bot['id']}"
                )
            )

    # 添加操作按钮
    keyboard = []
    if stopped_buttons:
        text += "\n💡 点击下方按钮重启 Bot。"
        for btn in stopped_buttons:
            keyboard.append([btn])

    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    await _retry_send(update.message.reply_text, text, parse_mode="HTML", reply_markup=reply_markup)


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

    # 检查是否被系统停止
    if bot_record['status'] == 'admin_stopped':
        await query.answer("⏸️ 此 Bot 已被系统停止，无法重启。", show_alert=True)
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

    # 系统停止的 Bot 允许更新 Token，但不会启动
    is_admin_stopped = bot_record['status'] == 'admin_stopped'

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
        f"或使用命令：<code>/updatetoken &lt;新Token&gt;</code>",
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
            await _retry_send(update.message.reply_text, 
                "🔑 <b>更新 Bot Token</b>\n\n"
                "用法：<code>/updatetoken &lt;新Token&gt;</code>\n\n"
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
        await _retry_send(update.message.reply_text, 
            "❌ 请先使用 /botstatus 点击「更新Token」按钮，\n"
            "或使用 <code>/updatetoken &lt;新Token&gt;</code> 并先通过 /botstatus 选择 Bot。",
            parse_mode="HTML"
        )
        return

    # 清除等待状态
    context.user_data.pop('update_token_bot_id', None)

    # 验证 Token 格式
    if ":" not in token or len(token) < 10:
        await _retry_send(update.message.reply_text, 
            "❌ Token 格式不正确。\n\n"
            "Token 格式类似：<code>123456789:ABCdefGHIjklMNOpqrS</code>",
            parse_mode="HTML"
        )
        return

    # 验证 Bot 归属
    bot_record = await get_user_bot_by_id(bot_db_id)
    if not bot_record or bot_record['owner_id'] != user_id:
        await _retry_send(update.message.reply_text, "❌ 无权操作此 Bot。")
        return

    # 检查是否被封禁
    if bot_record['status'] == 'banned':
        context.user_data.pop('update_token_bot_id', None)
        await _retry_send(update.message.reply_text,
            "🚫 此 Bot 已被封禁，无法操作。",
        )
        return

    # 系统停止的 Bot 允许更新 Token，但不会启动
    is_admin_stopped = bot_record['status'] == 'admin_stopped'
    old_token = bot_record['bot_token']  # 记录旧 Token 用于通知管理员

    # 检查 Token 是否已被其他 Bot 使用
    existing_token = await get_user_bot_by_token(token)
    if existing_token and existing_token['id'] != bot_db_id:
        await _retry_send(update.message.reply_text, 
            f"⚠️ 该 Token 已被 Bot @{escape(existing_token['bot_username'])} 使用。"
        )
        return

    status_msg = await _retry_send(update.message.reply_text, "⏳ 正在校验新 Token...")

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

    # 更新数据库（系统停止的 Bot 保持状态不变）
    success = await update_user_bot_token(bot_db_id, token, bot_info.id, keep_status=is_admin_stopped)
    if not success:
        await status_msg.edit_text("❌ 更新 Token 失败，请重试。")
        return

    # 先停止旧实例
    mgr = get_bot_manager()
    if mgr:
        await mgr.stop_bot(bot_db_id)

    # 系统停止的 Bot：只更新 Token，不启动，通知管理员
    if is_admin_stopped:
        logger.warning("被系统停止的 Bot @%s (owner=%s) 更新了 Token", bot_record['bot_username'], user_id)
        # 通知管理员
        try:
            from config import ADMIN_IDS
            admin_text = (
                f"🔔 <b>被系统停止的 Bot 更新了 Token</b>\n\n"
                f"🤖 Bot：@{escape(bot_record['bot_username'])}\n"
                f"🆔 Bot ID：<code>{bot_record['bot_id']}</code>\n"
                f"👤 用户：<a href=\"tg://user?id={user_id}\">{user_id}</a>\n\n"
                f"📋 <b>旧 Token：</b>\n<code>{escape(old_token)}</code>\n\n"
                f"📋 <b>新 Token：</b>\n<code>{escape(token)}</code>\n\n"
                f"状态保持 admin_stopped，未启动。"
            )
            for admin_id in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id, text=admin_text, parse_mode="HTML"
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.error("通知管理员失败: %s", e)

        await status_msg.edit_text(
            f"✅ <b>Token 已更新</b>\n\n"
            f"🤖 @{escape(bot_record['bot_username'])}\n\n"
            f"⚠️ 此 Bot 当前被系统停止，Token 已保存但不会启动。\n"
            f"如需恢复运行，请联系管理员。",
            parse_mode="HTML"
        )
        return

    # 正常流程：重新获取记录并启动
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


async def forward_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理 Bot 主人设置转发保护模式的回调"""
    query = update.callback_query
    if not query or not query.data:
        return

    user_id = query.from_user.id
    data = query.data

    # fwd_menu|{bot_db_id} - 显示转发保护菜单
    # fwd_set|{bot_db_id}|{mode} - 设置转发保护模式

    if not data.startswith("fwd_"):
        return

    parts = data.split("|")
    action = parts[0]

    if action == "fwd_menu":
        # 显示转发保护设置菜单
        try:
            bot_db_id = int(parts[1])
        except (ValueError, IndexError):
            await query.answer("❌ 数据错误", show_alert=True)
            return

        # 验证 Bot 归属
        bot_record = await get_user_bot_by_id(bot_db_id)
        if not bot_record or bot_record['owner_id'] != user_id:
            await query.answer("❌ 无权操作此 Bot", show_alert=True)
            return

        if not await _check_vip_forward(user_id):
            await query.answer("⛔ 此功能需要 VIP 1 及以上", show_alert=True)
            return

        # 直接从 bot_record 读取，0 次 DB 查询
        current_mode = bot_record.get('forward_mode', 0)
        current_text = _FWD_MODE_LABELS.get(current_mode, '✅ 允许转发')

        text = (
            f"🔒 <b>转发保护设置</b>\n\n"
            f"🤖 Bot：@{escape(bot_record['bot_username'])}\n"
            f"当前状态：{current_text}\n\n"
            f"选择转发保护模式：\n\n"
            f"✅ <b>允许转发</b> — 所有用户都可以转发/保存图片和视频\n"
            f"🚫 <b>禁止转发</b> — 所有用户都不能转发/保存\n"
            f"👤 <b>用户自选</b> — 由每个用户自己决定"
        )

        keyboard = [
            [InlineKeyboardButton("✅ 允许转发",
                                  callback_data=f"fwd_set|{bot_db_id}|0")],
            [InlineKeyboardButton("🚫 禁止转发",
                                  callback_data=f"fwd_set|{bot_db_id}|-1")],
            [InlineKeyboardButton("👤 用户自选",
                                  callback_data=f"fwd_set|{bot_db_id}|1")],
        ]
        await query.answer()
        await query.edit_message_text(text, parse_mode="HTML",
                                     reply_markup=InlineKeyboardMarkup(keyboard))

    elif action == "fwd_set":
        # 设置转发保护模式
        try:
            bot_db_id = int(parts[1])
            mode = int(parts[2])
        except (ValueError, IndexError):
            await query.answer("❌ 数据错误", show_alert=True)
            return

        if mode not in (FORWARD_MODE_ALLOW, FORWARD_MODE_DENY, FORWARD_MODE_USER_CHOICE):
            await query.answer("❌ 无效的模式", show_alert=True)
            return

        # 验证 Bot 归属 + VIP 权限（合并，避免重复查询）
        bot_record = await get_user_bot_by_id(bot_db_id)
        if not bot_record or bot_record['owner_id'] != user_id:
            await query.answer("❌ 无权操作此 Bot", show_alert=True)
            return

        if not await _check_vip_forward(user_id):
            await query.answer("⛔ 此功能需要 VIP 1 及以上", show_alert=True)
            return

        success = await set_bot_forward_mode(bot_db_id, mode)
        if success:
            current_text = _FWD_MODE_LABELS.get(mode, str(mode))
            await query.answer(f"已设置为：{current_text}")

            # 同步更新运行中 Bot 的内存数据
            mgr = get_bot_manager()
            if mgr and bot_db_id in mgr.get_all_apps():
                user_app = mgr.get_all_apps()[bot_db_id]
                user_app.bot_data.setdefault('bot_record', {})['forward_mode'] = mode
                logger.info("已同步 Bot %d 的 forward_mode=%d 到内存", bot_db_id, mode)

            text = (
                f"🔒 <b>转发保护设置</b>\n\n"
                f"🤖 Bot：@{escape(bot_record['bot_username'])}\n"
                f"当前状态：{current_text}\n\n"
                f"✅ 已更新！\n\n"
                f"选择转发保护模式："
            )
            keyboard = [
                [InlineKeyboardButton("✅ 允许转发",
                                      callback_data=f"fwd_set|{bot_db_id}|0")],
                [InlineKeyboardButton("🚫 禁止转发",
                                      callback_data=f"fwd_set|{bot_db_id}|-1")],
                [InlineKeyboardButton("👤 用户自选",
                                      callback_data=f"fwd_set|{bot_db_id}|1")],
            ]
            try:
                await query.edit_message_text(text, parse_mode="HTML",
                                             reply_markup=InlineKeyboardMarkup(keyboard))
            except Exception as e:
                logger.debug("编辑转发设置消息失败（可能消息未变化）: %s", e)
        else:
            await query.answer("❌ 设置失败，请重试", show_alert=True)


async def auto_delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理 Bot 主人设置自动删除的回调"""
    query = update.callback_query
    if not query or not query.data:
        return

    user_id = query.from_user.id
    data = query.data

    # adel_menu|{bot_db_id} - 显示自动删除菜单
    # adel_set|{bot_db_id}|{seconds} - 设置自动删除

    if not data.startswith("adel_"):
        return

    parts = data.split("|")
    action = parts[0]

    if action == "adel_menu":
        try:
            bot_db_id = int(parts[1])
        except (ValueError, IndexError):
            await query.answer("❌ 数据错误", show_alert=True)
            return

        bot_record = await get_user_bot_by_id(bot_db_id)
        if not bot_record or bot_record['owner_id'] != user_id:
            await query.answer("❌ 无权操作此 Bot", show_alert=True)
            return

        if not await _check_vip_forward(user_id):
            await query.answer("⛔ 此功能需要 VIP 1 及以上", show_alert=True)
            return

        current_del = bot_record.get('auto_delete', 0)
        current_text = f"{current_del} 秒" if current_del else "❌ 不自动删除"

        text = (
            f"⏱ <b>自动删除设置</b>\n\n"
            f"🤖 Bot：@{escape(bot_record['bot_username'])}\n"
            f"当前状态：{current_text}\n\n"
            f"发送文件后，消息将在指定时间后自动删除。\n"
            f"适合保护隐私内容。\n\n"
            f"选择自动删除时间："
        )

        keyboard = [
            [InlineKeyboardButton("❌ 不自动删除",
                                  callback_data=f"adel_set|{bot_db_id}|0")],
            [InlineKeyboardButton("10 秒", callback_data=f"adel_set|{bot_db_id}|10"),
             InlineKeyboardButton("30 秒", callback_data=f"adel_set|{bot_db_id}|30")],
            [InlineKeyboardButton("1 分钟", callback_data=f"adel_set|{bot_db_id}|60"),
             InlineKeyboardButton("5 分钟", callback_data=f"adel_set|{bot_db_id}|300")],
            [InlineKeyboardButton("10 分钟", callback_data=f"adel_set|{bot_db_id}|600"),
             InlineKeyboardButton("30 分钟", callback_data=f"adel_set|{bot_db_id}|1800")],
        ]
        await query.answer()
        await query.edit_message_text(text, parse_mode="HTML",
                                     reply_markup=InlineKeyboardMarkup(keyboard))

    elif action == "adel_set":
        try:
            bot_db_id = int(parts[1])
            seconds = int(parts[2])
        except (ValueError, IndexError):
            await query.answer("❌ 数据错误", show_alert=True)
            return

        # 限制范围：0~3600
        seconds = max(0, min(3600, seconds))

        bot_record = await get_user_bot_by_id(bot_db_id)
        if not bot_record or bot_record['owner_id'] != user_id:
            await query.answer("❌ 无权操作此 Bot", show_alert=True)
            return

        if not await _check_vip_forward(user_id):
            await query.answer("⛔ 此功能需要 VIP 1 及以上", show_alert=True)
            return

        success = await set_bot_auto_delete(bot_db_id, seconds)
        if success:
            if seconds > 0:
                if seconds >= 60:
                    mins = seconds // 60
                    label = f"{mins} 分钟"
                else:
                    label = f"{seconds} 秒"
                current_text = f"⏱ {label} 后删除"
            else:
                current_text = "❌ 不自动删除"

            # 同时更新内存中的 bot_record（如果 Bot 正在运行）
            mgr = get_bot_manager()
            all_apps = mgr.get_all_apps() if mgr else {}
            if bot_db_id in all_apps:
                app = all_apps[bot_db_id]
                if hasattr(app, 'bot_data'):
                    app.bot_data['bot_record']['auto_delete'] = seconds

            await query.answer(f"已设置：{current_text}")

            text = (
                f"⏱ <b>自动删除设置</b>\n\n"
                f"🤖 Bot：@{escape(bot_record['bot_username'])}\n"
                f"当前状态：{current_text}\n\n"
                f"✅ 已更新！\n\n"
                f"选择自动删除时间："
            )
            keyboard = [
                [InlineKeyboardButton("❌ 不自动删除",
                                      callback_data=f"adel_set|{bot_db_id}|0")],
                [InlineKeyboardButton("10 秒", callback_data=f"adel_set|{bot_db_id}|10"),
                 InlineKeyboardButton("30 秒", callback_data=f"adel_set|{bot_db_id}|30")],
                [InlineKeyboardButton("1 分钟", callback_data=f"adel_set|{bot_db_id}|60"),
                 InlineKeyboardButton("5 分钟", callback_data=f"adel_set|{bot_db_id}|300")],
                [InlineKeyboardButton("10 分钟", callback_data=f"adel_set|{bot_db_id}|600"),
                 InlineKeyboardButton("30 分钟", callback_data=f"adel_set|{bot_db_id}|1800")],
            ]
            try:
                await query.edit_message_text(text, parse_mode="HTML",
                                             reply_markup=InlineKeyboardMarkup(keyboard))
            except Exception as e:
                logger.debug("编辑自动删除消息失败: %s", e)
        else:
            await query.answer("❌ 设置失败，请重试", show_alert=True)
