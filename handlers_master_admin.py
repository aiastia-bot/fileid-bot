"""管理员命令 - /platform, /export, /startbot, /broadcast"""
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

    # 检查是否有参数，如 /platform bots 显示详细 Bot 列表
    args = context.args or []
    show_bots = args and args[0] in ('bots', 'bot', 'detail', 'details')

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
        bot_details = await get_platform_bot_details()
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
            f"\n💡 使用 <code>/platform bots</code> 查看每个 Bot 的详细信息"
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

    # /export <bot_username|db_id> — 导出指定Bot的文件代码CSV（支持同名Bot）
    if args and args[0] not in ('json', 'csv', 'bots', 'code'):
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
            "• <code>/export @bot_username</code> — 指定Bot文件代码 CSV",
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


# ==================== 管理员启动Bot ====================

async def start_bot_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/startbot 管理员启动指定Bot（支持 revoked 状态的Bot重新尝试启动）"""
    from config import ADMIN_IDS
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await _retry_send(update.message.reply_text, "⛔ 此命令仅限管理员使用。")
        return

    if not context.args:
        await _retry_send(update.message.reply_text, 
            "🚀 <b>启动指定 Bot</b>\n\n"
            "用法：<code>/startbot @用户名</code> 或 <code>/startbot 数据库ID</code>\n\n"
            "可用于启动已停止或 revoked 状态的 Bot。\n"
            "使用 /platform bots 查看所有 Bot。",
            parse_mode="HTML"
        )
        return

    arg = context.args[0].strip()
    bot_record = None

    # 按数据库ID查找
    try:
        bot_db_id = int(arg)
        bot_record = await get_user_bot_by_id(bot_db_id)
    except ValueError:
        pass

    # 按用户名查找
    if not bot_record:
        username = arg.lstrip('@')
        bot_record = await get_user_bot_by_username(username)

    if not bot_record:
        await _retry_send(update.message.reply_text, f"❌ 未找到 Bot：{escape(arg)}")
        return

    # 检查是否已在运行
    mgr = get_bot_manager()
    if mgr and bot_record['id'] in mgr.get_all_apps():
        await _retry_send(update.message.reply_text, 
            f"ℹ️ Bot @{escape(bot_record['bot_username'])} 已在运行中，无需启动。"
        )
        return

    # 检查是否被封禁
    if bot_record['status'] == 'banned':
        await _retry_send(update.message.reply_text, 
            f"🚫 Bot @{escape(bot_record['bot_username'])} 已被封禁，无法启动。\n"
            f"使用 /blacklist del {bot_record['owner_id']} 解除封禁。"
        )
        return

    status_msg = await _retry_send(update.message.reply_text, 
        f"⏳ 正在启动 @{escape(bot_record['bot_username'])}..."
    )

    # 更新数据库状态为 active
    await update_user_bot_status(bot_record['id'], 'active')

    # 先停止旧实例（如果存在）
    if mgr:
        await mgr.stop_bot(bot_record['id'])

    # 重新获取记录并启动
    bot_record = await get_user_bot_by_id(bot_record['id'])
    success = await mgr.start_bot(bot_record) if mgr else False

    if success:
        await status_msg.edit_text(
            f"✅ <b>Bot 启动成功！</b>\n\n"
            f"🤖 @{escape(bot_record['bot_username'])}\n"
            f"🆔 Bot ID：<code>{bot_record['bot_id']}</code>\n"
            f"👤 所有者：<code>{bot_record['owner_id']}</code>",
            parse_mode="HTML"
        )
        logger.info("管理员 %s 启动了 Bot @%s", user_id, bot_record['bot_username'])
    else:
        await status_msg.edit_text(
            f"❌ <b>启动失败</b>\n\n"
            f"Bot @{escape(bot_record['bot_username'])} 启动失败。\n"
            f"可能原因：Token 已失效。\n\n"
            f"💡 让用户使用 /updatetoken 更新 Token，\n"
            f"或使用 /delbot 删除后重建。",
            parse_mode="HTML"
        )


# ==================== 广播消息 ====================

async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/broadcast 管理员广播消息给所有Bot所有者"""
    from config import ADMIN_IDS
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await _retry_send(update.message.reply_text, "⛔ 此命令仅限管理员使用。")
        return

    if not context.args:
        await _retry_send(update.message.reply_text, 
            "📢 <b>广播命令</b>\n\n"
            "用法：<code>/broadcast 消息内容</code>\n\n"
            "消息将发送给所有 Bot 所有者。",
            parse_mode="HTML"
        )
        return

    text = " ".join(context.args)
    owner_ids = await get_all_owner_ids()
    status_msg = await _retry_send(update.message.reply_text, f"⏳ 正在广播给 {len(owner_ids)} 位用户...")

    success = 0
    fail = 0
    for oid in owner_ids:
        try:
            await _retry_send(context.bot.send_message, chat_id=oid, text=text, parse_mode="HTML")
            success += 1
        except Exception:
            fail += 1

    await status_msg.edit_text(f"✅ 广播完成：成功 {success}/{len(owner_ids)}，失败 {fail}")