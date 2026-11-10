"""文件发送底层函数 - 被 send_queue 调用

send_batch(bot, chat_id, files, caption) → 发一批文件（≤GROUP_SEND_SIZE）
  - 按类型分组（图片/视频用相册，文档用文档组，音频用音频组）
  - 内置重试 + 指数退避 + 429 RetryAfter 处理
  - 不负责限速（由队列消费者控制节奏）

send_file_group(context, chat_id, files, caption) → 向后兼容包装
  - 内部使用 send_queue 提交任务

bot_api_call(send_func, *args, **kwargs) → 全局限速的 API 调用
  - 所有 Telegram API 调用都应通过此函数，确保 per-bot 限速
"""
import asyncio
import logging
import time
from typing import List, Dict

from telegram.ext import ContextTypes
from telegram import InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio
from telegram.error import TimedOut, NetworkError, RetryAfter, BadRequest, Forbidden

from config import (
    GROUP_SEND_SIZE, SEND_RETRY_COUNT, SEND_RETRY_DELAY,
    SEND_INDIVIDUAL_DELAY, RETRY_AFTER_MAX_WAIT,
    BOT_MIN_API_INTERVAL,
)
from db import mark_file_invalid

logger = logging.getLogger(__name__)


def _bound_bot(send_func):
    bound = getattr(send_func, '__self__', None)
    if hasattr(bound, 'token'):
        return bound
    get_bot = getattr(bound, 'get_bot', None)
    return get_bot() if get_bot else None


async def bot_api_call(send_func, *args, **kwargs):
    """Compatibility wrapper; ExtBot routes direct and wrapped calls identically."""
    return await _retry_send(send_func, *args, **kwargs)


def _is_invalid_file_error(e):
    msg = str(e).lower()
    return 'media_file_invalid' in msg or 'wrong_file_id' in msg


def _is_blocked_error(e):
    """检查是否是用户拉黑了 Bot"""
    msg = str(e).lower()
    return 'blocked by the user' in msg or 'user_is_blocked' in msg


class SendBlockedError(Exception):
    """用户已拉黑 Bot，发送应立即中止"""
    pass


async def _retry_send(send_func, *args, **kwargs):
    """Network retries only. The ExtBot hook exclusively owns 429 retries."""
    from services.rate_limit import get_limiter
    bot = _bound_bot(send_func)
    managed = getattr(bot, 'rate_limiter', None) is not None
    for attempt in range(SEND_RETRY_COUNT+1):
        try:
            if bot is not None and not managed:
                # Legacy plain Bot helpers also participate in the same bot budget.
                limiter = get_limiter(bot.token)
                return await limiter.process_request(send_func, args, kwargs,
                    getattr(send_func, '__name__', '').replace('_', ''), kwargs, None)
            return await send_func(*args, **kwargs)
        except (BadRequest, RetryAfter):
            raise
        except (TimedOut, NetworkError):
            if attempt == SEND_RETRY_COUNT:
                raise
            await asyncio.sleep(SEND_RETRY_DELAY*2**attempt)


# ===== 核心：发送一批文件（被队列消费者调用） =====

async def _schedule_auto_delete(bot, chat_id: int, message_ids: list, delay: int):
    """Batch deletion uses the same API hook and avoids one request per message."""
    try:
        await asyncio.sleep(delay)
        for offset in range(0, len(message_ids), 100):
            await _retry_send(bot.delete_messages, chat_id=chat_id,
                              message_ids=message_ids[offset:offset+100])
    except asyncio.CancelledError:
        pass
    except Exception as error:
        logger.warning('Auto-delete incomplete for chat %s: %s', chat_id, type(error).__name__)


def _collect_msg_ids(result) -> list:
    """从发送结果中提取 message_id 列表"""
    if result is None:
        return []
    if isinstance(result, list):
        return [m.message_id for m in result if hasattr(m, 'message_id')]
    if hasattr(result, 'message_id'):
        return [result.message_id]
    return []


async def send_batch(bot, chat_id: int, files: List[Dict], caption: str = "",
                     protect_content: bool = False, auto_delete: int = 0) -> int:
    """发送一批文件（≤ GROUP_SEND_SIZE）
    
    按类型分组合并发送：
    - 图片+视频 → send_media_group（相册）
    - 文档 → send_media_group（文档组）
    - 音频 → send_media_group（音频组）
    
    Args:
        protect_content: 如果为 True，发送的图片/视频将禁止转发和保存
        auto_delete: >0 时发送后延迟 N 秒自动删除消息
    
    返回成功发送的数量
    """
    if not files:
        return 0

    bot_name = getattr(bot, 'username', 'unknown')
    logger.info("send_batch: @%s 发送 %d 个文件到 chat_id=%s protect=%s auto_del=%s",
                bot_name, len(files), chat_id, protect_content, auto_delete)

    _start = time.time()

    # 按类型分组（单次遍历，避免对 files 多次列表推导）
    photo_video = []
    documents = []
    audios = []
    for f in files:
        ft = f['file_type']
        if ft in ('photo', 'video'):
            photo_video.append(f)
        elif ft in ('document', 'voice'):
            documents.append(f)
        elif ft == 'audio':
            audios.append(f)

    sent_count = 0
    all_msg_ids = []  # 收集所有消息ID（用于自动删除）

    # 1. 图片+视频（仅图片/视频受 protect_content 保护）
    if photo_video:
        count, msg_ids = await _send_typed_batch(
            bot, chat_id, photo_video, caption, 'photo_video', bot_name,
            protect_content=protect_content)
        sent_count += count
        all_msg_ids.extend(msg_ids)

    # 2. 文档（组间小延迟）
    if documents:
        if photo_video:
            await asyncio.sleep(SEND_INDIVIDUAL_DELAY)
        count, msg_ids = await _send_typed_batch(
            bot, chat_id, documents, caption, 'document', bot_name,
            protect_content=False)
        sent_count += count
        all_msg_ids.extend(msg_ids)

    # 3. 音频
    if audios:
        if photo_video or documents:
            await asyncio.sleep(SEND_INDIVIDUAL_DELAY)
        count, msg_ids = await _send_typed_batch(
            bot, chat_id, audios, caption, 'audio', bot_name,
            protect_content=False)
        sent_count += count
        all_msg_ids.extend(msg_ids)

    # 自动删除
    if auto_delete > 0 and all_msg_ids:
        asyncio.create_task(_schedule_auto_delete(bot, chat_id, all_msg_ids, auto_delete))

    # 记录发送计数
    if sent_count > 0:
        try:
            from redis_manager import get_redis
            r = await get_redis()
            today = time.strftime('%Y-%m-%d')
            await r.counter_incr(f"stats:sent:{today}:{bot_name}", ttl=86400 * 2)
            await r.counter_incr(f"stats:sent:{today}:total", ttl=86400 * 2)
            logger.debug("发送计数: @%s +%d (%.1fs)", bot_name, sent_count, time.time() - _start)
        except Exception:
            pass

    return sent_count


