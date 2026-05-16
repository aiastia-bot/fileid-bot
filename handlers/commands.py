"""命令处理器模块"""
import asyncio
import io
import logging
from datetime import datetime

from telegram import Update
from telegram.ext import ContextTypes

from config import MAX_COLLECTION_FILES, FILE_TYPE_MAP
from db import (
    save_file, get_file, get_collection, create_collection,
    add_file_to_collection, complete_collection, delete_collection,
    get_user_collections, get_stats, get_all_files_for_export,
    get_user_forward_protect, set_user_forward_protect,
)
from utils import get_code_prefix, escape_markdown, generate_raw_code, admin_only
from senders import _retry_send

logger = logging.getLogger(__name__)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/start 和 /help 命令"""
    bot_username = context.bot.username

    # 获取主Bot用户名
    master_info = ""
    try:
        import __main__
        mgr = getattr(__main__, 'bot_manager', None)
        if mgr and mgr.master_bot_username:
            master_info = f"\n\n🏗 创建自 @{escape_markdown(mgr.master_bot_username)}"
    except Exception:
        pass

    # 获取沟通群组链接
    group_links = ""
    try:
        from db import get_platform_setting
        import json
        groups_json = await get_platform_setting('chat_groups', '')
        if groups_json:
            groups = json.loads(groups_json)
            if groups:
                group_lines = []
                for g in groups:
                    name = escape_markdown(g.get('name', ''))
                    url = g.get('url', '')
                    if name and url:
                        group_lines.append(f"[{name}]({url})")
                if group_lines:
                    group_links = "\n\n" + " ".join(group_lines)
    except Exception:
        pass

    help_text = f"""🤖 *FileID Bot* — 文件ID互转工具

📌 *核心功能：*
• 发送图片/视频/音频/文档 → 获取唯一代码
• 发送代码 → 获取对应文件
• 支持 `send_media_group` 组发送

📦 *集合功能：*
• `/create 名称` — 创建集合（连续发文件）
• `/done` — 完成集合
• `/cancel` — 取消当前操作
• `/mycol` — 查看我的集合
• `/delcol 代码` — 删除集合

🔧 *其他命令：*
• `/stop` — 停止所有发送任务
• 回复消息 + `/getid` — 获取文件ID
• `/stats` — 管理员统计

📝 *代码格式：*
• `{bot_username}_p:xxx` — 图片
• `{bot_username}_v:xxx` — 视频
• `{bot_username}_d:xxx` — 文档/音频
• `{bot_username}_col:xxx` — 集合

将代码直接发送给 bot 即可获取文件！{group_links}{master_info}"""

    await _retry_send(update.message.reply_text, help_text, parse_mode="Markdown", disable_web_page_preview=True)


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stop 停止所有发送任务"""
    chat_id = update.effective_chat.id
    bot_username = context.bot.username
    stopped_count = 0

    # 1. 停止自动发送标记
    context.user_data['stop_auto_send'] = True

    # 2. 通过 cancel_chat 取消队列中该用户所有任务 + 标记取消（阻止正在发送的后续任务）
    from send_queue import get_queue_from_context
    try:
        queue = get_queue_from_context(context)
        current_chat = getattr(queue, '_current_chat_id', None)
        current_send = getattr(queue, '_current_send_task', None)
        current_done = current_send.done() if current_send else None
        logger.info("/stop: @%s queue pending=%s current_chat=%s current_done=%s",
                    bot_username, queue.pending, current_chat, current_done)
        stopped_count = queue.cancel_chat(chat_id)
        if stopped_count > 0:
            logger.info("/stop: @%s 取消了 chat_id=%s 的 %d 个任务", bot_username, chat_id, stopped_count)
    except Exception as e:
        logger.warning("/stop: 取消队列任务失败: %s", e)

    if stopped_count > 0:
        await _retry_send(update.message.reply_text, f"⏹ 已停止！取消了 {stopped_count} 个待发送任务。")
    else:
        await _retry_send(update.message.reply_text, "⏹ 已停止。当前没有正在发送的任务。")


