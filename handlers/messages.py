"""消息处理器模块（文本、附件、转发、媒体组）"""
import asyncio
import logging
import time as _time
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import MAX_COLLECTION_FILES, GROUP_SEND_SIZE, FILE_TYPE_MAP
from config import RATE_LIMIT_WINDOW, RATE_LIMIT_MAX, RATE_LIMIT_MAX_WAIT
from db import (
    save_file, get_file, get_files_by_codes, get_collection, get_collection_files,
    create_collection, add_file_to_collection, get_user_forward_protect,
)
from utils import get_code_prefix, escape_markdown, generate_raw_code, parse_file_code, parse_collection_code
from senders import send_file_group, _retry_send
from send_queue import get_queue_from_context, split_files_to_batches


async def _short_key(context, col_code: str) -> str:
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
    col_info = await get_collection(col_code)
    if col_info and col_info.get('id'):
        key = f"c{col_info['id']}"
    else:
        # 降级：使用递增索引（仅当集合不在数据库中时）
        idx = len(context.bot_data['cb_map'])
        key = f"s{idx}"

    context.bot_data['cb_map'][key] = col_code
    return key

logger = logging.getLogger(__name__)


async def _rate_limit_wait(user_id: int, bot_username: str) -> bool:
    """用户级限流（排队模式）。
    Returns: True 可以继续, False 超时（不应继续处理）
    """
    try:
        from redis_manager import get_redis
        r = await get_redis()
        key = f"rate:user:{bot_username}:{user_id}"
        return await r.rate_limit_wait(key, RATE_LIMIT_MAX, RATE_LIMIT_WINDOW, RATE_LIMIT_MAX_WAIT)
    except Exception:
        return True  # 降级时放行


