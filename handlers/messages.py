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
    get_collection_files_for_bot,
    create_collection, add_file_to_collection, get_user_forward_protect,
)
from db.files import is_bot_over_file_quota
from utils import get_code_prefix, escape_markdown, generate_raw_code, parse_file_code, parse_collection_code
from senders import send_file_group, _retry_send
from send_queue import get_queue_from_context, split_files_to_batches
from services.backup import kick_propagate

# ===== 附件回复缓冲参数（占位消息 + 进度编辑 + 静默合并） =====
ATTACH_REPLY_DEBOUNCE = 3.0    # 静默 N 秒后合并发送汇总（每条新附件重置）
ATTACH_REPLY_MAX_WAIT = 15.0   # 连续上传时的强制汇总上限，避免全程静默
ATTACH_SUMMARY_COLLAPSE = 15   # 汇总超过该文件数时用可折叠引用（blockquote）


# ===== 级联备份：来自其他 Bot 的消息 =====

def _is_bot_sender(update: Update) -> bool:
    user = getattr(update, 'effective_user', None)
    return bool(user and user.is_bot)


async def _resolve_backup_sender(update: Update, context) -> bool:
    """来信 Bot 是否为我配置的备份父节点（白名单），未知 Bot 一律拒绝"""
    from db import get_backup_parent_telegram_ids
    my_bot_db_id = (context.bot_data.get('bot_record') or {}).get('id')
    if not my_bot_db_id:
        return False
    parents = await get_backup_parent_telegram_ids(my_bot_db_id)
    return update.effective_user.id in parents


async def _save_backup_media(update: Update, context) -> None:
    """静默保存父节点推来的备份文件并继续级联（不回复、不限流）"""
    message = update.message
    file_id, file_type, file_size, file_unique_id = _extract_file_info(message)
    if not file_id:
        return
    bot_record = context.bot_data.get('bot_record') or {}
    bot_username = context.bot.username
    code = await save_file(0, file_type, file_id, file_size, file_unique_id, bot_username,
                           get_code_prefix(bot_username),
                           bot_db_id=bot_record.get('id'),
                           source_chat_id=str(message.chat_id),
                           source_message_id=message.message_id)
    if code:
        kick_propagate(context, [{'code': code, 'telegram_file_id': file_id,
                                  'file_type': file_type, 'file_size': file_size,
                                  'file_unique_id': file_unique_id}])
    logger.info("备份文件已接收: code=%s file_type=%s from=%s",
                code, file_type, update.effective_user.username or update.effective_user.id)


async def _is_over_file_quota(context) -> bool:
    """检查本次保存后 Bot 是否已超出主人的文件额度（VIP 主人不限，直接 False）"""
    bot_record = context.bot_data.get('bot_record', {})
    return await is_bot_over_file_quota(bot_record.get('id'), bot_record.get('owner_id'))


async def _file_quota_hint_text() -> str:
    """超出文件额度的升级提示文案（动态读取基础版限额）"""
    from db.vip import get_basic_bot_files_limit
    basic_limit = await get_basic_bot_files_limit()
    basic_line = f"{basic_limit:,} 个文件" if basic_limit > 0 else "不限量"
    return (
        "⚠️ 此 Bot 的文件数量已达当前会员的额度上限。\n"
        "📁 文件仍会保存，但不再返回代码。\n\n"
        "💡 请 Bot 主人升级会员：\n"
        f"   💎 基础版 — {basic_line}\n"
        "   ⭐ VIP — 不限量"
    )


def _ensure_cb_maps(context) -> tuple:
    """获取或初始化 cb_map 正/反向索引，返回 (cb_map, cb_map_reverse)

    cb_map:         sk -> col_code
    cb_map_reverse: col_code -> sk   （避免每次反向查找都 O(n) 扫描）
    """
    cb_map = context.bot_data.get('cb_map')
    if cb_map is None:
        cb_map = {}
        context.bot_data['cb_map'] = cb_map
    cb_map_reverse = context.bot_data.get('cb_map_reverse')
    if cb_map_reverse is None:
        cb_map_reverse = {}
        context.bot_data['cb_map_reverse'] = cb_map_reverse
    return cb_map, cb_map_reverse


