"""数据库核心操作 - Async SQLAlchemy Engine + Session 管理"""
import os
import logging
from datetime import datetime
from typing import Optional, List, Dict, AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    create_async_engine, AsyncSession, AsyncEngine, async_sessionmaker
)

from config import DB_TYPE, DATABASE_URL, DB_PATH
from db.models import Base

logger = logging.getLogger(__name__)

# 全局引擎和会话工厂
_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


def _build_database_url() -> str:
    """根据配置构建数据库连接 URL"""
    if DB_TYPE == 'mysql':
        if DATABASE_URL:
            # 确保 aiomysql 驱动前缀
            url = DATABASE_URL
            if url.startswith('mysql://'):
                url = url.replace('mysql://', 'mysql+aiomysql://', 1)
            elif url.startswith('mysql+asyncmy://'):
                url = url.replace('mysql+asyncmy://', 'mysql+aiomysql://', 1)
            elif not url.startswith('mysql+aiomysql://'):
                url = f'mysql+aiomysql://{url}'
            return url
        raise ValueError("MySQL 模式需要配置 DATABASE_URL 环境变量")
    else:
        # SQLite 模式 - 确保 data 目录存在
        db_dir = os.path.dirname(DB_PATH)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        return f'sqlite+aiosqlite:///{DB_PATH}'


def _create_engine() -> AsyncEngine:
    """创建异步引擎"""
    url = _build_database_url()
    engine_kwargs = {
        'echo': False,
    }
    if DB_TYPE == 'sqlite':
        # SQLite 特定配置
        engine_kwargs['connect_args'] = {'timeout': 30}
        # WAL/synchronous 模式通过 on_connect 事件设置
        # 注意：pre_ping 对 SQLite 无意义（本地文件无连接断开概念），
        # 且每次取连接多一次 SELECT 1 往返，这里显式关闭
        engine_kwargs['pool_pre_ping'] = False
    else:
        engine_kwargs['pool_pre_ping'] = True
        engine_kwargs.update({
            'pool_size': 10,
            'max_overflow': 20,
            'pool_recycle': 3600,
        })
    engine = create_async_engine(url, **engine_kwargs)

    # SQLite: 在连接时设置 WAL 模式
    if DB_TYPE == 'sqlite':
        from sqlalchemy import event

        @event.listens_for(engine.sync_engine, "connect")
        def _set_sqlite_pragma(dbapi_conn, connection_record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            # WAL 配套优化：NORMAL 模式下 commit 不强制 fsync，
            # 本项目几乎每个写操作都单独 commit，FULL 模式写放大严重
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()

    return engine


def get_engine() -> AsyncEngine:
    """获取全局异步引擎（懒初始化）"""
    global _engine
    if _engine is None:
        _engine = _create_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """获取全局会话工厂（懒初始化）"""
    global _session_factory
    if _session_factory is None:
        engine = get_engine()
        _session_factory = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False
        )
    return _session_factory


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """获取异步数据库会话（上下文管理器）

    用法:
        async with get_session() as session:
            result = await session.execute(...)
    """
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def run_sync(func, *args, **kwargs):
    """兼容层：保持向后兼容的 run_sync 函数。
    新代码应该直接 await async 函数，不再需要 run_sync。
    """
    return await func(*args, **kwargs)


def _model_to_dict(obj) -> Dict:
    """将 ORM 对象转为字典"""
    return {c.key: getattr(obj, c.key) for c in obj.__table__.columns}


async def init_db():
    """Serialize upgrades across nodes before accepting traffic."""
    from db.schema import upgrade
    async with get_engine().connect() as conn:
        if DB_TYPE == 'mysql':
            locked = await conn.scalar(text("SELECT GET_LOCK('fileid_schema_upgrade', 60)"))
            await conn.commit()
            if locked != 1:
                raise RuntimeError('Another node is upgrading the database; retry startup later')
            try:
                async with conn.begin():
                    await upgrade(conn)
                    await _backfill_bot_db_id(conn)
            finally:
                await conn.execute(text("SELECT RELEASE_LOCK('fileid_schema_upgrade')"))
                await conn.commit()
        else:
            async with conn.begin():
                await conn.exec_driver_sql('BEGIN IMMEDIATE')
                await upgrade(conn)
                await _backfill_bot_db_id(conn)
    logger.info("数据库初始化完成 (type=%s)", DB_TYPE)


async def _backfill_bot_db_id(conn):
    """Attach legacy rows while preserving every code, even duplicate identities."""
    bots = (await conn.execute(text(
        "SELECT id, bot_username, created_at FROM user_bots ORDER BY bot_username, created_at, id"
    ))).fetchall()
    by_name = {}
    for bid, name, created in bots:
        by_name.setdefault(name, []).append((bid, created or ''))
    identities = {}
    for table in ('file_mappings', 'collections'):
        identity_column = ', file_unique_id' if table == 'file_mappings' else ''
        rows = (await conn.execute(text(
            f'SELECT id, bot_username, created_at{identity_column} FROM {table} WHERE bot_db_id IS NULL ORDER BY id'
        ))).fetchall()
        changes = []
        for row in rows:
            candidates = by_name.get(row[1], [])
            if not candidates:
                continue
            bid = candidates[0][0]
            for candidate, created in candidates:
                if created <= (row[2] or ''):
                    bid = candidate
                else:
                    break
            change = {'bid': bid, 'rid': row[0]}
            if table == 'file_mappings':
                if bid not in identities:
                    identities[bid] = set((await conn.execute(text(
                        'SELECT file_unique_id FROM file_mappings WHERE bot_db_id=:bid AND file_unique_id IS NOT NULL'
                    ), {'bid': bid})).scalars())
                identity = row[3]
                change['identity'] = identity if identity and identity not in identities[bid] else None
                if identity:
                    identities[bid].add(identity)
            changes.append(change)
        if changes:
            extra = ', file_unique_id=:identity' if table == 'file_mappings' else ''
            await conn.execute(text(f'UPDATE {table} SET bot_db_id=:bid{extra} WHERE id=:rid'), changes)
            logger.info('Legacy association: %s, %d rows', table, len(changes))


async def close_db():
    """关闭数据库引擎"""
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("数据库连接已关闭")