async def handle_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理用户发送的图片/视频/音频/文档
    
    当用户快速连续发送多个附件时，会自动合并回复（3秒内所有附件合并为一条消息），
    避免高频发送 API 导致 Telegram 限流。
    """
    t0 = _time.monotonic()
    message = update.message
    user_id = update.effective_user.id
    bot_username = context.bot.username

    # 用户级限流（排队等待）
    if not await _rate_limit_wait(user_id, bot_username):
        await _retry_send(message.reply_text, "⚠️ 操作太频繁，请稍后再试。")
        logger.warning("⏱ handle_attachment 限流拒绝 user=%s bot=%s 耗时%.1fs", user_id, bot_username, _time.monotonic() - t0)
        return
    logger.debug("⏱ handle_attachment rate_limit user=%s 耗时%.3fs", user_id, _time.monotonic() - t0)

    code_prefix = get_code_prefix(bot_username)
    creating_col = context.user_data.get('creating_collection')

    try:
        file_id, file_type, file_size, file_unique_id = _extract_file_info(message)
        if not file_id:
            await _retry_send(message.reply_text, "❌ 不支持的文件类型。支持: 图片、视频、音频、文档。")
            return

        t1 = _time.monotonic()
        bot_db_id = context.bot_data.get('bot_record', {}).get('id')
        code = await save_file(user_id, file_type, file_id, file_size, file_unique_id, bot_username, code_prefix, bot_db_id=bot_db_id)
        logger.debug("⏱ handle_attachment save_file user=%s 耗时%.3fs", user_id, _time.monotonic() - t1)
        if not code:
            await _retry_send(message.reply_text, "❌ 保存失败，请重试。")
            return

        type_name = FILE_TYPE_MAP.get(file_type, file_type)

        # 如果正在创建集合，追加文件（立即回复，不缓冲）
        if creating_col:
            current_count = context.user_data.get('collection_count', 0)
            if current_count >= MAX_COLLECTION_FILES:
                await _retry_send(message.reply_text, f"⚠️ 集合已满 {MAX_COLLECTION_FILES} 个文件，请发送 `/done` 完成。")
                return
            sort_order = current_count + 1
            t2 = _time.monotonic()
            await add_file_to_collection(creating_col, code, sort_order)
            logger.debug("⏱ handle_attachment add_file_to_collection 耗时%.3fs", _time.monotonic() - t2)
            context.user_data['collection_count'] = sort_order
            uid_info = f" file_unique_id: `{file_unique_id}`" if file_unique_id else ""
            await _retry_send(message.reply_text,
                text=f"✅ {type_name}已保存！{uid_info}\n\n代码: `{code}`\n\n📦 已添加到集合 ({sort_order}/{MAX_COLLECTION_FILES})",
                parse_mode='Markdown', reply_to_message_id=message.message_id)
            return

        # ===== 附件回复缓冲：合并快速连续发送的附件回复 =====
        if '_attachment_reply_buffer' not in context.bot_data:
            context.bot_data['_attachment_reply_buffer'] = {}

        chat_id = message.chat_id
        buffer_key = f"{chat_id}_{user_id}"

        if buffer_key not in context.bot_data['_attachment_reply_buffer']:
            context.bot_data['_attachment_reply_buffer'][buffer_key] = {
                'items': [],       # [(type_name, code, message_id), ...]
                'timer': None,
                'chat_id': chat_id,
            }

        buf = context.bot_data['_attachment_reply_buffer'][buffer_key]
        buf['items'].append((type_name, code, message.message_id))

        # 重置计时器
        if buf['timer']:
            buf['timer'].cancel()

        async def flush_attachment_replies():
            """3秒后合并发送所有附件回复"""
            try:
                await asyncio.sleep(3)
                buf = context.bot_data.get('_attachment_reply_buffer', {}).pop(buffer_key, None)
                if not buf or not buf['items']:
                    return

                items = buf['items']
                chat_id = buf['chat_id']

                if len(items) == 1:
                    # 单个附件，直接回复
                    tn, c, mid = items[0]
                    await _retry_send(context.bot.send_message,
                        chat_id=chat_id,
                        text=f"✅ {tn}已保存！\n\n代码: `{c}`",
                        parse_mode='Markdown',
                        reply_to_message_id=mid)
                else:
                    # 多个附件，合并为一条消息
                    lines = []
                    for tn, c, mid in items:
                        lines.append(f"• {tn}: `{c}`")
                    reply = f"✅ 已保存 {len(items)} 个文件：\n\n" + "\n".join(lines)
                    # reply_to 第一条消息
                    await _retry_send(context.bot.send_message,
                        chat_id=chat_id,
                        text=reply,
                        parse_mode='Markdown',
                        reply_to_message_id=items[0][2])

                logger.info("附件回复缓冲: 合并发送 %d 个附件到 chat_id=%s", len(items), chat_id)

            except Exception as e:
                logger.error("flush_attachment_replies 失败: %s", e, exc_info=True)
                context.bot_data.get('_attachment_reply_buffer', {}).pop(buffer_key, None)

        buf['timer'] = asyncio.create_task(flush_attachment_replies())
        logger.debug("⏱ handle_attachment 总耗时%.3fs user=%s bot=%s", _time.monotonic() - t0, user_id, bot_username)

    except Exception as e:
        logger.error("处理附件失败: %s", e)
        await _retry_send(message.reply_text, f"❌ 处理文件时出错: {e}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理文本消息，解析代码并发送文件。
    
    当 Telegram 将用户的长消息切割成多条时，
    会自动收集同一用户短时间内连续发送的代码消息，合并后统一处理。
    """
    t0 = _time.monotonic()
    message = update.message
    if not message or not message.text:
        return

    text = message.text.strip()
    bot_username = context.bot.username
    user_id = update.effective_user.id

    # 用户级限流（排队等待）
    if not await _rate_limit_wait(user_id, bot_username):
        await _retry_send(message.reply_text, "⚠️ 操作太频繁，请稍后再试。")
        logger.warning("⏱ handle_text 限流拒绝 user=%s bot=%s 耗时%.1fs", user_id, bot_username, _time.monotonic() - t0)
        return
    logger.debug("⏱ handle_text rate_limit user=%s 耗时%.3fs", user_id, _time.monotonic() - t0)

    # ===== 打包模式：优先处理代码打包 =====
    pack_code = context.user_data.get('packing_collection')
    if pack_code:
        await _handle_pack_text(update, context, text)
        return

    file_codes = parse_file_code(text, bot_username)
    collection_codes = parse_collection_code(text, bot_username)

    if not file_codes and not collection_codes:
        await _retry_send(message.reply_text, "❓ 未识别的输入。\n\n• 发送文件获取代码\n• 发送代码获取文件\n• `/help` 查看帮助")
        return

    chat_id = message.chat_id
    user_id = message.from_user.id if message.from_user else 0

    # ===== 消息合并缓冲：收集同一用户短时间内连续发送的代码 =====
    # 当代码数量较多时，Telegram 可能会将一条消息切割成多条
    # 等待 2 秒，收集同一用户的所有代码消息后统一处理
    if file_codes:
        if 'pending_code_buffer' not in context.bot_data:
            context.bot_data['pending_code_buffer'] = {}

        buffer_key = f"{chat_id}_{user_id}"
        if buffer_key not in context.bot_data['pending_code_buffer']:
            context.bot_data['pending_code_buffer'][buffer_key] = {
                'file_codes': [],
                'timer': None,
                'first_message': message,
            }

        buf = context.bot_data['pending_code_buffer'][buffer_key]
        buf['file_codes'].extend(file_codes)

        # 重置计时器
        if buf['timer']:
            buf['timer'].cancel()

        async def process_buffered_codes():
            """处理合并后的所有代码"""
            t1 = _time.monotonic()
            try:
                await asyncio.sleep(2)  # 等待2秒收集可能的后续消息
                logger.debug("⏱ process_buffered_codes sleep后 耗时%.3fs", _time.monotonic() - t1)

                buf = context.bot_data.get('pending_code_buffer', {}).pop(buffer_key, None)
                if not buf:
                    return

                all_file_codes = buf['file_codes']
                ref_message = buf['first_message']

                logger.info("合并处理: %d 个文件代码 (来自用户 %s)", len(all_file_codes), user_id)

                await _process_file_codes(context, chat_id, ref_message, all_file_codes)
                logger.info("⏱ process_buffered_codes 总耗时%.3fs user=%s codes=%d", _time.monotonic() - t1, user_id, len(all_file_codes))

            except Exception as e:
                logger.error("process_buffered_codes 失败: %s", e, exc_info=True)
                # 清理
                context.bot_data.get('pending_code_buffer', {}).pop(buffer_key, None)

        buf['timer'] = asyncio.create_task(process_buffered_codes())

        # 集合代码不需要缓冲，立即处理
        if collection_codes:
            t2 = _time.monotonic()
            await _process_collection_codes(context, chat_id, message, collection_codes)
            logger.debug("⏱ handle_text _process_collection_codes 耗时%.3fs", _time.monotonic() - t2)

        logger.debug("⏱ handle_text(缓冲路径) 总耗时%.3fs user=%s", _time.monotonic() - t0, user_id)
        return  # 文件代码已缓冲，等待后续消息

    # ===== 无文件代码，只有集合代码，直接处理 =====
    if collection_codes:
        t3 = _time.monotonic()
        await _process_collection_codes(context, chat_id, message, collection_codes)
        logger.debug("⏱ handle_text _process_collection_codes 耗时%.3fs", _time.monotonic() - t3)

    logger.debug("⏱ handle_text 总耗时%.3fs user=%s bot=%s", _time.monotonic() - t0, user_id, bot_username)


