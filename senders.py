"""文件发送逻辑模块 - 带重试机制和滑动限速"""
import asyncio
import logging
from typing import List, Dict

from telegram.ext import ContextTypes
from telegram import InputMediaPhoto, InputMediaVideo, InputMediaDocument, InputMediaAudio
from telegram.error import TimedOut, NetworkError, RetryAfter, BadRequest

from config import GROUP_SEND_SIZE, SEND_RETRY_COUNT, SEND_RETRY_DELAY, SEND_BATCH_DELAY, SEND_INDIVIDUAL_DELAY
from database import mark_file_invalid

logger = logging.getLogger(__name__)


def _is_invalid_file_error(e):
    msg = str(e).lower()
    return 'media_file_invalid' in msg or 'wrong_file_id' in msg


async def _retry_send(send_func, *args, **kwargs):
    """
    通用重试包装器：带指数退避的重试机制
    - 对 TimedOut 和 NetworkError 自动重试
    - 对 RetryAfter（429 Flood）等待指定时间后重试
    - BadRequest（file_id无效）不重试，直接抛出
    - 最多重试 SEND_RETRY_COUNT 次
    """
    last_exception = None
    for attempt in range(SEND_RETRY_COUNT + 1):
        try:
            return await send_func(*args, **kwargs)
        except BadRequest as e:
            # file_id 无效等错误，不重试直接抛出
            if _is_invalid_file_error(e):
                logger.warning("文件ID无效，跳过重试: %s", e)
            raise
        except RetryAfter as e:
            wait = e.retry_after if hasattr(e, 'retry_after') and e.retry_after else SEND_RETRY_DELAY * 4
            logger.warning("触发限流 (RetryAfter)，等待 %.1f 秒后重试 (第 %d/%d 次)", wait, attempt + 1, SEND_RETRY_COUNT)
            await asyncio.sleep(wait)
            last_exception = e
        except (TimedOut, NetworkError) as e:
            if attempt < SEND_RETRY_COUNT:
                delay = SEND_RETRY_DELAY * (2 ** attempt)  # 指数退避: 2s, 4s, 8s
                logger.warning("发送超时/网络错误: %s，等待 %.1f 秒后重试 (第 %d/%d 次)", type(e).__name__, delay, attempt + 1, SEND_RETRY_COUNT)
                await asyncio.sleep(delay)
                last_exception = e
            else:
                logger.error("发送失败，已重试 %d 次: %s", SEND_RETRY_COUNT, e)
                raise
        except Exception as e:
            # 非网络错误（如 media_file_invalid）不重试，直接抛出
            raise
    raise last_exception


