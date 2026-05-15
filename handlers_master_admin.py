"""管理员命令 - /platform, /export, /startbot, /stopbot, /broadcast"""
import html
import io
import json
import logging
from datetime import datetime
from senders import _retry_send

from telegram import Update
from telegram.ext import ContextTypes

from database import (
    get_user_bot_by_id,
    get_user_bot_by_username,
    update_user_bot_status,
    get_all_owner_ids,
    get_platform_stats,
    get_platform_bot_details, get_platform_export_data,
    get_active_bot_files,
    get_files_by_bot_db_id,
    get_blacklist_count,
    get_platform_setting, set_platform_setting,
)

logger = logging.getLogger(__name__)


def get_bot_manager():
    """获取全局 BotManager 实例"""
    import __main__
    return getattr(__main__, 'bot_manager', None)


def escape(text: str) -> str:
    """HTML 转义"""
    return html.escape(str(text), quote=False)


async def platform_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/platform 管理员查看平台统计和 Bot 详情"""
    from config import ADMIN_IDS
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await _retry_send(update.message.reply_text, "⛔ 此命令仅限管理员使用。")
        return

    # 检查是否有参数: /platform bots [revoked|all]
    args = context.args or []
    show_bots = args and args[0] in ('bots', 'bot', 'detail', 'details')
    bot_status = 'active'
    if show_bots and len(args) > 1:
        bot_status = args[1] if args[1] in ('revoked', 'banned', 'all') else 'active'

    stats = await get_platform_stats()
    mgr = get_bot_manager()
    running = mgr.active_count if mgr else 0
    bl_count = await get_blacklist_count()

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
        bot_details = await get_platform_bot_details(status=bot_status)
        # active 状态下隐藏文件数为 0 的 Bot，其他状态全部显示
        if bot_status == 'active' and bot_details:
            bot_details = [b for b in bot_details if b['file_count'] > 0]
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
                    f"   🆔 Bot ID: <code>{bot['bot_id']}</code> | DB ID: <code>{bot['id']}</code>\n"
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
            f"\n💡 使用 <code>/platform bots</code> 查看 Active Bot\n"
            f"使用 <code>/platform bots revoked</code> 查看 Revoked Bot\n"
            f"使用 <code>/platform bots all</code> 查看全部"
        )

    # Telegram 消息长度限制为 4096 字符，需要分段发送
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
                await _retry_send(update.message.reply_text, part, parse_mode="HTML")
            else:
                await _retry_send(context.bot.send_message, 
                    chat_id=update.message.chat_id,
                    text=part,
                    parse_mode="HTML"
                )
    else:
        await _retry_send(update.message.reply_text, text, parse_mode="HTML")


# ==================== 导出功能 ====================

async def export_data_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/export 管理员导出数据 - 支持指定Bot导出CSV文件代码"""
    from config import ADMIN_IDS
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await _retry_send(update.message.reply_text, "⛔ 此命令仅限管理员使用。")
        return

    args = context.args or []

    # /export txt @bot_username — 纯文本导出指定Bot的code列表
    if args and len(args) >= 2 and args[0] == 'txt':
        export_arg = args[1].strip().lstrip('@')
        status_msg = await _retry_send(update.message.reply_text, "⏳ 正在导出 TXT...")

        try:
            bot_record = None
            try:
                bot_record = await get_user_bot_by_id(int(export_arg))
            except ValueError:
                pass
            if not bot_record:
                bot_record = await get_user_bot_by_username(export_arg)
            if not bot_record:
                await status_msg.edit_text(f"❌ 未找到 Bot：{escape(export_arg)}")
                return
            bot_db_id_export = bot_record['id']
            bot_username = bot_record['bot_username']
            files = await get_files_by_bot_db_id(bot_db_id_export)
            if not files:
                await status_msg.edit_text(f"📭 Bot @{escape(bot_username)} (ID:{bot_db_id_export}) 没有文件记录。")
                return

            # 生成纯文本（每行一个code）
            output = io.StringIO()
            for f in files:
                output.write(f"{f['code']}\n")
            export_text = output.getvalue()
            filename = f"{bot_username}_{bot_db_id_export}_codes_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

            bytes_io = io.BytesIO(export_text.encode('utf-8'))
            await _retry_send(context.bot.send_document, 
                chat_id=update.message.chat_id,
                document=bytes_io,
                filename=filename,
                caption=f"📄 @{escape(bot_username)} (ID:{bot_db_id_export}) 纯文本 code 导出，共 {len(files)} 条。",
            )
            await status_msg.delete()
            logger.info("管理员 %s 导出了 Bot @%s (ID:%d) 的 TXT code (%d 条)", user_id, bot_username, bot_db_id_export, len(files))
        except Exception as e:
            await status_msg.edit_text(f"❌ 导出失败: {escape(str(e))}")
            logger.error("导出Bot TXT数据失败: %s", e, exc_info=True)
        return

    # /export <bot_username|db_id> — 导出指定Bot的文件代码CSV（支持同名Bot）
    if args and args[0] not in ('json', 'csv', 'bots', 'code', 'txt'):
        export_arg = args[0].strip().lstrip('@')
        status_msg = await _retry_send(update.message.reply_text, "⏳ 正在导出...")

        try:
            bot_record = None
            try:
                bot_record = await get_user_bot_by_id(int(export_arg))
            except ValueError:
                pass
            if not bot_record:
                bot_record = await get_user_bot_by_username(export_arg)
            if not bot_record:
                await status_msg.edit_text(f"❌ 未找到 Bot：{escape(export_arg)}")
                return
            bot_db_id_export = bot_record['id']
            bot_username = bot_record['bot_username']
            files = await get_files_by_bot_db_id(bot_db_id_export)
            if not files:
                await status_msg.edit_text(f"📭 Bot @{escape(bot_username)} (ID:{bot_db_id_export}) 没有文件记录。")
                return

            # 生成CSV（逗号分隔）
            output = io.StringIO()
            output.write("code,file_type,file_size,user_id,created_at\n")
            for f in files:
                output.write(f"{f['code']},{f['file_type']},{f['file_size']},{f['user_id']},{f['created_at']}\n")
            export_text = output.getvalue()
            filename = f"{bot_username}_{bot_db_id_export}_files_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

            bytes_io = io.BytesIO(export_text.encode('utf-8'))
            await _retry_send(context.bot.send_document, 
                chat_id=update.message.chat_id,
                document=bytes_io,
                filename=filename,
                caption=f"📁 @{escape(bot_username)} (ID:{bot_db_id_export}) 文件代码导出，共 {len(files)} 条记录。",
            )
            await status_msg.delete()
            logger.info("管理员 %s 导出了 Bot @%s (ID:%d) 的文件代码 (%d 条)", user_id, bot_username, bot_db_id_export, len(files))
        except Exception as e:
            await status_msg.edit_text(f"❌ 导出失败: {escape(str(e))}")
            logger.error("导出Bot数据失败: %s", e, exc_info=True)
        return

    # 无参数或 help 显示帮助
    export_format = args[0] if args else 'help'

    if export_format == 'help':
        await _retry_send(update.message.reply_text, 
            "📤 <b>数据导出命令</b>\n\n"
            "可用格式:\n"
            "• <code>/export json</code> — 完整 JSON 数据\n"
            "• <code>/export csv [日期]</code> — 活跃Bot文件 CSV（如 /export csv 2026-05-05）\n"
            "• <code>/export bots</code> — Bot 列表 CSV\n"
            "• <code>/export @bot_username</code> — 指定Bot文件代码 CSV\n"
            "• <code>/export txt @bot_username</code> — 指定Bot纯文本 code 列表",
            parse_mode="HTML"
        )
        return

    status_msg = await _retry_send(update.message.reply_text, "⏳ 正在准备导出数据...")

    try:
        data = await get_platform_export_data()

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
            # 支持日期参数: /export csv 2026-05-05
            since_date = args[1] if len(args) > 1 else None
            files = await get_active_bot_files(since_date)
            output = io.StringIO()
            output.write("code,bot_username,file_type,file_size,user_id,created_at\n")
            for f in files:
                output.write(f"{f['code']},{f['bot_username']},{f['file_type']},{f['file_size']},{f['user_id']},{f['created_at']}\n")
            export_text = output.getvalue()
            date_info = f"（{since_date} 起）" if since_date else ""
            filename = f"active_files_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            caption = f"📁 活跃Bot文件导出{date_info}，共 {len(files)} 条记录。"
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
                "• <code>/export csv [日期]</code> — 活跃Bot文件 CSV（如 /export csv 2026-05-05）\n"
                "• <code>/export bots</code> — Bot 列表 CSV\n"
                "• <code>/export @bot_username</code> — 指定Bot文件代码 CSV",
                parse_mode="HTML"
            )
            return

        bytes_io = io.BytesIO(export_text.encode('utf-8'))
        await _retry_send(context.bot.send_document, 
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


# ==================== 管理员重启/启动Bot ====================

async def _find_bot_record(arg: str):
    """辅助函数：按数据库ID或用户名查找Bot记录"""
    bot_record = None
    try:
        bot_record = await get_user_bot_by_id(int(arg))
    except ValueError:
        pass
    if not bot_record:
        username = arg.lstrip('@')
        bot_record = await get_user_bot_by_username(username)
    return bot_record


async def start_bot_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/startbot 管理员重启/启动指定Bot（始终先停止再启动）"""
    from config import ADMIN_IDS
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await _retry_send(update.message.reply_text, "⛔ 此命令仅限管理员使用。")
        return

    if not context.args:
        await _retry_send(update.message.reply_text, 
            "🔄 <b>重启/启动 Bot</b>\n\n"
            "用法：<code>/startbot @用户名</code> 或 <code>/startbot 数据库ID</code>\n\n"
            "无论 Bot 是否在运行，都会先停止再重新启动。\n"
            "可用于重启运行中的 Bot，或启动已停止/revoked 的 Bot。",
            parse_mode="HTML"
        )
        return

    arg = context.args[0].strip()
    bot_record = await _find_bot_record(arg)

    if not bot_record:
        await _retry_send(update.message.reply_text, f"❌ 未找到 Bot：{escape(arg)}")
        return

    # 检查是否被封禁
    if bot_record['status'] == 'banned':
        await _retry_send(update.message.reply_text, 
            f"🚫 Bot @{escape(bot_record['bot_username'])} 已被封禁，无法启动。\n"
            f"使用 /blacklist del {bot_record['owner_id']} 解除封禁。"
        )
        return

    mgr = get_bot_manager()
    action_text = "重启" if mgr_running_check(bot_record['id']) else "启动"
    status_msg = await _retry_send(update.message.reply_text, 
        f"⏳ 正在{action_text} @{escape(bot_record['bot_username'])}..."
    )

    # 更新数据库状态为 active（包括从 compromised 恢复）
    await update_user_bot_status(bot_record['id'], 'active')

    # 先停止旧实例（无论是否在运行都尝试停止）
    if mgr:
        await mgr.stop_bot(bot_record['id'])

    # 重新获取记录并启动
    bot_record = await get_user_bot_by_id(bot_record['id'])
    success = await mgr.start_bot(bot_record) if mgr else False

    if success:
        await status_msg.edit_text(
            f"✅ <b>Bot {action_text}成功！</b>\n\n"
            f"🤖 @{escape(bot_record['bot_username'])}\n"
            f"🆔 Bot ID：<code>{bot_record['bot_id']}</code>\n"
            f"👤 所有者：<code>{bot_record['owner_id']}</code>",
            parse_mode="HTML"
        )
        logger.info("管理员 %s %s了 Bot @%s", user_id, action_text, bot_record['bot_username'])
    else:
        await status_msg.edit_text(
            f"❌ <b>{action_text}失败</b>\n\n"
            f"Bot @{escape(bot_record['bot_username'])} 启动失败。\n"
            f"可能原因：Token 已失效。\n\n"
            f"💡 让用户使用 /updatetoken 更新 Token，\n"
            f"或使用 /delbot 删除后重建。",
            parse_mode="HTML"
        )


def mgr_running_check(bot_db_id: int) -> bool:
    """检查 Bot 是否在 BotManager 中运行"""
    mgr = get_bot_manager()
    return mgr is not None and bot_db_id in mgr.get_all_apps()


# ==================== 管理员停止Bot ====================

async def stop_bot_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stopbot 管理员停止指定Bot"""
    from config import ADMIN_IDS
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await _retry_send(update.message.reply_text, "⛔ 此命令仅限管理员使用。")
        return

    if not context.args:
        await _retry_send(update.message.reply_text, 
            "🛑 <b>停止指定 Bot</b>\n\n"
            "用法：<code>/stopbot @用户名</code> 或 <code>/stopbot 数据库ID</code>\n\n"
            "停止后的 Bot 只有管理员可通过 /startbot 重新启动。\n"
            "使用 /platform bots 查看所有 Bot。",
            parse_mode="HTML"
        )
        return

    arg = context.args[0].strip()
    bot_record = await _find_bot_record(arg)

    if not bot_record:
        await _retry_send(update.message.reply_text, f"❌ 未找到 Bot：{escape(arg)}")
        return

    mgr = get_bot_manager()
    was_running = mgr and bot_record['id'] in mgr.get_all_apps()

    if not was_running:
        # 即使没在运行，也更新数据库状态为 paused
        if bot_record['status'] == 'active':
            await update_user_bot_status(bot_record['id'], 'paused')
        await _retry_send(update.message.reply_text, 
            f"ℹ️ Bot @{escape(bot_record['bot_username'])} 当前未在运行。数据库状态已更新。"
        )
        return

    # 停止运行中的实例
    success = False
    if mgr:
        success = await mgr.stop_bot(bot_record['id'])

    # 更新数据库状态为 paused
    await update_user_bot_status(bot_record['id'], 'paused')

    if success:
        await _retry_send(update.message.reply_text, 
            f"✅ <b>Bot 已停止</b>\n\n"
            f"🤖 @{escape(bot_record['bot_username'])}\n"
            f"🆔 Bot ID：<code>{bot_record['bot_id']}</code>\n"
            f"👤 所有者：<code>{bot_record['owner_id']}</code>\n\n"
            f"💡 使用 <code>/startbot @{escape(bot_record['bot_username'])}</code> 可重新启动。",
            parse_mode="HTML"
        )
        logger.info("管理员 %s 停止了 Bot @%s", user_id, bot_record['bot_username'])
    else:
        await _retry_send(update.message.reply_text, 
            f"⚠️ 停止 Bot @{escape(bot_record['bot_username'])} 时出现问题，但数据库状态已更新。"
        )




# ==================== 广播消息 ====================

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/broadcast 管理员广播消息
    - /broadcast 消息内容 — 主Bot广播给所有活跃Bot所有者
    - /broadcast user 消息内容 — 所有活跃用户Bot分别给各自的用户群发消息
    """
    from config import ADMIN_IDS
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await _retry_send(update.message.reply_text, "⛔ 此命令仅限管理员使用。")
        return

    if not context.args:
        await _retry_send(update.message.reply_text, 
            "📢 <b>广播命令</b>\n\n"
            "用法：\n"
            "• <code>/broadcast 消息内容</code> — 主Bot广播给所有活跃Bot所有者\n"
            "• <code>/broadcast user 消息内容</code> — 所有活跃用户Bot分别给各自的用户群发消息\n\n"
            "💡 <code>/broadcast user</code> 会通过每个运行中的用户Bot，给该用户Bot的主人进行发送消息",
            parse_mode="HTML"
        )
        return

    # /broadcast user <message> — 所有活跃用户Bot给各自用户群发
    if context.args[0].lower() == 'user':
        await _broadcast_via_user_bots(update, context)
        return

    # 默认：主Bot广播给所有Bot所有者
    text = " ".join(context.args)
    # 只发给 active 状态 Bot 的所有者
    owner_ids = await get_all_owner_ids(status='active')
    if not owner_ids:
        await _retry_send(update.message.reply_text, "📭 没有活跃 Bot 的所有者可广播。")
        return
    status_msg = await _retry_send(update.message.reply_text, f"⏳ 正在广播给 {len(owner_ids)} 位活跃用户...")

    success = 0
    fail = 0
    for oid in owner_ids:
        try:
            await _retry_send(context.bot.send_message, chat_id=oid, text=text, parse_mode="HTML")
            success += 1
        except Exception:
            fail += 1

    await status_msg.edit_text(f"✅ 广播完成：成功 {success}/{len(owner_ids)}，失败 {fail}")


async def _broadcast_via_user_bots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/broadcast user 子命令 — 通过每个运行中的用户Bot给该Bot的主人发消息"""
    args = context.args[1:]  # 去掉 'user'

    if not args:
        await _retry_send(update.message.reply_text,
            "❌ 请输入消息内容。\n\n"
            "用法：<code>/broadcast user 消息内容</code>\n\n"
            "消息将通过每个运行中的用户Bot，发送给该Bot的主人。",
            parse_mode="HTML"
        )
        return

    message_text = " ".join(args)

    mgr = get_bot_manager()
    if not mgr or not mgr.get_all_apps():
        await _retry_send(update.message.reply_text, "❌ 当前没有运行中的用户 Bot。")
        return

    apps = mgr.get_all_apps()
    status_msg = await _retry_send(update.message.reply_text,
        f"⏳ 正在通过 {len(apps)} 个用户Bot给各自的主人发送消息...")

    total_success = 0
    total_fail = 0
    bot_results = []
    sent_owners = set()  # 去重：每个主人只发一次

    for bot_db_id, app in apps.items():
        bot_username = app.bot.username if app.bot else f"Bot#{bot_db_id}"

        # 查询该Bot的主人ID
        bot_record = await get_user_bot_by_id(bot_db_id)
        if not bot_record:
            bot_results.append(f"❓ @{escape(bot_username)}: 未找到记录")
            continue

        owner_id = bot_record['owner_id']

        # 去重：同一个主人只尝试一次（无论成功失败）
        if owner_id in sent_owners:
            continue

        # 通过该用户Bot给主人发消息
        sent_owners.add(owner_id)  # 标记已处理，避免其他Bot重复尝试
        try:
            await _retry_send(
                app.bot.send_message,
                chat_id=owner_id,
                text=message_text,
                parse_mode="HTML"
            )
            total_success += 1
            bot_results.append(f"✅ @{escape(bot_username)} → 主人 <code>{owner_id}</code>")
        except Exception as e:
            total_fail += 1
            bot_results.append(f"❌ @{escape(bot_username)} → 主人 <code>{owner_id}</code>: {escape(str(e)[:60])}")

    # 汇总结果
    result_lines = "\n".join(bot_results)
    await status_msg.edit_text(
        f"✅ <b>用户Bot主人通知完成</b>\n\n"
        f"📊 总计：成功 {total_success}，失败 {total_fail}\n\n"
        f"{result_lines}",
        parse_mode="HTML"
    )


# ==================== 群组链接管理 ====================

async def set_group_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setgroup 管理员管理沟通群组链接"""
    from config import ADMIN_IDS
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await _retry_send(update.message.reply_text, "⛔ 此命令仅限管理员使用。")
        return

    import json

    args = context.args or []

    if not args:
        # 显示当前群组列表和帮助
        groups_json = await get_platform_setting('chat_groups', '')
        groups = json.loads(groups_json) if groups_json else []

        text = "🔗 <b>沟通群组管理</b>\n\n"
        if groups:
            text += "<b>当前群组：</b>\n"
            for i, g in enumerate(groups, 1):
                text += f"  {i}\. <a href=\"{escape(g.get('url', ''))}\">{escape(g.get('name', ''))}</a>\n"
        else:
            text += "📭 暂无群组。\n"

        text += (
            "\n<b>用法：</b>\n"
            "• <code>/setgroup add 群组名称 URL</code> — 添加群组\n"
            "• <code>/setgroup del 序号</code> — 删除群组\n"
            "• <code>/setgroup clear</code> — 清空所有群组\n\n"
            "💡 群组将以超链接形式显示在用户 Bot 的 /start 消息中。"
        )
        await _retry_send(update.message.reply_text, text, parse_mode="HTML", disable_web_page_preview=True)
        return

    action = args[0].lower()

    if action == 'add':
        if len(args) < 3:
            await _retry_send(update.message.reply_text,
                "❌ 格式错误。\n用法：<code>/setgroup add 群组名称 URL</code>",
                parse_mode="HTML"
            )
            return

        # 支持：/setgroup add 群组名称 URL
        # URL 是最后一个参数，中间的都拼成名称
        url = args[-1]
        name = ' '.join(args[1:-1])

        if not name or not url:
            await _retry_send(update.message.reply_text,
                "❌ 群组名称和 URL 不能为空。",
                parse_mode="HTML"
            )
            return

        groups_json = await get_platform_setting('chat_groups', '')
        groups = json.loads(groups_json) if groups_json else []

        groups.append({'name': name, 'url': url})
        await set_platform_setting('chat_groups', json.dumps(groups, ensure_ascii=False))

        await _retry_send(update.message.reply_text,
            f"✅ 已添加群组：<a href=\"{escape(url)}\">{escape(name)}</a>",
            parse_mode="HTML",
            disable_web_page_preview=True
        )

    elif action == 'del':
        if len(args) < 2:
            await _retry_send(update.message.reply_text,
                "❌ 请指定序号。\n用法：<code>/setgroup del 序号</code>",
                parse_mode="HTML"
            )
            return

        try:
            index = int(args[1]) - 1
        except ValueError:
            await _retry_send(update.message.reply_text, "❌ 序号必须是数字。")
            return

        groups_json = await get_platform_setting('chat_groups', '')
        groups = json.loads(groups_json) if groups_json else []

        if index < 0 or index >= len(groups):
            await _retry_send(update.message.reply_text, "❌ 序号超出范围。")
            return

        removed = groups.pop(index)
        await set_platform_setting('chat_groups', json.dumps(groups, ensure_ascii=False))

        await _retry_send(update.message.reply_text,
            f"✅ 已删除群组：{escape(removed.get('name', ''))}",
            parse_mode="HTML"
        )

    elif action == 'clear':
        await set_platform_setting('chat_groups', '[]')
        await _retry_send(update.message.reply_text, "✅ 已清空所有群组。")

    else:
        await _retry_send(update.message.reply_text,
            "❓ 未知操作。\n\n"
            "• <code>/setgroup add 群组名称 URL</code> — 添加\n"
            "• <code>/setgroup del 序号</code> — 删除\n"
            "• <code>/setgroup clear</code> — 清空",
            parse_mode="HTML"
        )
