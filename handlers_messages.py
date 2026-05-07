"""消息处理器模块（文本、附件、转发、媒体组）"""
import re
import asyncio
import logging
from datetime import datetime
from typing import Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import MAX_COLLECTION_FILES, GROUP_SEND_SIZE, FILE_TYPE_MAP, SEND_INDIVIDUAL_DELAY, SEND_BATCH_DELAY
from database import save_file, get_file, get_collection, get_collection_files, create_collection, add_file_to_collection
from utils import get_code_prefix, escape_markdown, generate_raw_code, parse_file_code, parse_collection_code
from senders import send_file_group, _retry_send


def _short_key(context, col_code: str) -> str:
    """生成短 key 用于 callback_data（Telegram 限制 64 字节）
    
    使用集合的数据库ID作为短key（c{id}），重启后仍可通过ID从数据库恢复。
    """
    if 'cb_map' not in context.bot_data:
        context.bot_data['cb_map'] = {}

    # 如果已存在映射，复用
    for k, v in context.bot_data['cb_map'].items():
        if v == col_code:
            return k

    # 使用集合的数据库ID作为短key（重启不失效）
    from database import get_collection
    col_info = get_collection(col_code)
    if col_info and col_info.get('id'):
        key = f"c{col_info['id']}"
    else:
        # 降级：使用递增索引（仅当集合不在数据库中时）
        idx = len(context.bot_data['cb_map'])
        key = f"s{idx}"

    context.bot_data['cb_map'][key] = col_code
    return key

logger = logging.getLogger(__name__)


async def handle_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理用户发送的图片/视频/音频/文档"""
    message = update.message
    user_id = update.effective_user.id
    bot_username = context.bot.username
    code_prefix = get_code_prefix(bot_username)
    creating_col = context.user_data.get('creating_collection')

    try:
        file_id, file_type, file_size, file_unique_id = _extract_file_info(message)
        if not file_id:
            await message.reply_text("❌ 不支持的文件类型。支持: 图片、视频、音频、文档。")
            return

        bot_db_id = context.bot_data.get('bot_record', {}).get('id')
        code = save_file(user_id, file_type, file_id, file_size, file_unique_id, bot_username, code_prefix, bot_db_id=bot_db_id)
        if not code:
            await message.reply_text("❌ 保存失败，请重试。")
            return

        type_name = FILE_TYPE_MAP.get(file_type, file_type)
        uid_info = f" file_unique_id: `{file_unique_id}`" if file_unique_id else ""
        reply_text = f"✅ {type_name}已保存！{uid_info}\n\n代码: `{code}`"
        reply_kwargs = {'text': reply_text, 'parse_mode': 'Markdown', 'reply_to_message_id': message.message_id}

        # 如果正在创建集合，追加文件
        if creating_col:
            current_count = context.user_data.get('collection_count', 0)
            if current_count >= MAX_COLLECTION_FILES:
                await message.reply_text(f"⚠️ 集合已满 {MAX_COLLECTION_FILES} 个文件，请发送 `/done` 完成。")
                return
            sort_order = current_count + 1
            add_file_to_collection(creating_col, code, sort_order)
            context.user_data['collection_count'] = sort_order
            reply_kwargs['text'] += f"\n\n📦 已添加到集合 ({sort_order}/{MAX_COLLECTION_FILES})"

        await message.reply_text(**reply_kwargs)
    except Exception as e:
        logger.error("处理附件失败: %s", e)
        await message.reply_text(f"❌ 处理文件时出错: {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理文本消息，解析代码并发送文件。
    
    当 Telegram 将用户的长消息切割成多条时，
    会自动收集同一用户短时间内连续发送的代码消息，合并后统一处理。
    """
    message = update.message
    if not message or not message.text:
        return

    text = message.text.strip()
    bot_username = context.bot.username
    file_codes = parse_file_code(text, bot_username)
    collection_codes = parse_collection_code(text, bot_username)

    # 旧格式兼容: $p $v $d
    legacy_file_ids = []
    if not file_codes and not collection_codes:
        for m in re.compile(r'\$([pvd])(\S+)').finditer(text):
            legacy_file_ids.append((m.group(1), m.group(2)))

    if not file_codes and not collection_codes and not legacy_file_ids:
        await message.reply_text("❓ 未识别的输入。\n\n• 发送文件获取代码\n• 发送代码获取文件\n• `/help` 查看帮助")
        return

    chat_id = message.chat_id
    user_id = message.from_user.id if message.from_user else 0

    # ===== 消息合并缓冲：收集同一用户短时间内连续发送的代码 =====
    # 当代码数量较多时，Telegram 可能会将一条消息切割成多条
    # 等待 2 秒，收集同一用户的所有代码消息后统一处理
    has_sendable_codes = bool(file_codes) or bool(legacy_file_ids)
    if has_sendable_codes:
        if 'pending_code_buffer' not in context.bot_data:
            context.bot_data['pending_code_buffer'] = {}

        buffer_key = f"{chat_id}_{user_id}"
        if buffer_key not in context.bot_data['pending_code_buffer']:
            context.bot_data['pending_code_buffer'][buffer_key] = {
                'file_codes': [],
                'legacy_file_ids': [],
                'timer': None,
                'first_message': message,
            }

        buf = context.bot_data['pending_code_buffer'][buffer_key]
        buf['file_codes'].extend(file_codes)
        buf['legacy_file_ids'].extend(legacy_file_ids)

        # 重置计时器
        if buf['timer']:
            buf['timer'].cancel()

        async def process_buffered_codes():
            """处理合并后的所有代码"""
            try:
                await asyncio.sleep(2)  # 等待2秒收集可能的后续消息

                buf = context.bot_data.get('pending_code_buffer', {}).pop(buffer_key, None)
                if not buf:
                    return

                all_file_codes = buf['file_codes']
                all_legacy = buf['legacy_file_ids']
                ref_message = buf['first_message']

                logger.info("合并处理: %d 个文件代码, %d 个旧格式代码 (来自用户 %s)",
                            len(all_file_codes), len(all_legacy), user_id)

                await _process_file_codes(context, chat_id, ref_message, all_file_codes)

                # 处理旧格式
                if all_legacy:
                    await _process_legacy_codes(context, chat_id, all_legacy)

            except Exception as e:
                logger.error("process_buffered_codes 失败: %s", e, exc_info=True)
                # 清理
                context.bot_data.get('pending_code_buffer', {}).pop(buffer_key, None)

        buf['timer'] = asyncio.create_task(process_buffered_codes())

        # 集合代码不需要缓冲，立即处理
        if collection_codes:
            await _process_collection_codes(context, chat_id, message, collection_codes)

        return  # 文件代码已缓冲，等待后续消息

    # ===== 无文件代码，只有集合代码，直接处理 =====
    if collection_codes:
        await _process_collection_codes(context, chat_id, message, collection_codes)