async def _short_key(context, col_code: str) -> str:
    """生成短 key 用于 callback_data（Telegram 限制 64 字节）

    使用集合的数据库ID作为短key（c{id}），重启后仍可通过ID从数据库恢复。
    通过 cb_map_reverse 反向索引做 O(1) 复用查找。
    """
    cb_map, cb_map_reverse = _ensure_cb_maps(context)

    # O(1) 复用查找
    existing = cb_map_reverse.get(col_code)
    if existing is not None:
        return existing

    # 使用集合的数据库ID作为短key（重启不失效）
    col_info = await get_collection(col_code)
    if col_info and col_info.get('id'):
        key = f"c{col_info['id']}"
    else:
        # 降级：使用递增索引（仅当集合不在数据库中时）
        idx = len(cb_map)
        key = f"s{idx}"

    cb_map[key] = col_code
    cb_map_reverse[col_code] = key
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


# ===== 附件回复缓冲：占位消息 + 进度编辑 + 静默合并 =====
# 状态存 context.bot_data['_attachment_reply_buffer'][buffer_key]：
#   items / timer(静默去抖) / max_timer(强制汇总) / placeholder_id / placeholder_task
#   / edit_task / edit_dirty(有待发进度) / flushed / chat_id


async def _build_attachment_summary_text(items, total: int, ok_count: int) -> str:
    """附件汇总（HTML）；超过阈值用可折叠引用，长代码列表不刷屏"""
    lines = []
    for tn, c, mid, over in items:
        if over:
            lines.append(f"• {tn}: ⚠️ 已超出文件额度，未返回代码")
        else:
            lines.append(f"• {tn}: <code>{c}</code>")
    body = "\n".join(lines)
    if total > ATTACH_SUMMARY_COLLAPSE:
        body = f"<blockquote expandable>{body}</blockquote>"
    if ok_count == total:
        return f"✅ 已保存 {total} 个文件：\n\n{body}"
    hint = await _file_quota_hint_text()
    return f"✅ 已保存 {total} 个文件（{ok_count} 个返回代码）：\n\n{body}\n\n{hint}"


def _schedule_progress_edit(context, buf) -> None:
    """占位消息的进度编辑：至多一个编辑在途，内容永远取最新计数

    编辑由 API limiter 的独立 edit 预算节流（API_EDIT_INTERVAL），
    不占用同聊天新消息的发送通道。
    """
    task = buf.get('edit_task')
    if task is not None and not task.done():
        buf['edit_dirty'] = True
        return

    async def worker():
        while buf['edit_dirty']:
            buf['edit_dirty'] = False
            pt = buf.get('placeholder_task')
            if pt is not None and not pt.done():
                try:
                    await asyncio.wait_for(asyncio.shield(pt), 5)
                except Exception:
                    return  # 占位没发出去，无从编辑
            pid = buf.get('placeholder_id')
            if not pid:
                return
            try:
                await _retry_send(context.bot.edit_message_text,
                    f"📥 已收到 {len(buf['items'])} 个文件，正在保存…",
                    chat_id=buf['chat_id'], message_id=pid)
            except Exception:
                return  # 占位可能已被删除，放弃后续进度编辑

    buf['edit_task'] = asyncio.create_task(worker())


async def _send_placeholder(context, buf) -> None:
    """批量上传的占位进度消息（只发一次，后续靠编辑更新）"""
    first_mid = buf['items'][0][2]
    try:
        msg = await _retry_send(context.bot.send_message,
            chat_id=buf['chat_id'],
            text=f"📥 已收到 {len(buf['items'])} 个文件，正在保存…",
            reply_to_message_id=first_mid)
        if buf['flushed']:
            # flush 已先完成（占位被限速排队）：撤掉迟到的占位，避免倒叙刷屏
            try:
                await context.bot.delete_message(buf['chat_id'], msg.message_id)
            except Exception:
                pass
            return
        buf['placeholder_id'] = msg.message_id
        if len(buf['items']) > 2:
            buf['edit_dirty'] = True
            _schedule_progress_edit(context, buf)
    except Exception:
        buf['placeholder_id'] = None


