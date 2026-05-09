"""文件发送底层函数 - 被 send_queue 调用

send_batch(bot, chat_id, files, caption) → 发一批文件（≤GROUP_SEND_SIZE）
  - 按类型分组（图片/视频用相册，文档用文档组，音频用音频组）
  - 内置重试 + 指数退避 + 429 RetryAfter 处理
  - 不负责限速（由队列消费者控制节奏）

send_file_group(context, chat_id, files, caption) → 向后兼容包装
  - 内部使用 send_queue 提交任务
"""
import asyncio
import logging
import time
from typing import List, Dict

from telegram.ext import ContextTypes
from telegram import InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio
from telegram.error import TimedOut, NetworkError, RetryAfter, BadRequest

from config import (
    GROUP_SEND_SIZE, SEND_RETRY_COUNT, SEND_RETRY_DELAY,
    SEND_INDIVIDUAL_DELAY,
)
from database import mark_file_invalid

logger = logging.getLogger(__name__)


def _is_invalid_file_error(e):
    msg = str(e).lower()
    return 'media_file_invalid' in msg or 'wrong_file_id' in msg


async def _retry_send(send_func, *args, **kwargs):
    """
    通用重试包装器：带指数退避的重试机制
    - RetryAfter（429 Flood）等待指定时间后重试
    - TimedOut / NetworkError 自动重试（指数退避）
    - BadRequest（file_id无效）不重试
    - 最多重试 SEND_RETRY_COUNT 次
    """
    last_exception = None
    for attempt in range(SEND_RETRY_COUNT + 1):
        try:
            return await send_func(*args, **kwargs)
        except BadRequest as e:
            if _is_invalid_file_error(e):
                logger.warning("文件ID无效，跳过重试: %s", e)
            raise
        except RetryAfter as e:
            wait = e.retry_after if hasattr(e, 'retry_after') and e.retry_after else SEND_RETRY_DELAY * 4
            logger.warning("触发限流 (RetryAfter)，等待 %.1f 秒后重试 (第 %d/%d 次)",
                           wait, attempt + 1, SEND_RETRY_COUNT)
            await asyncio.sleep(wait)
            last_exception = e
        except (TimedOut, NetworkError) as e:
            if attempt < SEND_RETRY_COUNT:
                delay = SEND_RETRY_DELAY * (2 ** attempt)
                logger.warning("发送超时/网络错误: %s，等待 %.1f 秒后重试 (第 %d/%d 次)",
                               type(e).__name__, delay, attempt + 1, SEND_RETRY_COUNT)
                await asyncio.sleep(delay)
                last_exception = e
            else:
                logger.error("发送失败，已重试 %d 次: %s", SEND_RETRY_COUNT, e)
                raise
        except Exception:
            raise
    raise last_exception


# ===== 核心：发送一批文件（被队列消费者调用） =====

async def send_batch(bot, chat_id: int, files: List[Dict], caption: str = "") -> int:
    """发送一批文件（≤ GROUP_SEND_SIZE）
    
    按类型分组合并发送：
    - 图片+视频 → send_media_group（相册）
    - 文档 → send_media_group（文档组）
    - 音频 → send_media_group（音频组）
    
    返回成功发送的数量
    """
    if not files:
        return 0

    bot_name = getattr(bot, 'username', 'unknown')
    logger.info("send_batch: @%s 发送 %d 个文件到 chat_id=%s", bot_name, len(files), chat_id)

    # 按类型分组
    photo_video = [f for f in files if f['file_type'] in ('photo', 'video')]
    documents = [f for f in files if f['file_type'] in ('document', 'voice')]
    audios = [f for f in files if f['file_type'] == 'audio']

    sent_count = 0

    # 1. 图片+视频
    if photo_video:
        sent_count += await _send_typed_batch(bot, chat_id, photo_video, caption, 'photo_video', bot_name)

    # 2. 文档（组间小延迟）
    if documents:
        if photo_video:
            await asyncio.sleep(SEND_INDIVIDUAL_DELAY)
        sent_count += await _send_typed_batch(bot, chat_id, documents, caption, 'document', bot_name)

    # 3. 音频
    if audios:
        if photo_video or documents:
            await asyncio.sleep(SEND_INDIVIDUAL_DELAY)
        sent_count += await _send_typed_batch(bot, chat_id, audios, caption, 'audio', bot_name)

    return sent_count