async def _process_file_codes(context, chat_id, message, file_codes: list) -> None:
    """处理文件代码发送（带分批+进度反馈）"""
    if not file_codes:
        return

    files, not_found = [], []
    current_bot_db_id = context.bot_data.get("bot_record", {}).get("id")
    for code in file_codes:
        f = get_file(code)
        if f and (not current_bot_db_id or not f.get("bot_db_id") or f["bot_db_id"] == current_bot_db_id):
            files.append(f)
        else:
            not_found.append(code)

    total_sent = 0
    if files:
        total_files = len(files)
        # 文件数较多时，按类型分类后分批发送并显示进度
        if total_files > GROUP_SEND_SIZE:
            # 先按类型分类（与 send_file_group 内部逻辑一致）
            pv_files = [f for f in files if f['file_type'] in ('photo', 'video')]
            doc_files = [f for f in files if f['file_type'] == 'document']
            audio_files = [f for f in files if f['file_type'] in ('audio', 'voice')]
            type_summary = []
            if pv_files:
                type_summary.append(f"🖼🎬 {len(pv_files)}个图片/视频")
            if doc_files:
                type_summary.append(f"📄 {len(doc_files)}个文档")
            if audio_files:
                type_summary.append(f"🎵 {len(audio_files)}个音频")

            status_msg = await message.reply_text(
                f"📤 准备发送 {total_files} 个文件\n📋 {', '.join(type_summary)}\n\n⏳ 正在发送... (0/{total_files})"
            )
            batch_num = 0

            # 按类型依次分批发送（同类型可合并为相册/组）
            for type_label, type_files in [("图片/视频", pv_files), ("文档", doc_files), ("音频", audio_files)]:
                if not type_files:
                    continue
                for i in range(0, len(type_files), GROUP_SEND_SIZE):
                    batch = type_files[i:i + GROUP_SEND_SIZE]
                    try:
                        sent = await send_file_group(context, chat_id, batch)
                        total_sent += sent
                        batch_num += 1
                    except Exception as e:
                        logger.error("发送%s失败 (batch %d): %s", type_label, batch_num, e, exc_info=True)

                    # 更新进度
                    is_last_batch = (type_files is audio_files and i + GROUP_SEND_SIZE >= len(type_files)) or \
                                    (not audio_files and type_files is doc_files and i + GROUP_SEND_SIZE >= len(type_files)) or \
                                    (not audio_files and not doc_files and i + GROUP_SEND_SIZE >= len(type_files))
                    if is_last_batch or batch_num % 2 == 0:
                        try:
                            await context.bot.edit_message_text(
                                chat_id=chat_id,
                                message_id=status_msg.message_id,
                                text=f"📤 正在发送{type_label}... ({total_sent}/{total_files})"
                            )
                        except Exception:
                            pass

                    # 批间延迟
                    await asyncio.sleep(SEND_BATCH_DELAY)

            # 发送完成，更新最终状态
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg.message_id,
                    text=f"✅ 发送完成！成功 {total_sent}/{total_files}"
                )
            except Exception:
                pass
        else:
            # 文件数少，直接发送
            try:
                total_sent += await send_file_group(context, chat_id, files)
            except Exception as e:
                logger.error("发送文件失败: %s", e)
                await message.reply_text(f"❌ 发送文件时出错: {e}")

    if not_found:
        max_show = 20
        shown = not_found[:max_show]
        not_found_text = "\n".join(f"• `{c}`" for c in shown)
        if len(not_found) > max_show:
            not_found_text += f"\n... 等 {len(not_found)} 个"
        await message.reply_text(f"⚠️ 以下代码未找到 ({len(not_found)} 个):\n" + not_found_text, parse_mode="Markdown")