async def _flush_attachment_buffer(context, buffer_key) -> None:
    """合并发送缓冲中的附件回复；已有占位消息则原位编辑成汇总"""
    bufs = context.bot_data.get('_attachment_reply_buffer', {})
    buf = bufs.pop(buffer_key, None)
    if not buf or not buf['items']:
        return
    buf['flushed'] = True
    for t in (buf.get('timer'), buf.get('max_timer')):
        if t:
            t.cancel()
    # 停掉在途进度编辑，避免覆盖最终汇总
    buf['edit_dirty'] = False
    edit_task = buf.get('edit_task')
    if edit_task is not None and not edit_task.done():
        edit_task.cancel()
    # 占位发送若还在限速排队，给它几秒机会（成功则原位编辑）
    pt = buf.get('placeholder_task')
    if pt is not None and not pt.done():
        try:
            await asyncio.wait_for(asyncio.shield(pt), 5)
        except Exception:
            pass

    items = buf['items']
    total = len(items)
    ok_count = sum(1 for it in items if not it[3])
    try:
        if buf.get('placeholder_id'):
            reply = await _build_attachment_summary_text(items, total, ok_count)
            await _retry_send(context.bot.edit_message_text, reply,
                chat_id=buf['chat_id'], message_id=buf['placeholder_id'])
        elif total == 1 and not items[0][3]:
            # 单个附件走原直接回复路径（最快，保持不变）
            tn, c, mid, _ = items[0]
            await _retry_send(context.bot.send_message,
                chat_id=buf['chat_id'],
                text=f"✅ {tn}已保存！\n\n代码: `{c}`",
                parse_mode='Markdown',
                reply_to_message_id=mid)
        else:
            reply = await _build_attachment_summary_text(items, total, ok_count)
            await _retry_send(context.bot.send_message, reply,
                chat_id=buf['chat_id'], parse_mode='HTML',
                reply_to_message_id=items[0][2])
        logger.info("附件回复缓冲: 发送 %d 个附件汇总到 chat_id=%s (占位编辑=%s)",
                    total, buf['chat_id'], bool(buf.get('placeholder_id')))
    except Exception as e:
        logger.error("附件汇总发送失败: %s", e, exc_info=True)


async def _buffer_attachment_reply(context, chat_id: int, user_id: int, item: tuple) -> None:
    """附件回复入缓冲并调度反馈

    - 首个附件：只建缓冲不发消息（保住单附件「直接回复」的最快体验）
    - 第 2 个附件：判定批量上传，发一次占位消息提供过程反馈
    - 后续附件：编辑占位报最新计数（至多一个编辑在途）
    - 静默 3 秒或累计 15 秒（先到者）：原位编辑成最终汇总
    """
    if '_attachment_reply_buffer' not in context.bot_data:
        context.bot_data['_attachment_reply_buffer'] = {}
    buffer_key = f"{chat_id}_{user_id}"
    buf = context.bot_data['_attachment_reply_buffer'].get(buffer_key)
    if buf is None:
        buf = {
            'items': [], 'timer': None, 'max_timer': None,
            'chat_id': chat_id, 'placeholder_id': None,
            'placeholder_task': None, 'edit_task': None,
            'edit_dirty': False, 'flushed': False,
        }
        context.bot_data['_attachment_reply_buffer'][buffer_key] = buf

        async def flush_on_max_wait():
            await asyncio.sleep(ATTACH_REPLY_MAX_WAIT)
            await _flush_attachment_buffer(context, buffer_key)
        buf['max_timer'] = asyncio.create_task(flush_on_max_wait())

    buf['items'].append(item)
    if len(buf['items']) == 2 and buf['placeholder_task'] is None:
        buf['placeholder_task'] = asyncio.create_task(_send_placeholder(context, buf))
    elif len(buf['items']) > 2:
        buf['edit_dirty'] = True
        _schedule_progress_edit(context, buf)

    # 静默计时器：连续上传时不断顺延，由 max_timer 兜底
    if buf['timer']:
        buf['timer'].cancel()

    async def flush_on_silence():
        await asyncio.sleep(ATTACH_REPLY_DEBOUNCE)
        await _flush_attachment_buffer(context, buffer_key)
    buf['timer'] = asyncio.create_task(flush_on_silence())


