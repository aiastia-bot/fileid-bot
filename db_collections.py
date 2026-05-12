"""集合操作相关数据库函数（Async SQLAlchemy）"""
import logging
from datetime import datetime
from typing import Optional, List, Dict

from sqlalchemy import select, update

from db_core import get_session, _model_to_dict
from models import Collection, CollectionItem, FileMapping

logger = logging.getLogger(__name__)


async def _get_redis():
    """延迟导入获取 Redis 实例"""
    from redis_manager import get_redis
    return await get_redis()


async def get_collection(code: str) -> Optional[Dict]:
    """获取集合信息，带缓存"""
    r = await _get_redis()
    cached = await r.cache_get_json(f"col:{code}")
    if cached:
        return cached

    async with get_session() as session:
        result = await session.execute(
            select(Collection).where(Collection.code == code)
        )
        col = result.scalar_one_or_none()
        data = _model_to_dict(col) if col else None
        if data:
            await r.cache_set_json(f"col:{code}", data, ttl=300)
        return data


async def get_collection_by_id(col_id: int) -> Optional[Dict]:
    """通过数据库ID获取集合信息"""
    async with get_session() as session:
        result = await session.execute(
            select(Collection).where(Collection.id == col_id)
        )
        col = result.scalar_one_or_none()
        return _model_to_dict(col) if col else None


async def get_collection_files(code: str) -> List[Dict]:
    """获取集合中的所有文件，带缓存"""
    r = await _get_redis()
    cached = await r.cache_get_json(f"col_files:{code}")
    if cached:
        return cached

    async with get_session() as session:
        result = await session.execute(
            select(FileMapping)
            .join(CollectionItem, FileMapping.code == CollectionItem.file_code)
            .where(CollectionItem.collection_code == code)
            .order_by(CollectionItem.sort_order)
        )
        files = result.scalars().all()
        data = [_model_to_dict(f) for f in files]
        await r.cache_set_json(f"col_files:{code}", data, ttl=300)
        return data


async def create_collection(code: str, bot_username: str, name: str, user_id: int,
                            bot_db_id: int = None) -> bool:
    """创建新集合"""
    async with get_session() as session:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            col = Collection(
                code=code, bot_username=bot_username, name=name,
                user_id=user_id, file_count=0, status='open',
                created_at=now, updated_at=now, bot_db_id=bot_db_id
            )
            session.add(col)
            await session.commit()
            return True
        except Exception as e:
            logger.error("创建集合失败: %s", e)
            return False


async def add_file_to_collection(col_code: str, file_code: str, sort_order: int) -> bool:
    """添加文件到集合"""
    async with get_session() as session:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            item = CollectionItem(
                collection_code=col_code, file_code=file_code, sort_order=sort_order
            )
            session.add(item)
            await session.execute(
                update(Collection)
                .where(Collection.code == col_code)
                .values(file_count=sort_order, updated_at=now)
            )
            await session.commit()
            # 清除集合文件缓存
            r = await _get_redis()
            await r.cache_delete(f"col_files:{col_code}")
            await r.cache_delete(f"col:{col_code}")
            return True
        except Exception as e:
            logger.error("添加文件到集合失败: %s", e)
            return False


async def complete_collection(col_code: str, file_count: int) -> bool:
    """完成集合"""
    async with get_session() as session:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await session.execute(
                update(Collection)
                .where(Collection.code == col_code)
                .values(status='completed', file_count=file_count, updated_at=now)
            )
            await session.commit()
            # 清除集合缓存
            r = await _get_redis()
            await r.cache_delete(f"col:{col_code}")
            await r.cache_delete(f"col_files:{col_code}")
            return True
        except Exception as e:
            logger.error("完成集合失败: %s", e)
            return False


async def delete_collection(col_code: str) -> bool:
    """删除集合及其文件项"""
    async with get_session() as session:
        try:
            # 删除集合项
            result = await session.execute(
                select(CollectionItem).where(CollectionItem.collection_code == col_code)
            )
            for item in result.scalars().all():
                await session.delete(item)
            # 删除集合
            result = await session.execute(
                select(Collection).where(Collection.code == col_code)
            )
            col = result.scalar_one_or_none()
            if col:
                await session.delete(col)
            await session.commit()
            # 清除集合缓存
            r = await _get_redis()
            await r.cache_delete(f"col:{col_code}")
            await r.cache_delete(f"col_files:{col_code}")
            return True
        except Exception as e:
            logger.error("删除集合失败: %s", e)
            return False


async def get_user_collections(user_id: int, limit: int = 20, bot_db_id: int = None) -> List[Dict]:
    """获取用户集合列表（按 bot_db_id 隔离）"""
    async with get_session() as session:
        q = select(
            Collection.code, Collection.name, Collection.file_count,
            Collection.status, Collection.created_at
        ).where(Collection.user_id == user_id)

        if bot_db_id is not None:
            q = q.where(Collection.bot_db_id == bot_db_id)

        q = q.order_by(Collection.created_at.desc()).limit(limit)
        result = await session.execute(q)
        rows = result.fetchall()
        return [
            {'code': r[0], 'name': r[1], 'file_count': r[2], 'status': r[3], 'created_at': r[4]}
            for r in rows
        ]