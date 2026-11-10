"""管理员命令 - /platform, /export, /startbot, /stopbot, /broadcast"""
import io
import json
import logging
import os
import tempfile
from datetime import datetime
from senders import _retry_send

from telegram import Update
from telegram.ext import ContextTypes

from db import (
    get_user_bot_by_id,
    get_user_bot_by_username,
    update_user_bot_status,
    get_all_owner_ids,
    get_platform_stats,
    get_platform_bot_details,
    stream_files_by_bot_db_id,
    stream_active_bot_files,
    stream_export_bots, stream_export_files, stream_export_collections,
    stream_export_blacklist,
    get_blacklist_count,
    get_platform_setting, set_platform_setting,
    update_user_vip, get_user_vip_info,
)
from handlers.master._utils import get_bot_manager, escape

logger = logging.getLogger(__name__)

# VIP 用户 Telegram 显示名缓存（/setvip list 用，避免每次命令逐个 get_chat）
_vip_name_cache: dict = {}  # user_id -> (name, ts)
_VIP_NAME_TTL = 3600


async def _get_user_display_name(bot, user_id: int) -> str:
    """尽力获取用户 Telegram 显示名（1 小时缓存），失败回退 用户{ID}"""
    import time as _time
    now = _time.time()
    cached = _vip_name_cache.get(user_id)
    if cached and now - cached[1] < _VIP_NAME_TTL:
        return cached[0]
    name = None
    try:
        chat = await bot.get_chat(user_id)
        if chat:
            name = chat.first_name or chat.username or None
    except Exception:
        name = None
    display = name if name else f"用户{user_id}"
    _vip_name_cache[user_id] = (display, now)
    if len(_vip_name_cache) > 5000:
        for k in sorted(_vip_name_cache, key=lambda k: _vip_name_cache[k][1])[:2500]:
            del _vip_name_cache[k]
    return display


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
                    f"   👤 所有者: <a href=\"tg://user?id={bot['owner_id']}\">{bot['owner_id']}</a>\n"
                    f"   📁 文件: {bot['file_count']} | 🆕 今日新文件: {bot.get('today_file_count', 0)} | 📦 集合: {bot['col_count']} | 👥 用户: {bot['user_count']}\n"
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

def _new_export_file(suffix: str) -> str:
    """创建导出用临时文件路径"""
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="export_")
    os.close(fd)
    return path