async def _process_file_codes(context, chat_id, message, file_codes: list) -> None:
    """处理文件代码：查 DB → 拆分批次 → 异步提交到发送队列（不阻塞 webhook）
    
    ⚠️ 使用 submit_batch_async 而非 submit_batch，避免队列消费耗时阻塞
    process_update 导致 webhook 超时(503)。
    """
    t0 = _time.monotonic()
    if not file_codes:
        return

    current_bot_db_id = context.bot_data.get("bot_record", {}).get("id")

    t1 = _time.monotonic()
    found_files = await get_files_by_codes(file_codes, current_bot_db_id)
    logger.debug("⏱ _process_file_codes get_files_by_codes(%d codes) 耗时%.3fs", len(file_codes), _time.monotonic() - t1)

    found_codes = {f['code'] for f in found_files}
    not_found = [c for c in file_codes if c not in found_codes]

    if found_files:
        total = len(found_files)
        queue = get_queue_from_context(context)
        batches = split_files_to_batches(found_files)

        # 检查转发保护（直接从内存 bot_record 读取，通常 0 次 DB 查询）
        protect = await _get_protect_async(context, chat_id)

        # 异步提交所有批次到队列（不等待发送完成，避免阻塞 webhook）
        t2 = _time.monotonic()
        for batch in batches:
            try:
                queue.submit_batch_async(chat_id, batch, protect_content=protect)
            except Exception as e:
                logger.error("队列提交失败: %s", e)
        logger.debug("⏱ _process_file_codes queue提交(%d 批) 耗时%.3fs", len(batches), _time.monotonic() - t2)

        # 发送排队提示后立即返回，webhook 快速响应
        t3 = _time.monotonic()
        if total > GROUP_SEND_SIZE:
            try:
                await _retry_send(message.reply_text,
                    f"📤 已排队 {total} 个文件（{len(batches)} 批），正在后台发送…"
                )
            except Exception:
                pass
        else:
            try:
                await _retry_send(message.reply_text,
                    f"📤 正在发送 {total} 个文件…"
                )
            except Exception:
                pass
        logger.debug("⏱ _process_file_codes _retry_send(提示) 耗时%.3fs", _time.monotonic() - t3)

        logger.info("_process_file_codes: 已异步提交 %d 个文件（%d 批）", total, len(batches))

    if not_found:
        t4 = _time.monotonic()
        max_show = 20
        shown = not_found[:max_show]
        not_found_text = "\n".join(f"• `{c}`" for c in shown)
        if len(not_found) > max_show:
            not_found_text += f"\n... 等 {len(not_found)} 个"
        await _retry_send(message.reply_text, f"⚠️ 以下代码未找到 ({len(not_found)} 个):\n" + not_found_text, parse_mode="Markdown")
        logger.debug("⏱ _process_file_codes _retry_send(not_found) 耗时%.3fs", _time.monotonic() - t4)

    logger.info("⏱ _process_file_codes 总耗时%.3fs codes=%d found=%d not_found=%d",
                _time.monotonic() - t0, len(file_codes), len(found_files), len(not_found))