async def create_collection_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/create 创建集合"""
    user_id = update.effective_user.id
    bot_username = context.bot.username
    bot_db_id = context.bot_data.get('bot_record', {}).get('id')

    if context.user_data.get('creating_collection'):
        await _retry_send(update.message.reply_text, "⚠️ 你已有正在创建的集合，请先 `/done` 完成或 `/cancel` 取消。")
        return

    name = ' '.join(context.args) if context.args else f"集合_{datetime.now().strftime('%m%d%H%M')}"
    code_prefix = get_code_prefix(bot_username)
    raw_code = generate_raw_code()
    full_code = f"{code_prefix}_col:{raw_code}"

    if await create_collection(full_code, bot_username, name, user_id, bot_db_id=bot_db_id):
        context.user_data['creating_collection'] = full_code
        context.user_data['collection_count'] = 0

        safe_name = escape_markdown(name)
        await _retry_send(update.message.reply_text,
            f"✅ 集合「{safe_name}」创建成功！\n\n"
            f"📦 代码: `{full_code}`\n\n"
            f"👉 请连续发送要添加的文件（图片/视频/音频/文档），"
            f"最多 {MAX_COLLECTION_FILES} 个。\n"
            f"✅ 发送 `/done` 完成添加\n"
            f"❌ 发送 `/cancel` 取消集合",
            parse_mode="Markdown"
        )
    else:
        await _retry_send(update.message.reply_text, "❌ 创建集合失败，请重试。")


async def done_collection_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/done 完成集合或打包"""
    # 优先检查打包模式
    pack_code = context.user_data.get('packing_collection')
    if pack_code:
        count = context.user_data.get('packing_count', 0)
        if count == 0:
            await delete_collection(pack_code)
            await _retry_send(update.message.reply_text, "⚠️ 打包集合为空，已自动取消。")
        else:
            await complete_collection(pack_code, count)
            col_info = await get_collection(pack_code)
            col_name = col_info['name'] if col_info else "未命名"
            safe_name = escape_markdown(col_name)
            await _retry_send(update.message.reply_text,
                f"🎉 打包集合「{safe_name}」创建完成！\n\n"
                f"📦 代码: `{pack_code}`\n"
                f"📊 共 {count} 个文件\n\n"
                f"将代码发送给 bot 即可获取所有文件。",
                parse_mode="Markdown"
            )
        context.user_data.pop('packing_collection', None)
        context.user_data.pop('packing_count', None)
        context.user_data.pop('packing_codes', None)
        return

    col_code = context.user_data.get('creating_collection')
    if not col_code:
        await _retry_send(update.message.reply_text, "⚠️ 你没有正在创建的集合。发送 `/create 名称` 或 `/pack 名称` 开始。")
        return

    count = context.user_data.get('collection_count', 0)
    if count == 0:
        await delete_collection(col_code)
        await _retry_send(update.message.reply_text, "⚠️ 集合为空，已自动取消。")
    else:
        await complete_collection(col_code, count)
        col_info = await get_collection(col_code)
        col_name = col_info['name'] if col_info else "未命名"
        safe_name = escape_markdown(col_name)
        await _retry_send(update.message.reply_text,
            f"🎉 集合「{safe_name}」创建完成！\n\n"
            f"📦 代码: `{col_code}`\n"
            f"📊 共 {count} 个文件\n\n"
            f"将代码发送给 bot 即可获取所有文件。",
            parse_mode="Markdown"
        )

    context.user_data.pop('creating_collection', None)
    context.user_data.pop('collection_count', None)


