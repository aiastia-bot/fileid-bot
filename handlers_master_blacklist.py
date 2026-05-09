"""黑名单管理命令 - /blacklist"""
import html
import logging

from telegram import Update
from telegram.ext import ContextTypes

from database import (
    get_user_bots_by_owner,
    update_user_bot_status,
    add_to_blacklist, remove_from_blacklist, is_user_blacklisted,
    get_blacklist,
    unban_user_bots,
)

logger = logging.getLogger(__name__)


def get_bot_manager():
    """获取全局 BotManager 实例"""
    import __main__
    return getattr(__main__, 'bot_manager', None)


def escape(text: str) -> str:
    """HTML 转义"""
    return html.escape(str(text), quote=False)


async def blacklist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/blacklist 管理黑名单"""
    from config import ADMIN_IDS
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ 此命令仅限管理员使用。")
        return

    if not context.args:
        # 显示黑名单列表
        bl = await get_blacklist()
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
        if await add_to_blacklist(target_id, reason):
            # 如果该用户有正在运行的 Bot，停止它们
            target_bots = await get_user_bots_by_owner(target_id)
            mgr = get_bot_manager()
            stopped = 0
            for bot in target_bots:
                if mgr and bot['id'] in mgr.get_all_apps():
                    await mgr.stop_bot(bot['id'])
                    stopped += 1
                await update_user_bot_status(bot['id'], 'banned')

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

        if await remove_from_blacklist(target_id):
            # 恢复该用户的 Bot
            await unban_user_bots(target_id)

            await update.message.reply_text(
                f"✅ 用户 <code>{target_id}</code> 已从黑名单移除。\n"
                f"💡 如需重新启动其 Bot，请使用 /platform bots 查看，或让用户使用 /botstatus。",
                parse_mode="HTML"
            )
            logger.info("管理员 %s 将用户 %s 从黑名单移除", user_id, target_id)
        else:
            await update.message.reply_text("⚠️ 该用户不在黑名单中。")

    elif action == 'list':
        bl = await get_blacklist()
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

        if await is_user_blacklisted(target_id):
            # 获取详细信息
            bl_list = await get_blacklist()
            entry = next((e for e in bl_list if e['user_id'] == target_id), None)
            text = f"🚫 用户 <code>{target_id}</code> <b>在黑名单中</b>。"
            if entry:
                text += f"\n📅 加入时间: {entry['created_at']}"
                if entry['reason']:
                    text += f"\n📝 原因: {escape(entry['reason'])}"
            # 显示该用户的 Bot
            target_bots = await get_user_bots_by_owner(target_id)
            if target_bots:
                text += f"\n\n🤖 该用户的 Bot ({len(target_bots)} 个):"
                for bot in target_bots:
                    text += f"\n  • @{escape(bot['bot_username'])} — {bot['status']}"
            await update.message.reply_text(text, parse_mode="HTML")
        else:
            target_bots = await get_user_bots_by_owner(target_id)
            text = f"✅ 用户 <code>{target_id}</code> 不在黑名单中。"
            if target_bots:
                text += f"\n🤖 该用户有 {len(target_bots)} 个 Bot。"
            await update.message.reply_text(text, parse_mode="HTML")

    else:
        await update.message.reply_text(
            "❓ 未知操作。可用操作: <code>add</code>, <code>del</code>, <code>list</code>, <code>check</code>",
            parse_mode="HTML"
        )