async def _send_typed_batch(bot, chat_id, files, caption, type_key, bot_name,
                            protect_content: bool = False) -> tuple:
    """发送同类型的一批文件，返回 (sent_count, [msg_ids])"""
    if len(files) == 1:
        return await _send_single(bot, chat_id, files[0], caption, bot_name,
                                  protect_content=protect_content)
    return await _send_media_group(bot, chat_id, files, caption, type_key, bot_name,
                                   protect_content=protect_content)


async def _send_single(bot, chat_id, f, caption, bot_name,
                       protect_content: bool = False) -> tuple:
    """发送单个文件，返回 (1或0, [msg_ids])"""
    try:
        ft = f['file_type']
        fid = f['telegram_file_id']
        cap = caption[:1024] if caption else ""
        timeout = dict(read_timeout=30, write_timeout=30)
        # 仅图片/视频支持 protect_content
        protect = protect_content and ft in ('photo', 'video')

        msg = None
        if ft == 'photo':
            msg = await _retry_send(bot.send_photo, chat_id=chat_id, photo=fid, caption=cap,
                              protect_content=protect, **timeout)
        elif ft == 'video':
            msg = await _retry_send(bot.send_video, chat_id=chat_id, video=fid, caption=cap,
                              protect_content=protect, **timeout)
        elif ft == 'audio':
            msg = await _retry_send(bot.send_audio, chat_id=chat_id, audio=fid, caption=cap, **timeout)
        else:
            msg = await _retry_send(bot.send_document, chat_id=chat_id, document=fid, caption=cap, **timeout)
        return (1, _collect_msg_ids(msg))
    except asyncio.CancelledError:
        raise
    except RetryAfter:
        # 限流直接上抛，让队列等待重试
        raise
    except Exception as e:
        # 用户已拉黑 Bot，立即中止整个发送任务
        if _is_blocked_error(e):
            logger.warning("用户 chat_id=%s 已拉黑 Bot @%s，立即中止发送", chat_id, bot_name)
            raise SendBlockedError(f"chat_id={chat_id} 已拉黑 Bot")
        logger.error("发送单个文件失败: %s", e)
        if _is_invalid_file_error(e):
            await mark_file_invalid(f.get("code", ""))
        return (0, [])


async def _send_media_group(bot, chat_id, files, caption, type_key, bot_name,
                            protect_content: bool = False) -> tuple:
    """发送媒体组，失败时降级为逐个发送，返回 (sent_count, [msg_ids])"""
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
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("构建媒体列表失败: %s", e)

    if not media_list:
        return (0, [])

    # 尝试组发送
    try:
        result = await _retry_send(bot.send_media_group, chat_id=chat_id, media=media_list,
                          protect_content=protect_content, **timeout)
        return (len(media_list), _collect_msg_ids(result))
    except asyncio.CancelledError:
        raise
    except RetryAfter as e:
        # 限流不降级逐个发送，直接上抛让队列等待重试
        raise
    except SendBlockedError:
        raise
    except Exception as e:
        # 非限流错误：降级为逐个发送
        logger.warning("媒体组发送失败，降级逐个发送: %s", e)

    # 降级：逐个发送
    sent = 0
    msg_ids = []
    for f in files:
        count, ids = await _send_single(bot, chat_id, f, "", bot_name,
                               protect_content=protect_content)
        sent += count
        msg_ids.extend(ids)
        if sent < len(files):
            await asyncio.sleep(SEND_INDIVIDUAL_DELAY)
    return (sent, msg_ids)


# ===== 向后兼容：send_file_group → 通过队列发送 =====

async def send_file_group(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    files: List[Dict],
    caption: str = "",
    update=None,
    protect_content: bool = False,
    auto_delete: int = 0,
) -> int:
    """向后兼容：提交到发送队列并等待完成

    旧代码仍可调用此函数，内部会自动使用发送队列。
    新代码建议直接用 send_queue.submit_batch()
    """
    from send_queue import get_queue_from_context, split_files_to_batches

    # 从 bot_record 自动获取 auto_delete（如果未显式传入）
    if not auto_delete:
        auto_delete = context.bot_data.get('bot_record', {}).get('auto_delete', 0) or 0

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
        sent = await queue.submit_batch(chat_id, batch, caption,
                                        protect_content=protect_content,
                                        auto_delete=auto_delete)
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