"""文件操作相关数据库函数（Async SQLAlchemy）"""
import string
import random
import logging
from datetime import datetime
from typing import Optional, List, Dict

from sqlalchemy import select, update, func, or_, text

from db.core import get_session, _model_to_dict
from db.models import FileMapping, Collection

logger = logging.getLogger(__name__)


async def get_active_bot_files(since_date: str = None) -> List[Dict]:
    """获取活跃Bot的文件列表，按bot_username+created_at排序，支持日期过滤"""
    async with get_session() as session:
        from db.models import UserBot
        query = (
            select(
                FileMapping.code, FileMapping.bot_username, FileMapping.file_type,
                FileMapping.file_size, FileMapping.user_id, FileMapping.created_at
            )
            .join(UserBot, FileMapping.bot_db_id == UserBot.id)
            .where(
                UserBot.status == 'active',
                or_(FileMapping.is_valid.is_(None), FileMapping.is_valid == 1)
            )
        )
        if since_date:
            query = query.where(FileMapping.created_at >= since_date)
        query = query.order_by(FileMapping.bot_username, FileMapping.created_at)
        result = await session.execute(query)
        rows = result.fetchall()
        return [
            {'code': r[0], 'bot_username': r[1], 'file_type': r[2],
             'file_size': r[3], 'user_id': r[4], 'created_at': r[5]}
            for r in rows
        ]


async def mark_file_invalid(code: str) -> bool:
    """标记文件为无效（file_id 失效）"""
    async with get_session() as session:
        result = await session.execute(
            update(FileMapping)
            .where(FileMapping.code == code)
            .values(is_valid=0)
        )
        await session.commit()
        return result.rowcount > 0


async def save_file(user_id: int, file_type: str, file_id: str,
                    file_size: int, file_unique_id: str, bot_username: str,
                    code_prefix: str, bot_db_id: int = None) -> Optional[str]:
    """保存文件到数据库，返回完整代码"""
    from config import CODE_LENGTH, FILE_TYPE_PREFIX

    async with get_session() as session:
        try:
            # 去重：如果同一 bot 下已存在相同 file_unique_id，直接返回已有代码
            if file_unique_id:
                if bot_db_id:
                    existing = await session.execute(
                        select(FileMapping.code).where(
                            FileMapping.file_unique_id == file_unique_id,
                            or_(FileMapping.bot_db_id == bot_db_id, FileMapping.bot_db_id.is_(None))
                        )
                    )
                else:
                    existing = await session.execute(
                        select(FileMapping.code).where(
                            FileMapping.file_unique_id == file_unique_id,
                            FileMapping.bot_username == bot_username
                        )
                    )
                existing_row = existing.scalar_one_or_none()
                if existing_row:
                    logger.info("文件已存在，复用代码: %s (file_unique_id=%s)", existing_row, file_unique_id)
                    return existing_row

            # 生成唯一代码
            chars = string.ascii_letters + string.digits
            while True:
                raw_code = ''.join(random.choices(chars, k=CODE_LENGTH))
                # 检查 code 是否已存在于 file_mappings 或 collections
                file_result = await session.execute(
                    select(FileMapping.id).where(FileMapping.code == raw_code).limit(1)
                )
                col_result = await session.execute(
                    select(Collection.id).where(Collection.code == raw_code).limit(1)
                )
                if not file_result.scalar_one_or_none() and not col_result.scalar_one_or_none():
                    break

            prefix = FILE_TYPE_PREFIX.get(file_type, 'd')
            full_code = f"{code_prefix}_{prefix}:{raw_code}"

            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            fm = FileMapping(
                code=full_code, bot_username=bot_username, file_type=file_type,
                telegram_file_id=file_id, file_size=file_size,
                file_unique_id=file_unique_id, user_id=user_id,
                created_at=now, bot_db_id=bot_db_id
            )
            session.add(fm)
            await session.commit()
            logger.info("新文件已写入DB: code=%s file_type=%s bot=%s user_id=%s file_unique_id=%s",
                        full_code, file_type, bot_username, user_id, file_unique_id)
            return full_code
        except Exception as e:
            if 'unique' in str(e).lower() or 'integrity' in str(e).lower():
                logger.error("代码重复（极少发生）")
                return None
            logger.error("保存文件失败: %s", e)
            return None


async def get_file(code: str) -> Optional[Dict]:
    """根据代码获取文件信息"""
    async with get_session() as session:
        result = await session.execute(
            select(FileMapping).where(FileMapping.code == code)
        )
        fm = result.scalar_one_or_none()
        return _model_to_dict(fm) if fm else None


