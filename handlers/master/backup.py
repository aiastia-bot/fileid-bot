"""/backup 级联备份命令（主 Bot，面向所有 Bot 主人）

用法：
  /backup                     查看备份树与覆盖情况
  /backup on @主bot @备份1 [@备份2 ...]   配置备份组并立即建树
  /backup add @bot            追加备份 Bot（自动补位重排）
  /backup remove @bot         移除某个成员
  /backup off                 关闭备份
  /backup sync                把主 Bot 已有而下游缺的文件批量回填
"""
import logging

from telegram import Update
from telegram.ext import ContextTypes

from config import ADMIN_IDS, BACKUP_ENABLED, BACKUP_FANOUT
from db import (
    get_user_bot_by_username, get_backup_config, get_owner_edges,
    coverage_stats, set_backup_members, add_backup_member,
    remove_backup_member, disable_backup, reconcile_owner,
)
from db.vip import user_can_use_backup
from services.backup import request_backup_sync

logger = logging.getLogger(__name__)

USAGE = (
    "📦 级联备份用法：\n"
    "/backup — 查看备份树与覆盖\n"
    "/backup on @主bot @备份1 [@备份2 ...] — 配置备份组\n"
    "/backup add @bot — 追加备份 Bot\n"
    "/backup remove @bot — 移除成员\n"
    "/backup off — 关闭备份\n"
    "/backup sync — 回填存量文件"
)

TUTORIAL = (
    "📦 <b>级联备份教程</b>\n\n"
    "把几个 Bot 组成互备树：主 Bot 少跑腿，"
    "文件在多个 Bot 上都有副本，某个 Bot 挂了随时补位。\n\n"
    "0️⃣ <b>前置</b>：每个参与 Bot 都要在 @BotFather 打开 "
    "Bot-to-Bot Communication Mode"
    "（Bot Settings → Bot-to-Bot Communication），缺一个都不通。\n\n"
    "1️⃣ <b>配置</b>：<code>/backup on @主bot @备份1 @备份2</code>\n"
    "   第一个是主 Bot（对外营业那个），它只推 1 份给备份；\n"
    "   备份 Bot 再分发给后面的成员，主 Bot 负载不随备份数量增长。\n\n"
    "2️⃣ <b>自动运行</b>：配置后，发给任何成员的新文件都会自动级联备份，"
    "接收方静默保存自己的副本（无任何回复消息）。\n\n"
    "3️⃣ <b>自动补位</b>：某个 Bot 失效（被封/删除/暂停）后 1 分钟内"
    "自动重建链路，剩余 Bot 顶上，并私信通知你。\n\n"
    "4️⃣ <b>取回</b>：把主 Bot 给你的代码发给任何备份 Bot，"
    "它会用<b>自己的</b>文件身份把文件发给你——代码不用换。\n\n"
    "5️⃣ <b>旧文件</b>：<code>/backup sync</code> 把主 Bot 已有的老文件"
    "批量补推给下游（新文件无需此步）。\n\n"
    "📊 <code>/backup</code> 随时查看链路和每个下游缺多少文件。\n\n"
    "💡 <b>建议</b>：主 Bot 选你用得最多的；备份 Bot 平时不用管它。"
)


async def _lookup_owned_bot(owner_id: int, username: str):
    name = username.strip().lstrip('@').strip()
    bot = await get_user_bot_by_username(name)
    if not bot:
        return None, f"❌ 找不到 Bot @{name}（先在主 Bot 用 /addbot 添加）"
    if bot.get('owner_id') != owner_id:
        return None, f"❌ @{name} 不是你的 Bot"
    if bot.get('status') != 'active':
        return None, f"❌ @{name} 当前状态为 {bot.get('status')}，不能参与备份"
    return bot, None


def _render_tree(members: list, edges: list) -> str:
    if not members:
        return "（未配置）"
    lines = []
    for m in members:
        role = "主Bot" if m['position'] == 0 else "备份"
        mark = "" if m['status'] == 'active' else f" ⛔{m['status']}"
        lines.append(f"  {role}: @{m['username']}{mark}")
    if edges:
        names = {m['bot_db_id']: f"@{m['username']}" for m in members}
        lines.append("链路: " + " ".join(f"{names.get(p, p)}→{names.get(c, c)}"
                                       for p, c in edges))
    return "\n".join(lines)