async def _send_export_file(context, update, status_msg, tmp_path: str,
                            filename: str, caption: str):
    """发送导出的临时文件并清理"""
    try:
        with open(tmp_path, 'rb') as f:
            await _retry_send(context.bot.send_document,
                chat_id=update.message.chat_id,
                document=f,
                filename=filename,
                caption=caption,
            )
        await status_msg.delete()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def _write_json_array(f, agen) -> int:
    """将异步生成器的条目增量写入 JSON 数组（避免全量构造大 list），返回条目数"""
    count = 0
    f.write('[')
    async for item in agen:
        if count:
            f.write(', ')
        f.write(json.dumps(item, ensure_ascii=False, default=str))
        count += 1
    f.write(']')
    return count


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

        tmp_path = None
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

            # 流式导出：边查边写临时文件，避免全量载入内存
            tmp_path = _new_export_file(".txt")
            count = 0
            with open(tmp_path, 'w', encoding='utf-8') as f:
                async for row in stream_files_by_bot_db_id(bot_db_id_export):
                    f.write(f"{row['code']}\n")
                    count += 1
            if count == 0:
                await status_msg.edit_text(f"📭 Bot @{escape(bot_username)} (ID:{bot_db_id_export}) 没有文件记录。")
                return

            filename = f"{bot_username}_{bot_db_id_export}_codes_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
            await _send_export_file(context, update, status_msg, tmp_path, filename,
                f"📄 @{escape(bot_username)} (ID:{bot_db_id_export}) 纯文本 code 导出，共 {count} 条。")
            tmp_path = None  # 已发送并清理
            logger.info("管理员 %s 导出了 Bot @%s (ID:%d) 的 TXT code (%d 条)", user_id, bot_username, bot_db_id_export, count)
        except Exception as e:
            await status_msg.edit_text(f"❌ 导出失败: {escape(str(e))}")
            logger.error("导出Bot TXT数据失败: %s", e, exc_info=True)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        return

    # /export <bot_username|db_id> — 导出指定Bot的文件代码CSV（支持同名Bot）
    if args and args[0] not in ('json', 'csv', 'bots', 'code', 'txt'):
        export_arg = args[0].strip().lstrip('@')
        status_msg = await _retry_send(update.message.reply_text, "⏳ 正在导出...")

        tmp_path = None
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

            # 流式导出：边查边写临时文件，避免全量载入内存
            tmp_path = _new_export_file(".csv")
            count = 0
            with open(tmp_path, 'w', encoding='utf-8') as f:
                f.write("code,file_type,file_size,user_id,created_at\n")
                async for row in stream_files_by_bot_db_id(bot_db_id_export):
                    f.write(f"{row['code']},{row['file_type']},{row['file_size']},{row['user_id']},{row['created_at']}\n")
                    count += 1
            if count == 0:
                await status_msg.edit_text(f"📭 Bot @{escape(bot_username)} (ID:{bot_db_id_export}) 没有文件记录。")
                return

            filename = f"{bot_username}_{bot_db_id_export}_files_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            await _send_export_file(context, update, status_msg, tmp_path, filename,
                f"📁 @{escape(bot_username)} (ID:{bot_db_id_export}) 文件代码导出，共 {count} 条记录。")
            tmp_path = None  # 已发送并清理
            logger.info("管理员 %s 导出了 Bot @%s (ID:%d) 的文件代码 (%d 条)", user_id, bot_username, bot_db_id_export, count)
        except Exception as e:
            await status_msg.edit_text(f"❌ 导出失败: {escape(str(e))}")
            logger.error("导出Bot数据失败: %s", e, exc_info=True)
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
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

    tmp_path = None
    try:
        # 全部改为流式导出：边查边写临时文件，避免全表 fetchall 造成内存尖峰
        if export_format in ('json', 'code'):
            tmp_path = _new_export_file(".json")
            counts = {}
            with open(tmp_path, 'w', encoding='utf-8') as f:
                f.write('{')
                f.write('"bots": ')
                counts['bots'] = await _write_json_array(f, stream_export_bots())
                f.write(', "files": ')
                counts['files'] = await _write_json_array(f, stream_export_files())
                f.write(', "collections": ')
                counts['collections'] = await _write_json_array(f, stream_export_collections())
                f.write(', "blacklist": ')
                counts['blacklist'] = await _write_json_array(f, stream_export_blacklist())
                f.write('}')
            filename = f"platform_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            caption = (
                f"📊 平台数据导出\n\n"
                f"🤖 Bot: {counts['bots']} 个\n"
                f"📁 文件: {counts['files']} 条\n"
                f"📦 集合: {counts['collections']} 个\n"
                f"🚫 黑名单: {counts['blacklist']} 人"
            )
        elif export_format == 'csv':
            # 支持日期参数: /export csv 2026-05-05
            since_date = args[1] if len(args) > 1 else None
            tmp_path = _new_export_file(".csv")
            count = 0
            with open(tmp_path, 'w', encoding='utf-8') as f:
                f.write("code,bot_username,file_type,file_size,user_id,created_at\n")
                async for row in stream_active_bot_files(since_date):
                    f.write(f"{row['code']},{row['bot_username']},{row['file_type']},{row['file_size']},{row['user_id']},{row['created_at']}\n")
                    count += 1
            date_info = f"（{since_date} 起）" if since_date else ""
            filename = f"active_files_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            caption = f"📁 活跃Bot文件导出{date_info}，共 {count} 条记录。"
        elif export_format == 'bots':
            tmp_path = _new_export_file(".csv")
            count = 0
            with open(tmp_path, 'w', encoding='utf-8') as f:
                f.write("id,owner_id,bot_id,bot_username,bot_firstname,status,created_at\n")
                async for b in stream_export_bots():
                    f.write(f"{b['id']},{b['owner_id']},{b['bot_id']},{b['bot_username']},{b['bot_firstname']},{b['status']},{b['created_at']}\n")
                    count += 1
            filename = f"bots_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            caption = f"🤖 Bot 列表导出，共 {count} 条记录。"
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

        await _send_export_file(context, update, status_msg, tmp_path, filename, caption)
        tmp_path = None  # 已发送并清理
        logger.info("管理员 %s 导出了平台数据 (格式: %s)", user_id, export_format)
    except Exception as e:
        await status_msg.edit_text(f"❌ 导出失败: {escape(str(e))}")
        logger.error("导出数据失败: %s", e, exc_info=True)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


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
                f"👤 所有者：<a href=\"tg://user?id={bot_record['owner_id']}\">{bot_record['owner_id']}</a>",
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
        # 即使没在运行，也更新数据库状态为 admin_stopped
        if bot_record['status'] == 'active':
            await update_user_bot_status(bot_record['id'], 'admin_stopped')

        # 主动删除 webhook（防止残留 webhook 导致 bot 仍能接收消息）
        try:
            from telegram import Bot
            temp_bot = Bot(token=bot_record['bot_token'])
            await _retry_send(temp_bot.delete_webhook)
            await temp_bot.shutdown()
            logger.info("已删除 Bot @%s 的 webhook", bot_record['bot_username'])
        except Exception as e:
            logger.warning("删除 Bot @%s webhook 失败: %s", bot_record['bot_username'], e)

        await _retry_send(update.message.reply_text, 
            f"ℹ️ Bot @{escape(bot_record['bot_username'])} 当前未在运行。数据库状态已更新，webhook 已删除。"
        )
        return

    # 停止运行中的实例
    success = False
    if mgr:
        success = await mgr.stop_bot(bot_record['id'])

    # 更新数据库状态为 admin_stopped（区别于 VIP 暂停的 paused）
    await update_user_bot_status(bot_record['id'], 'admin_stopped')

    if success:
        await _retry_send(update.message.reply_text, 
            f"✅ <b>Bot 已停止</b>\n\n"
            f"🤖 @{escape(bot_record['bot_username'])}\n"
            f"🆔 Bot ID：<code>{bot_record['bot_id']}</code>\n"
            f"👤 所有者：<a href=\"tg://user?id={bot_record['owner_id']}\">{bot_record['owner_id']}</a>\n\n"
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
    """/broadcast user 子命令 — 通过每个运行中的用户Bot给该Bot的主人发消息
    
    使用 asyncio.create_task 异步执行，避免 webhook 超时导致 Telegram 重复投递。
    """
    import asyncio

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
    admin_chat_id = update.message.chat_id
    bot = context.bot  # 捕获 bot 引用

    # 立即回复，避免 webhook 超时
    await _retry_send(update.message.reply_text,
        f"⏳ 正在通过 {len(apps)} 个用户Bot给各自的主人发送消息...\n完成后会通知你。")

    async def _do_broadcast():
        total_success = 0
        total_fail = 0
        bot_results = []
        sent_owners = set()  # 去重：每个主人只发一次

        for bot_db_id, app in apps.items():
            bot_username = app.bot.username if app.bot else f"Bot#{bot_db_id}"

            # 优先用内存中的 bot_record（启动时已加载），避免每个 Bot 查一次 DB
            bot_record = app.bot_data.get('bot_record') or {}
            if not bot_record.get('owner_id'):
                bot_record = await get_user_bot_by_id(bot_db_id)
                if not bot_record:
                    bot_results.append(f"❓ @{escape(bot_username)}: 未找到记录")
                    continue

            owner_id = bot_record['owner_id']

            # 去重：同一个主人只尝试一次（无论成功失败）
            if owner_id in sent_owners:
                continue

            sent_owners.add(owner_id)
            try:
                await _retry_send(
                    app.bot.send_message,
                    chat_id=owner_id,
                    text=message_text,
                    parse_mode="HTML"
                )
                total_success += 1
                bot_results.append(f"✅ @{escape(bot_username)} → 主人 <a href=\"tg://user?id={owner_id}\">{owner_id}</a>")
            except Exception as e:
                total_fail += 1
                bot_results.append(f"❌ @{escape(bot_username)} → 主人 <a href=\"tg://user?id={owner_id}\">{owner_id}</a>: {escape(str(e)[:60])}")

        # 汇总结果发送给管理员
        result_lines = "\n".join(bot_results)
        summary = f"✅ <b>用户Bot主人通知完成</b>\n\n📊 总计：成功 {total_success}，失败 {total_fail}\n\n"
        full_text = summary + result_lines

        try:
            if len(full_text) > 4000:
                await _retry_send(bot.send_message,
                    chat_id=admin_chat_id, text=summary, parse_mode="HTML")
                parts = []
                current = ""
                for line in result_lines.split('\n'):
                    if len(current) + len(line) + 1 > 3800:
                        parts.append(current)
                        current = line + '\n'
                    else:
                        current += line + '\n'
                if current:
                    parts.append(current)
                for part in parts:
                    await _retry_send(bot.send_message,
                        chat_id=admin_chat_id, text=part, parse_mode="HTML")
            else:
                await _retry_send(bot.send_message,
                    chat_id=admin_chat_id, text=full_text, parse_mode="HTML")
        except Exception as e:
            logger.error("发送广播结果给管理员失败: %s", e)

    # 异步执行，不阻塞 webhook 响应
    asyncio.create_task(_do_broadcast())


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
                text += f"  {i}. <a href=\"{escape(g.get('url', ''))}\">{escape(g.get('name', ''))}</a>\n"
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


# ==================== 管理员设置VIP ====================

async def set_vip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from handlers.master.membership_admin import setvip_command
    await setvip_command(update, context)


# ==================== 管理员帮助 ====================

async def help_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/helpadmin 管理员命令帮助（仅管理员可见）"""
    from config import ADMIN_IDS, VIP_PLANS, BASIC_LEVEL
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await _retry_send(update.message.reply_text, "⛔ 此命令仅限管理员使用。")
        return

    from services.membership import get_plan
    basic = await get_plan(BASIC_LEVEL)
    text = (
        "🛠 <b>管理员命令帮助</b>\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "👤 <b>用户命令（所有用户可用）</b>\n"
        "• /start /help — 开始使用 / 帮助\n"
        "• /vip — 会员中心（购买/续费/迁移会员/支付记录）\n"
        "• /premium — 购买 Telegram Premium\n"
        "• /newbot — 一键创建 Bot\n"
        "• /addbot — 添加已有 Bot（Token）\n"
        "• /mybots — 我的 Bot 列表（含转发保护设置）\n"
        "• /delbot — 删除 Bot\n"
        "• /botstatus — Bot 运行状态\n"
        "• /updatetoken — 更新失效 Token\n"
        "用户 Bot 内：/create /pack /done /cancel /getid /mycol /stop /delcol /settings\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "👑 <b>会员管理</b>\n"
        "• <code>/setvip USER_ID LEVEL [MONTHS]</code> — 设置会员\n"
        "  等级 0-4（4=基础版），默认 12 个月；<code>0</code> 取消会员\n"
        "• <code>/setvip info USER_ID</code> — 查询用户会员\n"
        "• <code>/setvip list</code> — 付费会员列表（名字/ID/到期/Bot）\n"
        "• <code>/setfree</code> — 免费注册管理面板\n"
        "  <code>close</code> 关闭免费注册（新用户需购基础版 "
        f"{basic['monthly_price']}⭐/月，存量免费用户保留）\n"
        "  <code>open</code> 恢复免费注册\n"
        "  <code>limit N</code> 免费用户名额上限（0=不限）\n"
        "  <code>files N</code> 免费用户 Bot 文件上限（默认 20,000）\n"
        "  <code>basicfiles N</code> 基础版 Bot 文件上限（默认 50,000）\n"
        "  💡 VIP 不限文件数；超限文件仍入库但不返回代码\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🤖 <b>Bot / 平台管理</b>\n"
        "• <code>/platform [bots|bots all]</code> — 平台统计 / Bot 详情\n"
        "• <code>/startbot @用户名|数据库ID</code> — 启动/重启 Bot\n"
        "• <code>/stopbot @用户名|数据库ID</code> — 停止 Bot\n"
        "• <code>/blacklist</code> — 黑名单管理\n"
        "• <code>/broadcast user 消息内容</code> — 广播消息\n"
        "• <code>/export</code> — 导出数据\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "💎 <b>星星资产 / 其他</b>\n"
        "• <code>/mystars</code> — 星星余额 / 发礼物 / 赠 Premium / 领取码\n"
        "• <code>/setgroup add|del|clear</code> — 用户 Bot 沟通群组\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔄 <b>会员迁移（用户自助，无需管理员操作）</b>\n"
        "用户在 /vip 点「迁移会员」生成迁移码，新账号发 "
        "<code>/start mig_码</code> 领取。\n"
        "剩余时长原样搬运、有效期=会员剩余期。\n"
        "通过迁移收到的会员不能再迁出；重新购买或管理员重新授予后重置，可再迁移一次。"
    )
    await _retry_send(update.message.reply_text, text, parse_mode="HTML")


# ==================== 管理员管理免费注册/名额 ====================

async def set_free_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/setfree 管理员管理免费注册开关和免费用户名额

    用法：
        /setfree               — 查看当前状态
        /setfree close         — 关闭免费注册（新用户需购买基础版 10⭐/月，存量免费用户保留）
        /setfree open          — 恢复免费注册
        /setfree limit N       — 免费用户数量上限（0=不限制；未设置时用环境变量 MAX_VIP0_USERS）
    """
    from config import ADMIN_IDS, VIP_PLANS, BASIC_LEVEL
    from db.vip import (
        is_free_registration_closed, get_max_vip0_users_limit,
        invalidate_free_settings_cache, get_vip0_user_count,
    )

    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await _retry_send(update.message.reply_text, "⛔ 此命令仅限管理员使用。")
        return

    args = context.args or []
    from services.membership import get_plan
    basic = await get_plan(BASIC_LEVEL)

    if not args:
        closed = await is_free_registration_closed()
        limit = await get_max_vip0_users_limit()
        count = await get_vip0_user_count()
        limit_text = "不限制" if limit <= 0 else str(limit)

        from db.vip import get_free_bot_files_limit, get_basic_bot_files_limit
        free_files = await get_free_bot_files_limit()
        basic_files = await get_basic_bot_files_limit()
        free_files_text = "不限" if free_files <= 0 else f"{free_files:,}"
        basic_files_text = "不限" if basic_files <= 0 else f"{basic_files:,}"

        text = (
            f"🆓 <b>免费注册管理</b>\n\n"
            f"📌 免费注册：{'🚫 已关闭（新用户需购买基础版）' if closed else '✅ 开放中'}\n"
            f"💎 基础版价格：月付 {basic['monthly_price']}⭐ / 年付 {basic['yearly_price']}⭐（{basic['max_bots']} 个 Bot）\n"
            f"📊 免费用户上限：{limit_text}\n"
            f"👥 当前免费占用名额：{count}（按用户去重，仅统计有运行中 Bot 的免费用户）\n"
            f"📁 Bot 文件上限（按 Bot 计）：免费 {free_files_text} / 基础版 {basic_files_text} / VIP 不限\n\n"
            f"<b>用法：</b>\n"
            f"• <code>/setfree close</code> — 关闭免费注册\n"
            f"• <code>/setfree open</code> — 恢复免费注册\n"
            f"• <code>/setfree limit N</code> — 设置免费用户上限（0=不限制）\n"
            f"• <code>/setfree files N</code> — 免费用户 Bot 文件上限（0=不限）\n"
            f"• <code>/setfree basicfiles N</code> — 基础版 Bot 文件上限（0=不限）\n\n"
            f"💡 关闭免费注册后，存量免费用户不受影响；免费开放期间不出售基础版。"
        )
        await _retry_send(update.message.reply_text, text, parse_mode="HTML")
        return

    action = args[0].lower()

    if action == 'close':
        await set_platform_setting('free_registration', 'closed')
        await invalidate_free_settings_cache()
        await _retry_send(update.message.reply_text,
            "✅ 已关闭免费注册。\n\n"
            "• 新用户需购买基础版或 VIP 才能创建 Bot\n"
            "• 存量免费用户（已有 Bot）不受影响\n"
            "• /vip 页面将展示基础版购买入口"
        )
        logger.info("管理员 %s 关闭了免费注册", user_id)

    elif action == 'open':
        await set_platform_setting('free_registration', 'open')
        await invalidate_free_settings_cache()
        await _retry_send(update.message.reply_text,
            "✅ 已恢复免费注册。\n\n"
            "• 基础版停止出售（已购买的基础版用户不受影响，到期后 Bot 暂停）"
        )
        logger.info("管理员 %s 恢复了免费注册", user_id)

    elif action == 'limit':
        if len(args) < 2:
            await _retry_send(update.message.reply_text,
                "❌ 请指定数量。\n用法：<code>/setfree limit N</code>（0=不限制）",
                parse_mode="HTML"
            )
            return
        try:
            n = int(args[1])
        except ValueError:
            await _retry_send(update.message.reply_text, "❌ 数量必须是数字。")
            return
        if n < 0:
            await _retry_send(update.message.reply_text, "❌ 数量不能为负数。")
            return

        await set_platform_setting('max_vip0_users', str(n))
        await invalidate_free_settings_cache()
        limit_text = "不限制" if n == 0 else f"{n} 个"
        count = await get_vip0_user_count()
        await _retry_send(update.message.reply_text,
            f"✅ 免费用户上限已设置为 {limit_text}。\n"
            f"当前免费占用名额：{count}"
        )
        logger.info("管理员 %s 设置免费用户上限为 %d", user_id, n)

    elif action in ('files', 'basicfiles'):
        if len(args) < 2:
            await _retry_send(update.message.reply_text,
                "❌ 请指定数量。\n用法：<code>/setfree files N</code>（0=不限）",
                parse_mode="HTML"
            )
            return
        try:
            n = int(args[1])
        except ValueError:
            await _retry_send(update.message.reply_text, "❌ 数量必须是数字。")
            return
        if n < 0:
            await _retry_send(update.message.reply_text, "❌ 数量不能为负数。")
            return

        setting_key = 'free_bot_files' if action == 'files' else 'basic_bot_files'
        tier_name = "免费用户" if action == 'files' else "基础版"
        await set_platform_setting(setting_key, str(n))
        await invalidate_free_settings_cache()
        limit_text = "不限制" if n == 0 else f"{n:,} 个"
        await _retry_send(update.message.reply_text,
            f"✅ {tier_name} Bot 文件上限已设置为 {limit_text}（按 Bot 计）。"
        )
        logger.info("管理员 %s 设置 %s Bot 文件上限为 %d", user_id, tier_name, n)

    else:
        await _retry_send(update.message.reply_text,
            "❓ 未知操作。\n\n"
            "• <code>/setfree close</code> — 关闭免费注册\n"
            "• <code>/setfree open</code> — 恢复免费注册\n"
            "• <code>/setfree limit N</code> — 设置免费用户上限\n"
            "• <code>/setfree files N</code> — 免费用户 Bot 文件上限\n"
            "• <code>/setfree basicfiles N</code> — 基础版 Bot 文件上限",
            parse_mode="HTML"
        )