async def send_file_group(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    files: List[Dict],
    caption: str = ""
) -> int:
    """
    组发送文件（图片+视频用相册，文档用文档组，音频用音频组）
    带滑动限速：每次发送之间添加延迟，避免触发 Telegram 限流
    返回成功发送的数量
    """
    if not files:
        logger.warning("send_file_group: files 为空")
        return 0

    bot_name = getattr(context.bot, 'username', 'unknown')
    logger.info("send_file_group: @%s 准备发送 %d 个文件到 chat_id=%s", bot_name, len(files), chat_id)

    # 按类型分组
    photo_video = []
    documents = []
    audios = []

    for f in files:
        ft = f['file_type']
        if ft in ('photo', 'video'):
            photo_video.append(f)
        elif ft == 'audio':
            audios.append(f)
        else:  # document, voice
            documents.append(f)

    sent_count = 0

    # 1. 发送图片+视频
    for i in range(0, len(photo_video), GROUP_SEND_SIZE):
        batch = photo_video[i:i + GROUP_SEND_SIZE]
        logger.info("发送图片+视频组: %d 个文件", len(batch))

        # 滑动限速：每组之间延迟（第一批不延迟）
        if i > 0:
            logger.debug("组间延迟 %.1f 秒", SEND_BATCH_DELAY)
            await asyncio.sleep(SEND_BATCH_DELAY)

        if len(batch) == 1:
            f = batch[0]
            try:
                fid = f['telegram_file_id']
                logger.info("发送单个媒体: type=%s, file_id=%s...(len=%d)", f['file_type'], str(fid)[:30], len(str(fid)))
                if f['file_type'] == 'photo':
                    await _retry_send(
                        context.bot.send_photo,
                        chat_id=chat_id, photo=fid,
                        caption=caption[:1024] if caption else "",
                        read_timeout=30, write_timeout=30
                    )
                else:
                    await _retry_send(
                        context.bot.send_video,
                        chat_id=chat_id, video=fid,
                        caption=caption[:1024] if caption else "",
                        read_timeout=30, write_timeout=30
                    )
                sent_count += 1
            except Exception as e:
                logger.error("发送单个媒体失败: %s", e)
                if _is_invalid_file_error(e):
                    mark_file_invalid(f.get("code", "")) if f.get("bot_username") == bot_name else None
        else:
            media_list = []
            for idx, f in enumerate(batch):
                file_id = f['telegram_file_id']
                cap = caption if idx == 0 else ""
                try:
                    if f['file_type'] == 'photo':
                        media_list.append(InputMediaPhoto(media=file_id, caption=cap[:1024] if cap else ""))
                    else:
                        media_list.append(InputMediaVideo(media=file_id, caption=cap[:1024] if cap else ""))
                except Exception as e:
                    logger.error("构建媒体列表失败: %s", e)
            if media_list:
                try:
                    await _retry_send(
                        context.bot.send_media_group,
                        chat_id=chat_id, media=media_list,
                        read_timeout=30, write_timeout=30
                    )
                    sent_count += len(media_list)
                except Exception as e:
                    logger.error("发送媒体组失败，降级逐个发送: %s", e)
                    # 降级：逐个发送，每个之间有延迟
                    for f in batch:
                        try:
                            if f['file_type'] == 'photo':
                                await _retry_send(
                                    context.bot.send_photo,
                                    chat_id=chat_id, photo=f['telegram_file_id'],
                                    read_timeout=30, write_timeout=30
                                )
                            else:
                                await _retry_send(
                                    context.bot.send_video,
                                    chat_id=chat_id, video=f['telegram_file_id'],
                                    read_timeout=30, write_timeout=30
                                )
                            sent_count += 1
                            # 滑动限速：单个文件之间延迟
                            await asyncio.sleep(SEND_INDIVIDUAL_DELAY)
                        except Exception as e2:
                            logger.error("降级发送失败: %s", e2)
                            if _is_invalid_file_error(e2):
                                mark_file_invalid(f.get("code", "")) if f.get("bot_username") == bot_name else None

    # 2. 发送文档
    for i in range(0, len(documents), GROUP_SEND_SIZE):
        batch = documents[i:i + GROUP_SEND_SIZE]

        # 滑动限速：和上一组类型之间延迟
        if i > 0 or photo_video:
            await asyncio.sleep(SEND_BATCH_DELAY)

        if len(batch) == 1:
            try:
                await _retry_send(
                    context.bot.send_document,
                    chat_id=chat_id, document=batch[0]['telegram_file_id'],
                    caption=caption[:1024] if caption else "",
                    read_timeout=30, write_timeout=30
                )
                sent_count += 1
            except Exception as e:
                logger.error("发送文档失败: %s", e)
                if _is_invalid_file_error(e):
                    mark_file_invalid(batch[0].get("code", "")) if batch[0].get("bot_username") == bot_name else None
        else:
            media_list = []
            for f in batch:
                try:
                    media_list.append(InputMediaDocument(media=f['telegram_file_id']))
                except Exception as e:
                    logger.error("构建文档列表失败: %s", e)
            if media_list:
                try:
                    await _retry_send(
                        context.bot.send_media_group,
                        chat_id=chat_id, media=media_list,
                        read_timeout=30, write_timeout=30
                    )
                    sent_count += len(media_list)
                except Exception as e:
                    logger.error("发送文档组失败，降级逐个发送: %s", e)
                    for f in batch:
                        try:
                            await _retry_send(
                                context.bot.send_document,
                                chat_id=chat_id, document=f['telegram_file_id'],
                                read_timeout=30, write_timeout=30
                            )
                            sent_count += 1
                            await asyncio.sleep(SEND_INDIVIDUAL_DELAY)
                        except Exception as e2:
                            logger.error("降级发送文档失败: %s", e2)
                            if _is_invalid_file_error(e2):
                                mark_file_invalid(f.get("code", "")) if f.get("bot_username") == bot_name else None

    # 3. 发送音频
    for i in range(0, len(audios), GROUP_SEND_SIZE):
        batch = audios[i:i + GROUP_SEND_SIZE]

        # 滑动限速：和上一组类型之间延迟
        if i > 0 or photo_video or documents:
            await asyncio.sleep(SEND_BATCH_DELAY)

        if len(batch) == 1:
            try:
                await _retry_send(
                    context.bot.send_audio,
                    chat_id=chat_id, audio=batch[0]['telegram_file_id'],
                    caption=caption[:1024] if caption else "",
                    read_timeout=30, write_timeout=30
                )
                sent_count += 1
            except Exception as e:
                logger.error("发送音频失败: %s", e)
                if _is_invalid_file_error(e):
                    mark_file_invalid(batch[0].get("code", "")) if batch[0].get("bot_username") == bot_name else None
        else:
            media_list = []
            for f in batch:
                try:
                    media_list.append(InputMediaAudio(media=f['telegram_file_id']))
                except Exception as e:
                    logger.error("构建音频列表失败: %s", e)
            if media_list:
                try:
                    await _retry_send(
                        context.bot.send_media_group,
                        chat_id=chat_id, media=media_list,
                        read_timeout=30, write_timeout=30
                    )
                    sent_count += len(media_list)
                except Exception as e:
                    logger.error("发送音频组失败，降级逐个发送: %s", e)
                    for f in batch:
                        try:
                            await _retry_send(
                                context.bot.send_audio,
                                chat_id=chat_id, audio=f['telegram_file_id'],
                                read_timeout=30, write_timeout=30
                            )
                            sent_count += 1
                            await asyncio.sleep(SEND_INDIVIDUAL_DELAY)
                        except Exception as e2:
                            logger.error("降级发送音频失败: %s", e2)
                            if _is_invalid_file_error(e2):
                                mark_file_invalid(f.get("code", "")) if f.get("bot_username") == bot_name else None

    return sent_count