async def get_files_by_codes(codes: list, bot_db_id: int = None) -> list:
    """批量根据代码获取文件信息（自动过滤已失效的文件）"""
    if not codes:
        return []
    async with get_session() as session:
        result = await session.execute(
            select(FileMapping).where(
                FileMapping.code.in_(codes),
                or_(FileMapping.is_valid.is_(None), FileMapping.is_valid == 1)
            )
        )
        rows = result.scalars().all()
        results = []
        for fm in rows:
            f = _model_to_dict(fm)
            # 过滤：如果指定了 bot_db_id，只返回属于该 Bot 的文件
            if bot_db_id and f.get("bot_db_id") and f["bot_db_id"] != bot_db_id:
                continue
            results.append(f)
        return results


async def get_all_files_for_export() -> List[Dict]:
    """导出所有文件记录"""
    async with get_session() as session:
        result = await session.execute(
            select(
                FileMapping.code, FileMapping.file_type, FileMapping.file_size,
                FileMapping.user_id, FileMapping.created_at
            ).order_by(FileMapping.created_at.desc())
        )
        rows = result.fetchall()
        return [
            {'code': r[0], 'file_type': r[1], 'file_size': r[2],
             'user_id': r[3], 'created_at': r[4]}
            for r in rows
        ]


async def get_files_by_bot_username(bot_username: str) -> List[Dict]:
    """根据 bot_username 导出该 Bot 的所有文件代码"""
    async with get_session() as session:
        result = await session.execute(
            select(
                FileMapping.code, FileMapping.file_type, FileMapping.file_size,
                FileMapping.user_id, FileMapping.created_at
            ).where(FileMapping.bot_username == bot_username)
            .order_by(FileMapping.created_at.desc())
        )
        rows = result.fetchall()
        return [
            {'code': r[0], 'file_type': r[1], 'file_size': r[2],
             'user_id': r[3], 'created_at': r[4]}
            for r in rows
        ]


async def get_files_by_bot_db_id(bot_db_id: int) -> List[Dict]:
    """根据 bot_db_id 导出该 Bot 的所有文件代码"""
    async with get_session() as session:
        result = await session.execute(
            select(
                FileMapping.code, FileMapping.file_type, FileMapping.file_size,
                FileMapping.user_id, FileMapping.created_at
            ).where(FileMapping.bot_db_id == bot_db_id)
            .order_by(FileMapping.created_at.desc())
        )
        rows = result.fetchall()
        return [
            {'code': r[0], 'file_type': r[1], 'file_size': r[2],
             'user_id': r[3], 'created_at': r[4]}
            for r in rows
        ]


async def get_recent_files_for_bot(
    bot_db_id: int,
    file_type: str = None,
    offset: int = 0,
    limit: int = 100
) -> List[Dict]:
    """获取指定 Bot 最近的文件记录（含 telegram_file_id，用于 /ex 发送）
    
    Args:
        bot_db_id: Bot 数据库 ID
        file_type: 文件类型过滤 'photo'/'video'/'document'/None=全部
                   'document' 包含 document+audio+voice+animation
        offset: 跳过前 N 条
        limit: 取 N 条
    
    Returns:
        List[Dict]: 包含 code, telegram_file_id, file_type, file_size, created_at
    """
    async with get_session() as session:
        query = (
            select(
                FileMapping.code, FileMapping.telegram_file_id, FileMapping.file_type,
                FileMapping.file_size, FileMapping.created_at
            )
            .where(
                FileMapping.bot_db_id == bot_db_id,
                or_(FileMapping.is_valid.is_(None), FileMapping.is_valid == 1)
            )
        )

        # 类型过滤
        if file_type == 'photo':
            query = query.where(FileMapping.file_type == 'photo')
        elif file_type == 'video':
            query = query.where(FileMapping.file_type == 'video')
        elif file_type == 'document':
            # document 包含 document, audio, voice, animation
            query = query.where(
                FileMapping.file_type.in_(['document', 'audio', 'voice', 'animation'])
            )

        query = query.order_by(FileMapping.created_at.desc()).offset(offset).limit(limit)
        result = await session.execute(query)
        rows = result.fetchall()
        return [
            {'code': r[0], 'telegram_file_id': r[1], 'file_type': r[2],
             'file_size': r[3], 'created_at': r[4]}
            for r in rows
        ]