async def _send_typed_batch(bot, chat_id, files, caption, type_key, bot_name) -> int:
    """发送同类型的一批文件"""
    if len(files) == 1:
        return await _send_single(bot, chat_id, files[0], caption, bot_name)
    return await _send_media_group(bot, chat_id, files, caption, type_key, bot_name)


async def _send_single(bot, chat_id, f, caption, bot_name) -> int:
    """发送单个文件"""
    try:
        ft = f['file_type']
        fid = f['telegram_file_id']
        cap = caption[:1024] if caption else ""
        timeout = dict(read_timeout=30, write_timeout=30)

        if ft == 'photo':
            await _retry_send(bot.send_photo, chat_id=chat_id, photo=fid, caption=cap, **timeout)
        elif ft == 'video':
            await _retry_send(bot.send_video, chat_id=chat_id, video=fid, caption=cap, **timeout)
        elif ft == 'audio':
            await _retry_send(bot.send_audio, chat_id=chat_id, audio=fid, caption=cap, **timeout)
        else:
            await _retry_send(bot.send_document, chat_id=chat_id, document=fid, caption=cap, **timeout)
        return 1
    except Exception as e:
        logger.error("发送单个文件失败: %s", e)
        if _is_invalid_file_error(e):
            await mark_file_invalid(f.get("code", ""))
        return 0


async def _send_media_group(bot, chat_id, files, caption, type_key, bot_name) -> int:
    """发送媒体组，失败时降级为逐个发送"""
    timeout = dict(read_timeout=30, write_timeout=30)
    media_list = []

    for idx, f in enumerate(files):
        fid = f['telegram_file_id']
        cap = caption if idx == 0 else ""
        cap = cap[:1024] if cap else ""

        try:
            if f['file_type'] == 'photo':
                media_list.append(InputMediaPhoto(media=fid, caption=cap))
            elif f['file_type'] == 'video':
                media_list.append(InputMediaVideo(media=fid, caption=cap))
            elif f['file_type'] == 'audio':
                media_list.append(InputMediaAudio(media=fid, caption=cap))
            else:
                media_list.append(InputMediaDocument(media=fid, caption=cap))
        except Exception as e:
            logger.error("构建媒体列表失败: %s", e)

    if not media_list:
        return 0

    # 尝试组发送
    try:
        await _retry_send(bot.send_media_group, chat_id=chat_id, media=media_list, **timeout)
        return len(media_list)
    except Exception as e:
        logger.warning("媒体组发送失败，降级逐个发送: %s", e)

    # 降级：逐个发送
    sent = 0
    for f in files:
        s = await _send_single(bot, chat_id, f, "", bot_name)
        sent += s
        if sent < len(files):
            await asyncio.sleep(SEND_INDIVIDUAL_DELAY)
    return sent


# ===== 向后兼容：send_file_group → 通过队列发送 =====

async def send_file_group(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    files: List[Dict],
    caption: str = "",
    update=None,
) -> int:
    """向后兼容：提交到发送队列并等待完成
    
    旧代码仍可调用此函数，内部会自动使用发送队列。
    新代码建议直接用 send_queue.submit_batch()
    """
    from send_queue import get_queue_from_context, split_files_to_batches

    queue = get_queue_from_context(context)
    batches = split_files_to_batches(files)

    if not batches:
        return 0

    total_sent = 0

    # 发送进度提示
    progress_msg = None
    if update and len(files) > 10:
        try:
            progress_msg = await update.message.reply_text(
                f"📤 已排队 {len(files)} 个文件（{len(batches)} 批）…"
            )
        except Exception:
            pass

    for i, batch in enumerate(batches):
        sent = await queue.submit_batch(chat_id, batch, caption)
        total_sent += sent

        # 更新进度
        if progress_msg and (i + 1) % 2 == 0:
            try:
                await progress_msg.edit_text(
                    f"📤 发送中… 批次 {i + 1}/{len(batches)} "
                    f"（已发送 {total_sent}/{len(files)}）"
                )
            except Exception:
                pass

    # 完成提示
    if progress_msg:
        try:
            await progress_msg.edit_text(f"✅ 发送完成！共 {total_sent}/{len(files)} 个文件")
        except Exception:
            pass

    return total_sent