async def _process_collection_codes(context, chat_id, message, collection_codes: list) -> None:
    """处理集合代码"""
    for col_code in collection_codes:
        col_info = get_collection(col_code)
        current_bot_db_id_col = context.bot_data.get("bot_record", {}).get("id")
        if not col_info or (current_bot_db_id_col and col_info.get("bot_db_id") and col_info["bot_db_id"] != current_bot_db_id_col):
            await message.reply_text(f"❌ 集合不存在: `{col_code}`", parse_mode="Markdown")
            continue

        safe_name = escape_markdown(col_info['name'])
        if col_info['status'] != 'completed':
            await message.reply_text(f"⚠️ 集合「{safe_name}」尚未完成。")
            continue

        files = get_collection_files(col_code)
        if not files:
            await message.reply_text(f"⚠️ 集合「{safe_name}」为空。")
            continue

        total_files = len(files)
        type_counts = {}
        for f in files:
            type_counts[f['file_type']] = type_counts.get(f['file_type'], 0) + 1
        type_stats_text = " ".join(f"{FILE_TYPE_MAP.get(k, k)}x{v}" for k, v in type_counts.items())

        sk = _short_key(context, col_code)
        col_text = f"📦 *集合「{safe_name}」*\n\n📊 共 {total_files} 个文件\n📋 {type_stats_text}\n\n请选择操作："
        keyboard = [
            [InlineKeyboardButton("⬇️ 全部发送", callback_data=f"s|{sk}")],
            [InlineKeyboardButton("▶️ 自动发送", callback_data=f"a|{sk}")],
        ]
        if total_files > GROUP_SEND_SIZE:
            keyboard.append([InlineKeyboardButton("📖 分页浏览", callback_data=f"p|{sk}|1")])

        await message.reply_text(col_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def _process_legacy_codes(context, chat_id, legacy_file_ids: list) -> None:
    """处理旧格式代码（带重试和限速）"""
    total_sent = 0
    for idx, (prefix, fid) in enumerate(legacy_file_ids):
        try:
            if prefix == 'p':
                await _retry_send(context.bot.send_photo, chat_id=chat_id, photo=fid, read_timeout=30, write_timeout=30)
            elif prefix == 'v':
                await _retry_send(context.bot.send_video, chat_id=chat_id, video=fid, read_timeout=30, write_timeout=30)
            elif prefix == 'd':
                await _retry_send(context.bot.send_document, chat_id=chat_id, document=fid, read_timeout=30, write_timeout=30)
            total_sent += 1
            # 滑动限速
            if idx < len(legacy_file_ids) - 1:
                await asyncio.sleep(SEND_INDIVIDUAL_DELAY)
        except Exception as e:
            logger.error("旧格式发送失败（已重试）: %s", e)


async def handle_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理转发的非媒体消息"""
    message = update.message
    if message.document or message.photo or message.video or message.audio or message.voice:
        await handle_attachment(update, context)
    elif message.text:
        await handle_text(update, context)
    else:
        await message.reply_text("请转发包含媒体的消息，我会返回其代码。")


async def handle_group_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理媒体组（用户一次性发送多个媒体）"""
    message = update.message
    if not message:
        return

    logger.info("handle_group_media 触发: photo=%s, video=%s, document=%s, audio=%s, voice=%s, media_group_id=%s",
                bool(message.photo), bool(message.video), bool(message.document),
                bool(message.audio), bool(message.voice), message.media_group_id)

    media_group_id = message.media_group_id
    if not media_group_id:
        await handle_attachment(update, context)
        return

    # 收集同组媒体
    if 'pending_media_groups' not in context.bot_data:
        context.bot_data['pending_media_groups'] = {}

    if media_group_id not in context.bot_data['pending_media_groups']:
        context.bot_data['pending_media_groups'][media_group_id] = {'messages': [], 'timer': None}

    group_data = context.bot_data['pending_media_groups'][media_group_id]
    group_data['messages'].append(message)
    if group_data['timer']:
        group_data['timer'].cancel()

    async def process():
        try:
            await asyncio.sleep(2)
            msgs = group_data['messages']
            if not msgs:
                return
            codes = await _save_media_messages(msgs, context)
            if codes:
                creating_col = context.user_data.get('creating_collection')
                if creating_col:
                    await _add_to_collection(context, creating_col, codes)
                    count = context.user_data.get('collection_count', 0)
                    reply = f"✅ 媒体组已保存并添加到集合！\n\n共 {len(codes)} 个文件 ({count}/{MAX_COLLECTION_FILES})\n\n"
                else:
                    reply = f"✅ 媒体组已保存！共 {len(codes)} 个文件：\n\n"
                reply += "\n".join(f"`{c}`" for c in codes)
                await msgs[0].reply_text(reply, parse_mode="Markdown")
        except Exception as e:
            logger.error("process_media_group 失败: %s", e, exc_info=True)
        finally:
            context.bot_data.get('pending_media_groups', {}).pop(media_group_id, None)

    group_data['timer'] = asyncio.create_task(process())


async def handle_forwarded_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理转发的媒体消息（自动为媒体组创建集合）"""
    message = update.message
    if not message:
        return

    logger.info("转发媒体: media_group_id=%s, photo=%s, video=%s, doc=%s",
                message.media_group_id, bool(message.photo), bool(message.video), bool(message.document))

    has_media = message.document or message.photo or message.video or message.audio or message.voice
    if not has_media:
        if message.text:
            await handle_text(update, context)
        else:
            await message.reply_text("请转发包含媒体的消息。")
        return

    media_group_id = message.media_group_id
    if not media_group_id:
        # 单个转发，直接处理
        await handle_attachment(update, context)
        return

    # 媒体组：收集后自动创建集合
    if 'pending_forward_groups' not in context.bot_data:
        context.bot_data['pending_forward_groups'] = {}

    if media_group_id not in context.bot_data['pending_forward_groups']:
        context.bot_data['pending_forward_groups'][media_group_id] = {'messages': [], 'timer': None}

    group_data = context.bot_data['pending_forward_groups'][media_group_id]
    group_data['messages'].append(message)
    if group_data['timer']:
        group_data['timer'].cancel()

    async def process():
        try:
            await asyncio.sleep(2)
            msgs = group_data['messages']
            if not msgs:
                return

            codes = await _save_media_messages(msgs, context)
            if not codes:
                await msgs[0].reply_text("❌ 转发的媒体组处理失败。")
                return

            # 自动创建集合
            uid = msgs[0].from_user.id
            bname = context.bot.username
            code_prefix = get_code_prefix(bname)
            col_name = f"转发组_{datetime.now().strftime('%m%d%H%M')}"
            full_col_code = f"{code_prefix}_col:{generate_raw_code()}"

            # 保存集合到数据库
            from database import get_db
            bot_db_id = context.bot_data.get('bot_record', {}).get('id')
            conn = get_db()
            try:
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "INSERT INTO collections (code, bot_username, name, user_id, file_count, status, created_at, updated_at, bot_db_id) VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?)",
                    (full_col_code, bname, col_name, uid, len(codes), now, now, bot_db_id)
                )
                for i, code in enumerate(codes):
                    conn.execute("INSERT INTO collection_items (collection_code, file_code, sort_order) VALUES (?, ?, ?)", (full_col_code, code, i + 1))
                conn.commit()
            except Exception as e:
                logger.error("自动创建转发集合失败: %s", e)
                reply = f"✅ 转发媒体已保存（共 {len(codes)} 个）：\n\n" + "\n".join(f"`{c}`" for c in codes)
                await msgs[0].reply_text(reply, parse_mode="Markdown")
                return
            finally:
                conn.close()

            # 回复
            safe_name = escape_markdown(col_name)
            reply = f"✅ 转发媒体组已保存并自动创建集合！\n\n📦 集合: *{safe_name}*\n📊 共 {len(codes)} 个文件\n📦 集合代码: `{full_col_code}`\n\n单个文件代码：\n"
            reply += "\n".join(f"`{c}`" for c in codes)
            sk = _short_key(context, full_col_code)
            keyboard = [[InlineKeyboardButton("⬇️ 全部发送", callback_data=f"s|{sk}"), InlineKeyboardButton("▶️ 自动发送", callback_data=f"a|{sk}")]]
            try:
                await msgs[0].reply_text(reply, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
            except Exception:
                await msgs[0].reply_text(reply, parse_mode="Markdown")

        except Exception as e:
            logger.error("process_forward_group 失败: %s", e, exc_info=True)
        finally:
            context.bot_data.get('pending_forward_groups', {}).pop(media_group_id, None)

    group_data['timer'] = asyncio.create_task(process())


# ==================== 内部辅助函数 ====================

def _extract_file_info(message) -> tuple:
    """从消息中提取文件信息，返回 (file_id, file_type, file_size, file_unique_id)"""
    if message.photo:
        photo = message.photo[len(message.photo) - 1]
        return photo.file_id, 'photo', photo.file_size or 0, photo.file_unique_id or ''
    elif message.video:
        return message.video.file_id, 'video', message.video.file_size or 0, message.video.file_unique_id or ''
    elif message.audio:
        return message.audio.file_id, 'audio', message.audio.file_size or 0, message.audio.file_unique_id or ''
    elif message.document:
        return message.document.file_id, 'document', message.document.file_size or 0, message.document.file_unique_id or ''
    elif message.voice:
        return message.voice.file_id, 'voice', message.voice.file_size or 0, message.voice.file_unique_id or ''
    return None, None, 0, ''


async def _save_media_messages(messages, context) -> list:
    """批量保存媒体消息，返回代码列表"""
    uid = messages[0].from_user.id
    bname = context.bot.username
    code_prefix = get_code_prefix(bname)
    bot_db_id = context.bot_data.get('bot_record', {}).get('id')
    codes = []

    for msg in messages:
        file_id, file_type, file_size, file_unique_id = _extract_file_info(msg)
        if file_id and file_type:
            code = save_file(uid, file_type, file_id, file_size, file_unique_id, bname, code_prefix, bot_db_id=bot_db_id)
            if code:
                codes.append(code)
    return codes


async def _add_to_collection(context, col_code, codes):
    """将代码列表添加到集合"""
    current_count = context.user_data.get('collection_count', 0)
    for i, code in enumerate(codes):
        if current_count + i + 1 > MAX_COLLECTION_FILES:
            break
        add_file_to_collection(col_code, code, current_count + i + 1)
    new_count = min(current_count + len(codes), MAX_COLLECTION_FILES)
    context.user_data['collection_count'] = new_count