async def flush_all_attachment_reply_buffers(application) -> None:
    """进程退出前把未发出的附件汇总补发出去（文件已入库，只差通知用户）"""
    try:
        bufs = application.bot_data.get('_attachment_reply_buffer')
    except Exception:
        return
    if not bufs:
        return
    for buffer_key in list(bufs.keys()):
        try:
            await _flush_attachment_buffer(application, buffer_key)
        except Exception:
            logger.exception("shutdown 附件汇总补发失败: %s", buffer_key)


async def handle_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理用户发送的图片/视频/音频/文档

    连续上传时走两段式反馈：第 2 个附件到达后发一条占位消息，
    后续编辑占位报进度，静默后原位编辑成汇总——批量全程只发 1 条新消息。
    """
    t0 = _time.monotonic()
    message = update.message
    user_id = update.effective_user.id
    bot_username = context.bot.username

    if _is_bot_sender(update):
        return  # Bot 来信由 handle_group_media 的备份分支处理，不当作用户上传

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
        code = await save_file(user_id, file_type, file_id, file_size, file_unique_id, bot_username, code_prefix,
                               bot_db_id=bot_db_id,
                               source_chat_id=str(message.chat_id), source_message_id=message.message_id)
        logger.debug("⏱ handle_attachment save_file user=%s 耗时%.3fs", user_id, _time.monotonic() - t1)
        if not code:
            await _retry_send(message.reply_text, "❌ 保存失败，请重试。")
            return

        logger.info("文件已保存: code=%s file_type=%s user_id=%s chat_id=%s message_id=%s file_unique_id=%s",
                    code, file_type, user_id, message.chat_id, message.message_id, file_unique_id)

        # 级联备份：推给下游节点（fire-and-forget，不阻塞回复）
        kick_propagate(context, [{'code': code, 'telegram_file_id': file_id,
                                  'file_type': file_type, 'file_size': file_size,
                                  'file_unique_id': file_unique_id}])

        type_name = FILE_TYPE_MAP.get(file_type, file_type)

        # 文件额度检查：超限后文件已入库但不返回代码（VIP 主人不限）
        over_quota = await _is_over_file_quota(context)

        # 如果正在创建集合，追加文件（立即回复，不缓冲）
        if creating_col:
            if over_quota:
                await _retry_send(message.reply_text,
                    text=await _file_quota_hint_text(),
                    reply_to_message_id=message.message_id)
                return
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

        # ===== 附件回复缓冲：占位消息 + 进度编辑，静默后原位编辑成汇总 =====
        await _buffer_attachment_reply(context, message.chat_id, user_id,
                                       (type_name, code, message.message_id, over_quota))
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
    if _is_bot_sender(update):
        return  # 不解析其他 Bot 发来的文本，避免代码往复请求

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

    # requested_code：跨 Bot 同码取回时用户实际输入的代码
    found_codes = {f.get('requested_code') or f['code'] for f in found_files}
    not_found = [c for c in file_codes if c not in found_codes]

    if found_files:
        total = len(found_files)
        queue = get_queue_from_context(context)
        batches = split_files_to_batches(found_files)

        # 检查转发保护（直接从内存 bot_record 读取，通常 0 次 DB 查询）
        protect = await _get_protect_async(context, chat_id)
        auto_delete = _get_auto_delete(context)

        # 异步提交所有批次到队列（不等待发送完成，避免阻塞 webhook）
        t2 = _time.monotonic()
        accepted_files = 0
        queue.clear_chat_cancel(chat_id)
        for batch in batches:
            try:
                task = queue.submit_batch_async(chat_id, batch, protect_content=protect,
                                                auto_delete=auto_delete)
                await task.accepted
                accepted_files += len(batch)
            except Exception as e:
                logger.error("队列提交失败: %s", e)
                await _retry_send(message.reply_text, f"⚠️ 已排队 {accepted_files}/{total} 个文件。{e}")
                break
        logger.debug("⏱ _process_file_codes queue提交(%d 批) 耗时%.3fs", len(batches), _time.monotonic() - t2)

        # 发送排队提示后立即返回，webhook 快速响应
        t3 = _time.monotonic()
        if total > GROUP_SEND_SIZE:
            try:
                await _retry_send(message.reply_text,
                    f"📤 已排队 {accepted_files}/{total} 个文件，正在后台发送。使用 /queue 查看进度，/stop 取消。"
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
        if not col_info:
            await _retry_send(message.reply_text, f"❌ 集合不存在: `{col_code}`", parse_mode="Markdown")
            continue
        # 跨 Bot 同码取回：别的 Bot 创建的集合也可在本 Bot 使用，
        # 文件按本 Bot 的备份行（自己的 file_id）解析

        safe_name = escape_markdown(col_info['name'])
        if col_info['status'] != 'completed':
            await _retry_send(message.reply_text, f"⚠️ 集合「{safe_name}」尚未完成。")
            continue

        t2 = _time.monotonic()
        files, missing_count = await get_collection_files_for_bot(col_code, current_bot_db_id_col)
        logger.debug("⏱ _process_collection_codes get_collection_files_for_bot(%s) 耗时%.3fs files=%d missing=%d",
                     col_code, _time.monotonic() - t2, len(files), missing_count)

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
        if missing_count:
            col_text += f"\n⚠️ 有 {missing_count} 个文件尚未同步到本 Bot，将跳过"
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
    if _is_bot_sender(update):
        return  # 忽略其他 Bot 的转发
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

    # ===== 备份分支：来自配置父节点的 Bot 推送，静默保存并级联 =====
    if _is_bot_sender(update):
        if not await _resolve_backup_sender(update, context):
            logger.info("忽略未知 Bot 的媒体消息: from=%s", update.effective_user.id)
            return
        if not message.media_group_id:
            await _save_backup_media(update, context)
            return
        # 媒体组走下方缓冲流程，但标记 silent 不回复

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
    if _is_bot_sender(update):
        group_data['silent'] = True   # 备份推送：保存但不回复
    if group_data['timer']:
        group_data['timer'].cancel()

    async def process():
        try:
            await asyncio.sleep(2)
            msgs = group_data['messages']
            if not msgs:
                return
            codes, over_count = await _save_media_messages(msgs, context)
            if group_data.get('silent'):
                return  # 备份推送静默，_save_media_messages 内部已级联
            if codes or over_count:
                creating_col = context.user_data.get('creating_collection')
                if creating_col and codes:
                    await _add_to_collection(context, creating_col, codes)
                    count = context.user_data.get('collection_count', 0)
                    reply = f"✅ 媒体组已保存并添加到集合！\n\n共 {len(codes)} 个文件 ({count}/{MAX_COLLECTION_FILES})\n\n"
                    reply += "\n".join(f"`{c}`" for c in codes)
                else:
                    total = len(codes) + over_count
                    reply = f"✅ 媒体组已保存！共 {total} 个文件"
                    if over_count:
                        reply += f"（{len(codes)} 个返回代码）"
                    reply += "：\n\n"
                    reply += "\n".join(f"`{c}`" for c in codes) if codes else "（全部超出文件额度）"
                if over_count:
                    reply += "\n\n" + await _file_quota_hint_text()
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
    if _is_bot_sender(update):
        return  # 忽略其他 Bot 的转发媒体

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

            codes, over_count = await _save_media_messages(msgs, context)
            if not codes:
                if over_count:
                    await _retry_send(msgs[0].reply_text, await _file_quota_hint_text())
                else:
                    await _retry_send(msgs[0].reply_text, "❌ 转发的媒体组处理失败。")
                return
            if over_count:
                codes_hint = f"\n\n⚠️ 另有 {over_count} 个文件超出额度未保存进集合。"
            else:
                codes_hint = ""

            # 自动创建集合
            uid = msgs[0].from_user.id
            bname = context.bot.username
            code_prefix = get_code_prefix(bname)
            col_name = f"转发组_{datetime.now().strftime('%m%d%H%M')}"
            full_col_code = f"{code_prefix}_col:{generate_raw_code()}"

            # 保存集合到数据库（使用 async ORM）
            from db import create_collection, complete_collection, batch_add_codes_to_collection
            bot_db_id = context.bot_data.get('bot_record', {}).get('id')

            save_ok = False
            try:
                if await create_collection(full_col_code, bname, col_name, uid, bot_db_id=bot_db_id):
                    # 批量添加（1-2 次查询），替代原来逐文件 add_file_to_collection
                    # 的 N 次独立 session+commit（媒体组大时显著提速）
                    add_result = await batch_add_codes_to_collection(
                        full_col_code, codes, bot_db_id=bot_db_id, start_sort=0)
                    await complete_collection(full_col_code, add_result['added'])
                    save_ok = add_result['added'] > 0
            except Exception as e:
                logger.error("自动创建转发集合失败: %s", e)
            if not save_ok:
                reply = f"✅ 转发媒体已保存（共 {len(codes)} 个）：\n\n" + "\n".join(f"`{c}`" for c in codes)
                if over_count:
                    reply += codes_hint + "\n\n" + await _file_quota_hint_text()
                await _retry_send(msgs[0].reply_text, reply, parse_mode="Markdown")
                return

            # 回复
            safe_name = escape_markdown(col_name)
            reply = f"✅ 转发媒体组已保存并自动创建集合！\n\n📦 集合: *{safe_name}*\n📊 共 {len(codes)} 个文件\n📦 集合代码: `{full_col_code}`{codes_hint}\n\n单个文件代码：\n"
            reply += "\n".join(f"`{c}`" for c in codes)
            if over_count:
                reply += "\n\n" + await _file_quota_hint_text()
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


async def _save_media_messages(messages, context) -> tuple:
    """批量保存媒体消息，返回 (代码列表, 超出文件额度未返回代码的数量)
    超额度的文件已入库但不进代码列表（也不进自动集合，避免绕过额度）"""
    uid = messages[0].from_user.id
    bname = context.bot.username
    code_prefix = get_code_prefix(bname)
    bot_record = context.bot_data.get('bot_record', {})
    bot_db_id = bot_record.get('id')
    owner_id = bot_record.get('owner_id')
    codes = []
    over_count = 0
    saved_files = []

    for msg in messages:
        file_id, file_type, file_size, file_unique_id = _extract_file_info(msg)
        if file_id and file_type:
            code = await save_file(uid, file_type, file_id, file_size, file_unique_id, bname, code_prefix,
                                   bot_db_id=bot_db_id,
                                   source_chat_id=str(msg.chat_id), source_message_id=msg.message_id)
            if code:
                saved_files.append({'code': code, 'telegram_file_id': file_id,
                                    'file_type': file_type, 'file_size': file_size,
                                    'file_unique_id': file_unique_id})
                over = await is_bot_over_file_quota(bot_db_id, owner_id)
                if over:
                    over_count += 1
                else:
                    codes.append(code)
                logger.info("媒体组文件已保存: code=%s file_type=%s user_id=%s chat_id=%s message_id=%s file_unique_id=%s over_quota=%s",
                            code, file_type, uid, msg.chat_id, msg.message_id, file_unique_id, over)
    # 级联备份：推给下游节点（fire-and-forget，不阻塞回复）
    kick_propagate(context, saved_files)
    return codes, over_count


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


def _get_auto_delete(context) -> int:
    """从 bot_record 获取自动删除延迟秒数"""
    return context.bot_data.get('bot_record', {}).get('auto_delete', 0) or 0


async def _add_to_collection(context, col_code, codes):
    """将代码列表添加到集合（批量插入，避免逐条 session+commit）"""
    current_count = context.user_data.get('collection_count', 0)
    remaining = MAX_COLLECTION_FILES - current_count
    if remaining <= 0 or not codes:
        return
    take = codes[:remaining]

    from db import batch_add_codes_to_collection
    bot_db_id = context.bot_data.get('bot_record', {}).get('id')
    result = await batch_add_codes_to_collection(
        col_code, take, bot_db_id=bot_db_id, start_sort=current_count)
    context.user_data['collection_count'] = current_count + result['added']


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