async def cancel_collection_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel 取消当前操作"""
    # 优先检查打包模式
    pack_code = context.user_data.get('packing_collection')
    if pack_code:
        await delete_collection(pack_code)
        context.user_data.pop('packing_collection', None)
        context.user_data.pop('packing_count', None)
        context.user_data.pop('packing_codes', None)
        await _retry_send(update.message.reply_text, "❌ 已取消打包。")
        return

    col_code = context.user_data.get('creating_collection')
    if col_code:
        await delete_collection(col_code)
        context.user_data.pop('creating_collection', None)
        context.user_data.pop('collection_count', None)
        await _retry_send(update.message.reply_text, "❌ 已取消当前集合。")
    else:
        context.user_data['stop_auto_send'] = True
        await _retry_send(update.message.reply_text, "❌ 已停止当前操作。")


async def my_collections_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mycol 查看我的集合"""
    user_id = update.effective_user.id
    bot_db_id = context.bot_data.get('bot_record', {}).get('id')
    rows = await get_user_collections(user_id, bot_db_id=bot_db_id)

    if not rows:
        await _retry_send(update.message.reply_text, "📦 你还没有创建任何集合。")
        return

    text = "📦 *我的集合列表：*\n\n"
    for r in rows:
        status_icon = "✅" if r['status'] == 'completed' else "🔧"
        safe_name = escape_markdown(r['name'])
        text += (
            f"{status_icon} *{safe_name}*\n"
            f"  代码: `{r['code']}`\n"
            f"  文件数: {r['file_count']} | 创建于: {r['created_at']}\n\n"
        )

    await _retry_send(update.message.reply_text, text, parse_mode="Markdown")


async def delete_collection_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/delcol 删除集合"""
    user_id = update.effective_user.id
    if not context.args:
        code_prefix = get_code_prefix(context.bot.username)
        await _retry_send(update.message.reply_text, f"请提供集合代码。\n用法: `/delcol {code_prefix}_col:xxx`", parse_mode="Markdown")
        return

    col_code = context.args[0]
    col_info = await get_collection(col_code)
    if not col_info:
        await _retry_send(update.message.reply_text, "❌ 集合不存在。")
        return
    from config import ADMIN_IDS
    if col_info['user_id'] != user_id and user_id not in ADMIN_IDS:
        await _retry_send(update.message.reply_text, "⛔ 你没有权限删除此集合。")
        return

    await delete_collection(col_code)
    await _retry_send(update.message.reply_text, "✅ 集合已删除。")


async def get_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/getid 回复消息获取文件ID"""
    if not update.message.reply_to_message:
        await _retry_send(update.message.reply_text, "请回复一条包含媒体的消息来获取其ID。\n用法: 回复消息 + `/getid`", parse_mode="Markdown")
        return

    replied = update.message.reply_to_message
    bot_username = context.bot.username
    user_id = update.effective_user.id
    code_prefix = get_code_prefix(bot_username)
    bot_db_id = context.bot_data.get('bot_record', {}).get('id')
    result = None
    file_type = None
    file_unique_id = ''

    if replied.photo:
        photo = replied.photo[len(replied.photo) - 1]
        result = await save_file(user_id, 'photo', photo.file_id, photo.file_size or 0, photo.file_unique_id or '', bot_username, code_prefix, bot_db_id=bot_db_id)
        file_type = '图片'
        file_unique_id = photo.file_unique_id or ''
    elif replied.video:
        result = await save_file(user_id, 'video', replied.video.file_id, replied.video.file_size or 0, replied.video.file_unique_id or '', bot_username, code_prefix, bot_db_id=bot_db_id)
        file_type = '视频'
        file_unique_id = replied.video.file_unique_id or ''
    elif replied.audio:
        result = await save_file(user_id, 'audio', replied.audio.file_id, replied.audio.file_size or 0, replied.audio.file_unique_id or '', bot_username, code_prefix, bot_db_id=bot_db_id)
        file_type = '音频'
        file_unique_id = replied.audio.file_unique_id or ''
    elif replied.document:
        result = await save_file(user_id, 'document', replied.document.file_id, replied.document.file_size or 0, replied.document.file_unique_id or '', bot_username, code_prefix, bot_db_id=bot_db_id)
        file_type = '文档'
        file_unique_id = replied.document.file_unique_id or ''
    elif replied.voice:
        result = await save_file(user_id, 'voice', replied.voice.file_id, replied.voice.file_size or 0, replied.voice.file_unique_id or '', bot_username, code_prefix, bot_db_id=bot_db_id)
        file_type = '语音'
        file_unique_id = replied.voice.file_unique_id or ''
    else:
        await _retry_send(update.message.reply_text, "❌ 回复的消息不包含可识别的媒体文件。")
        return

    if result:
        uid_info = f" file_unique_id: `{file_unique_id}`" if file_unique_id else ""
        await _retry_send(update.message.reply_text,
            f"✅ {file_type}ID已保存！{uid_info}\n\n代码: `{result}`\n\n将此代码发送给 `@{bot_username}` 即可获取文件。",
            parse_mode="Markdown",
            reply_to_message_id=update.message.reply_to_message.message_id
        )
    else:
        await _retry_send(update.message.reply_text, "❌ 保存失败，请重试。")