async def _process_collection_codes(context, chat_id, message, collection_codes: list) -> None:
    """处理集合代码"""
    t0 = _time.monotonic()
    for col_code in collection_codes:
        t1 = _time.monotonic()
        col_info = await get_collection(col_code)
        logger.debug("⏱ _process_collection_codes get_collection(%s) 耗时%.3fs", col_code, _time.monotonic() - t1)

        current_bot_db_id_col = context.bot_data.get("bot_record", {}).get("id")
        if not col_info or (current_bot_db_id_col and col_info.get('bot_db_id') and col_info["bot_db_id"] != current_bot_db_id_col):
            await _retry_send(message.reply_text, f"❌ 集合不存在: `{col_code}`", parse_mode="Markdown")
            continue

        safe_name = escape_markdown(col_info['name'])
        if col_info['status'] != 'completed':
            await _retry_send(message.reply_text, f"⚠️ 集合「{safe_name}」尚未完成。")
            continue

        t2 = _time.monotonic()
        files = await get_collection_files(col_code)
        logger.debug("⏱ _process_collection_codes get_collection_files(%s) 耗时%.3fs files=%d", col_code, _time.monotonic() - t2, len(files) if files else 0)

        if not files:
            await _retry_send(message.reply_text, f"⚠️ 集合「{safe_name}」为空。")
            continue

        total_files = len(files)
        type_counts = {}
        for f in files:
            type_counts[f['file_type']] = type_counts.get(f['file_type'], 0) + 1
        type_stats_text = " ".join(f"{FILE_TYPE_MAP.get(k, k)}x{v}" for k, v in type_counts.items())

        t3 = _time.monotonic()
        sk = await _short_key(context, col_code)
        logger.debug("⏱ _process_collection_codes _short_key 耗时%.3fs", _time.monotonic() - t3)

        col_text = f"📦 *集合「{safe_name}」*\n\n📊 共 {total_files} 个文件\n📋 {type_stats_text}\n\n请选择操作："
        keyboard = [
            [InlineKeyboardButton("📖 分页发送", callback_data=f"s|{sk}")],
            [InlineKeyboardButton("▶️ 自动发送", callback_data=f"a|{sk}")],
        ]
        if total_files > GROUP_SEND_SIZE:
            keyboard.append([InlineKeyboardButton("📖 分页浏览", callback_data=f"p|{sk}|1")])

        t4 = _time.monotonic()
        await _retry_send(message.reply_text, col_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        logger.debug("⏱ _process_collection_codes _retry_send(集合) 耗时%.3fs", _time.monotonic() - t4)

    logger.info("⏱ _process_collection_codes 总耗时%.3fs codes=%d", _time.monotonic() - t0, len(collection_codes))


async def handle_forward(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理转发的非媒体消息"""
    message = update.message
    if message.document or message.photo or message.video or message.audio or message.voice:
        await handle_attachment(update, context)
    elif message.text:
        await handle_text(update, context)
    else:
        await _retry_send(message.reply_text, "请转发包含媒体的消息，我会返回其代码。")


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
                await _retry_send(msgs[0].reply_text, reply, parse_mode="Markdown")
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
            await _retry_send(message.reply_text, "请转发包含媒体的消息。")
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
                await _retry_send(msgs[0].reply_text, "❌ 转发的媒体组处理失败。")
                return

            # 自动创建集合
            uid = msgs[0].from_user.id
            bname = context.bot.username
            code_prefix = get_code_prefix(bname)
            col_name = f"转发组_{datetime.now().strftime('%m%d%H%M')}"
            full_col_code = f"{code_prefix}_col:{generate_raw_code()}"

            # 保存集合到数据库（使用 async ORM）
            from db import create_collection, add_file_to_collection, complete_collection
            bot_db_id = context.bot_data.get('bot_record', {}).get('id')

            save_ok = False
            try:
                if await create_collection(full_col_code, bname, col_name, uid, bot_db_id=bot_db_id):
                    for i, code in enumerate(codes):
                        await add_file_to_collection(full_col_code, code, i + 1)
                    await complete_collection(full_col_code, len(codes))
                    save_ok = True
            except Exception as e:
                logger.error("自动创建转发集合失败: %s", e)
            if not save_ok:
                reply = f"✅ 转发媒体已保存（共 {len(codes)} 个）：\n\n" + "\n".join(f"`{c}`" for c in codes)
                await _retry_send(msgs[0].reply_text, reply, parse_mode="Markdown")
                return

            # 回复
            safe_name = escape_markdown(col_name)
            reply = f"✅ 转发媒体组已保存并自动创建集合！\n\n📦 集合: *{safe_name}*\n📊 共 {len(codes)} 个文件\n📦 集合代码: `{full_col_code}`\n\n单个文件代码：\n"
            reply += "\n".join(f"`{c}`" for c in codes)
            sk = await _short_key(context, full_col_code)
            keyboard = [[InlineKeyboardButton("📖 分页发送", callback_data=f"s|{sk}"), InlineKeyboardButton("▶️ 自动发送", callback_data=f"a|{sk}")]]
            try:
                await _retry_send(msgs[0].reply_text, reply, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
            except Exception:
                await _retry_send(msgs[0].reply_text, reply, parse_mode="Markdown")

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
            code = await save_file(uid, file_type, file_id, file_size, file_unique_id, bname, code_prefix, bot_db_id=bot_db_id)
            if code:
                codes.append(code)
    return codes


async def _get_protect_async(context, chat_id: int) -> bool:
    """异步版本的转发保护判断（用于 mode==1 时查用户偏好）"""
    bot_record = context.bot_data.get('bot_record', {})
    forward_mode = bot_record.get('forward_mode', 0)
    bot_db_id = bot_record.get('id')

    if forward_mode == -1:
        return True
    elif forward_mode == 1 and bot_db_id:
        protect = await get_user_forward_protect(chat_id, bot_db_id)
        return bool(protect)
    return False


async def _add_to_collection(context, col_code, codes):
    """将代码列表添加到集合"""
    current_count = context.user_data.get('collection_count', 0)
    for i, code in enumerate(codes):
        if current_count + i + 1 > MAX_COLLECTION_FILES:
            break
        await add_file_to_collection(col_code, code, current_count + i + 1)
    new_count = min(current_count + len(codes), MAX_COLLECTION_FILES)
    context.user_data['collection_count'] = new_count


async def _handle_pack_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """处理打包模式下的文本消息：解析代码并批量添加到集合"""
    message = update.message
    bot_db_id = context.bot_data.get('bot_record', {}).get('id')
    pack_code = context.user_data['packing_collection']
    packing_count = context.user_data.get('packing_count', 0)
    packing_codes = context.user_data.get('packing_codes', set())
    max_pack_files = MAX_COLLECTION_FILES * 2

    # 按行分割，去除空行
    lines = [line.strip() for line in text.split('\n') if line.strip()]
    if not lines:
        await _retry_send(message.reply_text, "⚠️ 请发送文件代码（一行一个）。")
        return

    # 限制单次最多 500 个
    if len(lines) > 500:
        lines = lines[:500]
        await _retry_send(message.reply_text, "⚠️ 单次最多处理 500 个代码，已截取前 500 个。")

    # 步骤0: 格式验证 — 只保留符合代码格式的行（防注入 + 减少无效查询）
    import re
    code_pattern = re.compile(r'^[A-Za-z0-9_]+_(?:[pvd]|col):[A-Za-z0-9]+$')
    valid_lines = [l for l in lines if code_pattern.match(l) and len(l) <= 80]
    format_invalid = len(lines) - len(valid_lines)

    if not valid_lines:
        await _retry_send(message.reply_text,
            "⚠️ 没有有效的代码格式。\n\n"
            "正确格式示例：\n"
            f"`{context.bot.username}_p:xxx`\n"
            f"`{context.bot.username}_v:yyy`",
            parse_mode="Markdown"
        )
        return

    # 步骤1: 去除本条消息内的重复代码
    unique_codes = list(dict.fromkeys(valid_lines))  # 保持顺序去重

    # 步骤2: 与已添加的代码对比（内存去重）
    new_codes = [c for c in unique_codes if c not in packing_codes]

    if not new_codes:
        # 全部重复，无需查询数据库
        await _retry_send(message.reply_text,
            f"📋 全部重复，0 个新增。\n\n"
            f"📊 当前集合: {packing_count}/{max_pack_files}\n"
            f"继续发送代码，或 `/done` 完成",
        )
        return

    # 步骤3: 检查是否超过容量
    remaining = max_pack_files - packing_count
    if remaining <= 0:
        await _retry_send(message.reply_text,
            f"⚠️ 集合已满 {max_pack_files} 个文件！\n请发送 `/done` 完成打包。"
        )
        return

    if len(new_codes) > remaining:
        new_codes = new_codes[:remaining]
        await _retry_send(message.reply_text,
            f"⚠️ 容量不足，仅取前 {remaining} 个代码。"
        )

    # 步骤4: 批量验证并添加到数据库（3次查询优化）
    from db import batch_add_codes_to_collection
    result = await batch_add_codes_to_collection(
        col_code=pack_code,
        codes=new_codes,
        bot_db_id=bot_db_id,
        start_sort=packing_count
    )

    added = result['added']
    invalid = result['invalid']
    duplicate = result['duplicate']

    # 更新内存状态
    packing_count += added
    # 将所有提交的代码（不论有效无效）都加入已处理集合，避免重复提示
    for c in new_codes:
        packing_codes.add(c)

    context.user_data['packing_count'] = packing_count
    context.user_data['packing_codes'] = packing_codes

    # 构建回复
    fmt_info = f" | 格式错误 {format_invalid}" if format_invalid else ""
    reply = f"✅ 已添加 {added} 个文件（新增 {added} | 重复 {duplicate} | 无效 {invalid}{fmt_info}）\n\n"
    reply += f"📊 当前集合: {packing_count}/{max_pack_files}\n"
    reply += f"继续发送代码，或 `/done` 完成"

    await _retry_send(message.reply_text, reply)
