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
        'pool_pre_ping': True,
    }
    if DB_TYPE == 'sqlite':
        # SQLite 特定配置
        engine_kwargs['connect_args'] = {'timeout': 30}
        # WAL 模式通过 on_connect 事件设置
    else:
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
    """初始化数据库（创建表）"""
    engine = get_engine()

    if DB_TYPE == 'sqlite':
        # SQLite: 自动创建所有表（如果不存在）
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

            # 迁移：检查并添加缺失的列
            # 检查 is_valid 列
            result = await conn.execute(text("PRAGMA table_info(file_mappings)"))
            columns = {row[1] for row in result}
            if 'is_valid' not in columns:
                await conn.execute(text("ALTER TABLE file_mappings ADD COLUMN is_valid INTEGER DEFAULT 1"))
            if 'bot_db_id' not in columns:
                await conn.execute(text("ALTER TABLE file_mappings ADD COLUMN bot_db_id INTEGER"))

            # 检查 node_id 列
            result = await conn.execute(text("PRAGMA table_info(user_bots)"))
            columns = {row[1] for row in result}
            if 'node_id' not in columns:
                await conn.execute(text("ALTER TABLE user_bots ADD COLUMN node_id TEXT DEFAULT 'local'"))

            # 检查 collections.bot_db_id 列
            result = await conn.execute(text("PRAGMA table_info(collections)"))
            columns = {row[1] for row in result}
            if 'bot_db_id' not in columns:
                await conn.execute(text("ALTER TABLE collections ADD COLUMN bot_db_id INTEGER"))

            # 创建 users 表索引
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_users_user_id ON users(user_id)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sp_user ON star_payments(user_id)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_sp_payload ON star_payments(payload)"))

            # 创建索引
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_ub_owner ON user_bots(owner_id)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_ub_bot_id ON user_bots(bot_id)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_file_code ON file_mappings(code)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_file_user ON file_mappings(user_id)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_file_bot_db ON file_mappings(bot_db_id)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_col_code ON collections(code)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_col_user ON collections(user_id)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_col_bot_db ON collections(bot_db_id)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_ci_col ON collection_items(collection_code)"))
            await conn.execute(text("CREATE INDEX IF NOT EXISTS idx_bl_user ON user_blacklist(user_id)"))

            # 回填 bot_db_id
            await _backfill_bot_db_id(conn)
    else:
        # MySQL: 使用 CREATE TABLE IF NOT EXISTS（通过 ORM metadata）
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    logger.info("数据库初始化完成 (type=%s)", DB_TYPE)


async def _backfill_bot_db_id(conn):
    """回填 bot_db_id：将 NULL 的数据关联到正确的 Bot 记录"""
    # 检查是否需要回填
    try:
        result = await conn.execute(text("SELECT COUNT(*) as c FROM file_mappings WHERE bot_db_id IS NULL"))
        null_files = result.scalar()
        result = await conn.execute(text("SELECT COUNT(*) as c FROM collections WHERE bot_db_id IS NULL"))
        null_cols = result.scalar()
    except Exception as e:
        logger.error("检查 NULL bot_db_id 失败: %s", e)
        return

    if null_files == 0 and null_cols == 0:
        logger.info("无需回填 bot_db_id")
        return

    logger.info("开始回填 bot_db_id: %d 个文件, %d 个集合", null_files, null_cols)

    # 获取所有 Bot 记录
    try:
        result = await conn.execute(
            text("SELECT id, bot_username, created_at FROM user_bots ORDER BY bot_username, created_at")
        )
        all_bots = result.fetchall()
    except Exception as e:
        logger.error("获取 Bot 记录失败: %s", e)
        return

    # 按 bot_username 分组
    bots_by_username: Dict[str, list] = {}
    for bot in all_bots:
        uname = bot[1]  # bot_username
        if uname not in bots_by_username:
            bots_by_username[uname] = []
        bots_by_username[uname].append({
            'id': bot[0],
            'created_at': bot[2] or '',
        })

    logger.info("找到 %d 个不同 bot_username 的 Bot 记录", len(bots_by_username))

    updated_files = 0
    updated_cols = 0

    for username, bots in bots_by_username.items():
        if len(bots) == 1:
            bot_id = bots[0]['id']
            r1 = await conn.execute(
                text("UPDATE file_mappings SET bot_db_id = :bid WHERE bot_username = :uname AND bot_db_id IS NULL"),
                {'bid': bot_id, 'uname': username}
            )
            r2 = await conn.execute(
                text("UPDATE collections SET bot_db_id = :bid WHERE bot_username = :uname AND bot_db_id IS NULL"),
                {'bid': bot_id, 'uname': username}
            )
            updated_files += r1.rowcount
            updated_cols += r2.rowcount
            if r1.rowcount > 0 or r2.rowcount > 0:
                logger.info("  %s: 单 Bot，关联 %d 文件, %d 集合", username, r1.rowcount, r2.rowcount)
            continue

        # 多个同名 Bot：按时间区间分配
        for file_table in ['file_mappings', 'collections']:
            result = await conn.execute(
                text(f"SELECT id, created_at FROM {file_table} WHERE bot_username = :uname AND bot_db_id IS NULL"),
                {'uname': username}
            )
            rows = result.fetchall()

            for row in rows:
                data_created = row[1] or ''
                matched_bot_id = None

                for bot in bots:
                    if bot['created_at'] <= data_created:
                        matched_bot_id = bot['id']
                    else:
                        break

                if matched_bot_id is None and bots:
                    matched_bot_id = bots[0]['id']

                if matched_bot_id is not None:
                    await conn.execute(
                        text(f"UPDATE {file_table} SET bot_db_id = :bid WHERE id = :rid"),
                        {'bid': matched_bot_id, 'rid': row[0]}
                    )
                    if file_table == 'file_mappings':
                        updated_files += 1
                    else:
                        updated_cols += 1

        logger.info("  %s: %d 个同名 Bot，已分配", username, len(bots))

    # 检查残留 NULL
    result = await conn.execute(text("SELECT COUNT(*) as c FROM file_mappings WHERE bot_db_id IS NULL"))
    remain_files = result.scalar()
    result = await conn.execute(text("SELECT COUNT(*) as c FROM collections WHERE bot_db_id IS NULL"))
    remain_cols = result.scalar()

    logger.info("回填完成: %d 文件, %d 集合已关联。剩余 NULL: %d 文件, %d 集合",
                updated_files, updated_cols, remain_files, remain_cols)

    if remain_files > 0 or remain_cols > 0:
        result = await conn.execute(
            text("SELECT DISTINCT bot_username FROM file_mappings WHERE bot_db_id IS NULL")
        )
        for row in result.fetchall():
            logger.warning("  未匹配的 bot_username: %s（Bot 记录可能已被硬删除）", row[0])


async def close_db():
    """关闭数据库引擎"""
    global _engine, _session_factory
    if _engine:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("数据库连接已关闭")