async def _show_status(update: Update, context, owner_id: int) -> None:
    if not BACKUP_ENABLED:
        await update.message.reply_text("备份功能当前已在服务端关闭（BACKUP_ENABLED=0）。")
        return
    config = await get_backup_config(owner_id)
    members = config['members']
    if len(members) < 2 or not config['enabled']:
        await update.message.reply_text(
            "📦 尚未配置级联备份。\n\n" + TUTORIAL, parse_mode='HTML')
        return
    edges = await get_owner_edges(owner_id)
    text = f"📦 备份组（分发宽度 {BACKUP_FANOUT}）\n" + _render_tree(members, edges)
    stats = await coverage_stats(owner_id)
    if stats:
        text += "\n\n📊 覆盖情况："
        for s in stats:
            state = "✅ 已同步" if s['child_missing'] == 0 else f"⚠️ 缺 {s['child_missing']} 个"
            text += f"\n  {s['from']} → {s['to']}：父 {s['parent_total']} 个，{state}"
        missing_any = any(s['child_missing'] > 0 for s in stats)
        if missing_any:
            text += "\n\n💡 用 /backup sync 回填缺失文件"
    await update.message.reply_text(text)


async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    owner_id = update.effective_user.id
    args = (context.args or [])
    sub = args[0].lower() if args else ''

    if not BACKUP_ENABLED:
        await update.message.reply_text("备份功能当前已在服务端关闭（BACKUP_ENABLED=0）。")
        return

    # VIP 2/3 专属（管理员放行）
    if owner_id not in ADMIN_IDS and not await user_can_use_backup(owner_id):
        await update.message.reply_text(
            "🔒 级联备份是 VIP 2 / VIP 3 专属功能。\n\n"
            "使用 /vip 了解会员权益与升级方式。")
        return

    try:
        if sub in ('help', '教程'):
            await update.message.reply_text(TUTORIAL, parse_mode='HTML')
            return

        if sub in ('', 'status', 'list'):
            await _show_status(update, context, owner_id)
            return

        if sub == 'on':
            usernames = args[1:]
            if len(usernames) < 2:
                await update.message.reply_text(
                    "❌ 至少需要 2 个 Bot：第一个是主 Bot，其余是备份。\n\n" + USAGE)
                return
            seen_ids, names = [], []
            for username in usernames:
                bot, error = await _lookup_owned_bot(owner_id, username)
                if error:
                    await update.message.reply_text(error)
                    return
                if bot['id'] in seen_ids:
                    await update.message.reply_text(f"❌ @{bot['bot_username']} 重复出现")
                    return
                seen_ids.append(bot['id'])
                names.append(f"@{bot['bot_username']}")
            await set_backup_members(owner_id, seen_ids)
            result = await reconcile_owner(owner_id, BACKUP_FANOUT)
            config = await get_backup_config(owner_id)
            edges = await get_owner_edges(owner_id)
            text = "✅ 备份组已配置\n" + _render_tree(config['members'], edges)
            if result.get('dropped'):
                text += "\n\n⛔ 以下 Bot 已失效未参与: " + " ".join(f"@{u}" for u in result['dropped'])
            await update.message.reply_text(text)
            return

        if sub == 'add':
            if len(args) < 2:
                await update.message.reply_text("用法：/backup add @bot")
                return
            bot, error = await _lookup_owned_bot(owner_id, args[1])
            if error:
                await update.message.reply_text(error)
                return
            config = await get_backup_config(owner_id)
            ids = [m['bot_db_id'] for m in config['members']]
            if bot['id'] in ids:
                await update.message.reply_text(f"⚠️ @{bot['bot_username']} 已在备份组中")
                return
            await add_backup_member(owner_id, bot['id'])
            await reconcile_owner(owner_id, BACKUP_FANOUT)
            config = await get_backup_config(owner_id)
            edges = await get_owner_edges(owner_id)
            await update.message.reply_text(
                "✅ 已加入备份组\n" + _render_tree(config['members'], edges))
            return

        if sub in ('remove', 'rm'):
            if len(args) < 2:
                await update.message.reply_text("用法：/backup remove @bot")
                return
            bot, error = await _lookup_owned_bot(owner_id, args[1])
            if error:
                await update.message.reply_text(error)
                return
            await remove_backup_member(owner_id, bot['id'])
            await reconcile_owner(owner_id, BACKUP_FANOUT)
            config = await get_backup_config(owner_id)
            if not config['enabled'] or len(config['members']) < 2:
                await update.message.reply_text("✅ 已移除；成员不足 2 个，备份已自动关闭")
                return
            edges = await get_owner_edges(owner_id)
            await update.message.reply_text(
                "✅ 已移除并重建链路\n" + _render_tree(config['members'], edges))
            return

        if sub == 'off':
            await disable_backup(owner_id)
            await update.message.reply_text("✅ 备份已关闭，链路已清除")
            return

        if sub == 'sync':
            await update.message.reply_text("📦 正在发起存量回填…")
            result = await request_backup_sync(owner_id)
            await update.message.reply_text(result)
            return

        await update.message.reply_text(USAGE)
    except Exception as exc:
        logger.exception("/backup 执行失败 owner=%s", owner_id)
        await update.message.reply_text(f"❌ 执行失败: {exc}")