@admin_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/stats 管理员统计"""
    stats = await get_stats()
    type_text = "\n".join(f"  {FILE_TYPE_MAP.get(r['file_type'], r['file_type'])}: {r['c']}" for r in stats['type_stats'])
    text = (
        f"📊 *Bot 统计信息*\n\n"
        f"📁 总文件数: {stats['file_count']}\n"
        f"📦 总集合数: {stats['col_count']}\n"
        f"👥 总用户数: {stats['user_count']}\n"
        f"📅 今日新增: {stats['today_files']}\n\n"
        f"📋 按类型统计:\n{type_text}"
    )
    await _retry_send(update.message.reply_text, text, parse_mode="Markdown")


@admin_only
async def export_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/export 管理员导出数据"""
    rows = await get_all_files_for_export()
    if not rows:
        await _retry_send(update.message.reply_text, "没有数据可导出。")
        return

    output = io.StringIO()
    output.write(f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    output.write(f"总记录数: {len(rows)}\n\n")
    output.write("code\ttype\tsize\tuser_id\tcreated_at\n")
    for r in rows:
        output.write(f"{r['code']}\t{r['file_type']}\t{r['file_size']}\t{r['user_id']}\t{r['created_at']}\n")

    bytes_io = io.BytesIO(output.getvalue().encode('utf-8'))
    await _retry_send(
        context.bot.send_document,
        chat_id=update.message.chat_id,
        document=bytes_io,
        filename=f"fileid_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
        caption=f"导出完成，共 {len(rows)} 条记录。"
    )


async def pack_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/pack 通过代码打包创建集合"""
    user_id = update.effective_user.id
    bot_username = context.bot.username
    bot_db_id = context.bot_data.get('bot_record', {}).get('id')

    if context.user_data.get('packing_collection'):
        await _retry_send(update.message.reply_text,
            "⚠️ 你已在打包模式中。\n\n"
            "• 继续发送代码（一行一个）\n"
            "• `/done` 完成打包\n"
            "• `/cancel` 取消打包"
        )
        return

    if context.user_data.get('creating_collection'):
        await _retry_send(update.message.reply_text, "⚠️ 你已在创建集合模式中，请先 `/done` 或 `/cancel`。")
        return

    name = ' '.join(context.args) if context.args else f"打包_{datetime.now().strftime('%m%d%H%M')}"
    code_prefix = get_code_prefix(bot_username)
    raw_code = generate_raw_code()
    full_code = f"{code_prefix}_col:{raw_code}"

    max_pack_files = MAX_COLLECTION_FILES * 2

    if await create_collection(full_code, bot_username, name, user_id, bot_db_id=bot_db_id):
        context.user_data['packing_collection'] = full_code
        context.user_data['packing_count'] = 0
        context.user_data['packing_codes'] = set()

        safe_name = escape_markdown(name)
        await _retry_send(update.message.reply_text,
            f"✅ 进入打包模式！集合「{safe_name}」已创建\n\n"
            f"📦 集合代码: `{full_code}`\n"
            f"📊 容量上限: {max_pack_files} 个文件\n\n"
            f"👉 请发送文件代码（一行一个），例如：\n"
            f"`{code_prefix}_p:xxx`\n"
            f"`{code_prefix}_v:yyy`\n\n"
            f"✅ 发送 `/done` 完成打包\n"
            f"❌ 发送 `/cancel` 取消打包",
            parse_mode="Markdown"
        )
    else:
        await _retry_send(update.message.reply_text, "❌ 创建打包集合失败，请重试。")


async def ex_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ex 管理员专用 — 队列发送当前Bot的最近文件
    非管理员完全静默，无任何回应。
    
    用法:
        /ex          — 发送最后100个文件（全部类型）
        /ex p        — 发送最后100个图片
        /ex v        — 发送最后100个视频
        /ex d        — 发送最后100个文档/其他
        /ex 50       — 发送最后50个文件
        /ex p50      — 发送最后50个图片
        /ex p100-200 — 发送第100~200个图片
    """
    # 权限检查 — 非管理员完全静默
    from config import ADMIN_IDS
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return  # 静默，不给任何回应

    bot_db_id = context.bot_data.get('bot_record', {}).get('id')
    if not bot_db_id:
        return  # 无 Bot 记录，静默

    # 解析参数
    import re
    from db.files import get_recent_files_for_bot
    from send_queue import get_queue_from_context, split_files_to_batches

    args_str = ' '.join(context.args) if context.args else ''

    # 正则: 可选类型字母 + 可选范围
    # 类型: p=photo, v=video, d=document, 空=全部
    # 范围: N (最后N条) 或 N-M (第N到第M条) 或 空(默认100)
    match = re.match(r'^([pvd])?(\d+)?(?:-(\d+))?$', args_str.strip())
    if not match:
        await _retry_send(update.message.reply_text,
            "📤 <b>/ex 用法</b>\n\n"
            "• <code>/ex</code> — 最后100个文件\n"
            "• <code>/ex p</code> — 最后100个图片\n"
            "• <code>/ex v</code> — 最后100个视频\n"
            "• <code>/ex d</code> — 最后100个文档\n"
            "• <code>/ex 50</code> — 最后50个文件\n"
            "• <code>/ex p50</code> — 最后50个图片\n"
            "• <code>/ex p100-200</code> — 第100~200个图片",
            parse_mode="HTML"
        )
        return

    type_char = match.group(1)  # p/v/d/None
    num1 = int(match.group(2)) if match.group(2) else None
    num2 = int(match.group(3)) if match.group(3) else None

    # 确定类型
    file_type = None
    if type_char == 'p':
        file_type = 'photo'
    elif type_char == 'v':
        file_type = 'video'
    elif type_char == 'd':
        file_type = 'document'

    # 确定范围
    if num1 is not None and num2 is not None:
        # N-M 格式: offset=N-1, limit=M-N+1
        offset = num1 - 1 if num1 > 0 else 0
        limit = max(num2 - num1 + 1, 1)
    elif num1 is not None:
        # N 格式: 最后N条
        offset = 0
        limit = num1
    else:
        # 默认: 最后100条
        offset = 0
        limit = 100

    # 上限保护
    limit = min(limit, 500)

    # 查询数据库
    files = await get_recent_files_for_bot(bot_db_id, file_type=file_type, offset=offset, limit=limit)
    if not files:
        type_label = {'photo': '图片', 'video': '视频', 'document': '文档/其他'}.get(file_type, '文件')
        await _retry_send(update.message.reply_text, f"📭 没有找到{type_label}记录。")
        return

    # 构建发送格式（send_batch 需要的格式）
    send_files = [
        {'file_type': f['file_type'], 'telegram_file_id': f['telegram_file_id'], 'code': f['code']}
        for f in files if f.get('telegram_file_id')
    ]

    if not send_files:
        await _retry_send(update.message.reply_text, "📭 没有可发送的文件。")
        return

    # 使用发送队列（全部异步提交，不阻塞 handler，允许 /stop 立即生效）
    chat_id = update.effective_chat.id
    queue = get_queue_from_context(context)
    batches = split_files_to_batches(send_files)

    type_label = {'photo': '图片', 'video': '视频', 'document': '文档/其他'}.get(file_type, '全部类型')

    # 提交所有批次到队列（不等待发送完成）
    # /ex 是管理员专用命令，不受转发保护和自动删除限制
    for batch in batches:
        queue.submit_batch_async(chat_id, batch)

    logger.info("/ex: @%s 已提交 %d 个文件（%d 批）到队列", context.bot.username, len(send_files), len(batches))

    await _retry_send(update.message.reply_text,
        f"📤 已排队 {len(send_files)} 个{type_label}（{len(batches)} 批），正在后台发送…\n"
        f"💡 发送 /stop 可随时停止。"
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/settings 用户偏好设置
    
    当 Bot 主人设置了 forward_mode=1（用户自定义）时，
    用户可以选择是否开启转发保护（禁止转发/保存图片视频）
    """
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    user_id = update.effective_user.id
    bot_record = context.bot_data.get('bot_record', {})
    bot_db_id = bot_record.get('id')

    if not bot_db_id:
        await _retry_send(update.message.reply_text, "⚠️ 无 Bot 记录，无法设置。")
        return

    # 直接从内存中的 bot_record 读取，0 次 DB 查询
    forward_mode = bot_record.get('forward_mode', 0)

    if forward_mode != 1:
        mode_labels = {
            0: '✅ Bot 主人已开放转发权限，你可以自由转发/保存。',
            -1: '🚫 Bot 主人已禁止转发，所有图片和视频不可转发/保存。',
        }
        await _retry_send(update.message.reply_text,
            f"⚙️ <b>设置</b>\n\n{mode_labels.get(forward_mode, '当前无可用设置。')}",
            parse_mode="HTML"
        )
        return

    # forward_mode == 1（用户自定义），让用户选择
    current_protect = await get_user_forward_protect(user_id, bot_db_id)

    text = (
        "⚙️ <b>转发保护设置</b>\n\n"
        "Bot 主人允许你自定义是否保护图片和视频：\n\n"
        f"当前状态：{'🚫 已开启保护（不可转发/保存）' if current_protect else '✅ 已关闭保护（可自由转发/保存）'}\n\n"
        "点击下方按钮切换："
    )

    if current_protect:
        btn_text = "✅ 关闭保护（允许转发/保存）"
        btn_data = f"ufwd|{bot_db_id}|0"
    else:
        btn_text = "🚫 开启保护（禁止转发/保存）"
        btn_data = f"ufwd|{bot_db_id}|1"

    keyboard = [[InlineKeyboardButton(btn_text, callback_data=btn_data)]]
    await _retry_send(update.message.reply_text, text, parse_mode="HTML",
                      reply_markup=InlineKeyboardMarkup(keyboard))


async def user_forward_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理用户转发保护偏好设置的回调"""
    query = update.callback_query
    if not query or not query.data:
        return

    user_id = query.from_user.id
    data = query.data

    if not data.startswith("ufwd|"):
        return

    parts = data.split("|")
    try:
        bot_db_id = int(parts[1])
        protect = int(parts[2])
    except (ValueError, IndexError):
        await query.answer("❌ 数据错误", show_alert=True)
        return

    # 验证 forward_mode 仍然是 1（用户自定义）
    forward_mode = context.bot_data.get('bot_record', {}).get('forward_mode', 0)
    if forward_mode != 1:
        await query.answer("⚠️ Bot 主人已更改设置", show_alert=True)
        return

    success = await set_user_forward_protect(user_id, bot_db_id, protect)
    if success:
        if protect:
            await query.answer("已开启转发保护", show_alert=False)
            new_text = (
                "⚙️ <b>转发保护设置</b>\n\n"
                "🚫 已开启保护 — 图片和视频不可转发/保存\n\n"
                "点击下方按钮切换："
            )
            btn_text = "✅ 关闭保护（允许转发/保存）"
            btn_data = f"ufwd|{bot_db_id}|0"
        else:
            await query.answer("已关闭转发保护", show_alert=False)
            new_text = (
                "⚙️ <b>转发保护设置</b>\n\n"
                "✅ 已关闭保护 — 可以自由转发/保存图片和视频\n\n"
                "点击下方按钮切换："
            )
            btn_text = "🚫 开启保护（禁止转发/保存）"
            btn_data = f"ufwd|{bot_db_id}|1"

        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        keyboard = [[InlineKeyboardButton(btn_text, callback_data=btn_data)]]
        try:
            await query.edit_message_text(new_text, parse_mode="HTML",
                                         reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            pass
    else:
        await query.answer("❌ 设置失败，请重试", show_alert=True)
