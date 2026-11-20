"""统计相关数据库函数（Async SQLAlchemy）"""
import logging
from typing import Dict, List

from sqlalchemy import select, func, text, or_

from db.core import get_session
from db.models import BotFileCount, FileMapping, Collection, UserBot

logger = logging.getLogger(__name__)


async def get_stats() -> Dict:
    """获取 Bot 统计信息（文件数、集合数、用户数、今日新增、按类型统计）"""
    async with get_session() as session:
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")

        # 总文件数
        result = await session.execute(
            select(func.count()).select_from(FileMapping)
        )
        file_count = result.scalar() or 0

        # 总集合数
        result = await session.execute(
            select(func.count()).select_from(Collection).where(Collection.status == 'completed')
        )
        col_count = result.scalar() or 0

        # 总用户数（去重 user_id）
        result = await session.execute(
            select(func.count(func.distinct(FileMapping.user_id))).select_from(FileMapping)
        )
        user_count = result.scalar() or 0

        # 今日新增文件
        result = await session.execute(
            select(func.count()).select_from(FileMapping).where(FileMapping.created_at >= today)
        )
        today_files = result.scalar() or 0

        # 按文件类型统计
        result = await session.execute(
            select(FileMapping.file_type, func.count().label('c'))
            .group_by(FileMapping.file_type)
        )
        type_stats = [{'file_type': r[0], 'c': r[1]} for r in result.fetchall()]

        return {
            'file_count': file_count,
            'col_count': col_count,
            'user_count': user_count,
            'today_files': today_files,
            'type_stats': type_stats,
        }


async def get_platform_stats() -> Dict:
    """获取平台级统计（所有 Bot 的汇总信息）"""
    async with get_session() as session:
        # 总 Bot 数
        result = await session.execute(
            select(func.count()).select_from(UserBot).where(UserBot.status != 'deleted')
        )
        total_bots = result.scalar() or 0

        # 活跃 Bot 数
        result = await session.execute(
            select(func.count()).select_from(UserBot).where(UserBot.status == 'active')
        )
        active_bots = result.scalar() or 0

        # 活跃 Bot 的 id 集合（用于过滤文件/集合统计，只统计存活 Bot 的）
        result = await session.execute(
            select(UserBot.id).where(UserBot.status == 'active')
        )
        active_bot_ids = [r[0] for r in result.fetchall()]

        # 总用户数（去重 owner_id）
        result = await session.execute(
            select(func.count(func.distinct(UserBot.owner_id))).select_from(UserBot)
            .where(UserBot.status != 'deleted')
        )
        total_users = result.scalar() or 0

        # 总文件数（仅统计存活 Bot 的）
        if active_bot_ids:
            result = await session.execute(
                select(func.count()).select_from(FileMapping)
                .where(FileMapping.bot_db_id.in_(active_bot_ids))
            )
            total_files = result.scalar() or 0
        else:
            total_files = 0

        # 总集合数（仅统计存活 Bot 的）
        if active_bot_ids:
            result = await session.execute(
                select(func.count()).select_from(Collection)
                .where(Collection.bot_db_id.in_(active_bot_ids), Collection.status == 'completed')
            )
            total_collections = result.scalar() or 0
        else:
            total_collections = 0

        return {
            'bot_count': active_bots,
            'total_bots': total_bots,
            'owner_count': total_users,
            'file_count': total_files,
            'col_count': total_collections,
        }


async def get_platform_bot_page(
    status: str, *, page: int, page_size: int, sort: str = 'name'
) -> tuple[List[Dict], int, int]:
    """先给匹配的 Bot 排序，再取一页并计算详情。"""
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")

    async with get_session() as session:
        cols = (
            UserBot.id, UserBot.bot_username, UserBot.bot_firstname,
            UserBot.bot_id, UserBot.owner_id, UserBot.status, UserBot.created_at, UserBot.node_id,
        )
        file_count = func.coalesce(BotFileCount.count, 0).label('file_count')
        query = (
            select(*cols, file_count)
            .outerjoin(BotFileCount, BotFileCount.bot_db_id == UserBot.id)
            .where(UserBot.status != 'deleted' if status == 'all' else UserBot.status == status)
        )
        if status == 'active':
            query = query.where(BotFileCount.count > 0)

        total = await session.scalar(query.with_only_columns(func.count())) or 0
        last_page = max(1, (total + page_size - 1) // page_size)
        page = max(1, min(page, last_page))
        if not total:
            return [], total, page

        order = [UserBot.bot_username, UserBot.id]
        if sort == 'today':
            # 每个 Bot 用 (bot_db_id, created_at) 索引只统计当天的记录。
            today_file_count = (
                select(func.count()).select_from(FileMapping)
                .where(FileMapping.bot_db_id == UserBot.id, FileMapping.created_at >= today)
                .scalar_subquery().label('today_file_count')
            )
            query = query.add_columns(today_file_count)
            order.insert(0, today_file_count.desc())
        elif sort == 'created':
            order.insert(0, UserBot.created_at.desc())
        elif sort == 'files':
            order.insert(0, file_count.desc())

        bots = (await session.execute(
            query.order_by(*order)
            .offset((page - 1) * page_size).limit(page_size)
        )).all()
        bot_ids = [bot.id for bot in bots]
        if not bot_ids:
            return [], total, page

        if sort == 'today':
            today_counts = {bot.id: bot.today_file_count for bot in bots}
        else:
            result = await session.execute(
                select(FileMapping.bot_db_id, func.count())
                .where(FileMapping.bot_db_id.in_(bot_ids), FileMapping.created_at >= today)
                .group_by(FileMapping.bot_db_id)
            )
            today_counts = dict(result.fetchall())

        result = await session.execute(
            select(Collection.bot_db_id, func.count())
            .where(Collection.bot_db_id.in_(bot_ids), Collection.status == 'completed')
            .group_by(Collection.bot_db_id)
        )
        col_counts = dict(result.fetchall())

        result = await session.execute(
            select(FileMapping.bot_db_id, func.count(func.distinct(FileMapping.user_id)))
            .where(FileMapping.bot_db_id.in_(bot_ids))
            .group_by(FileMapping.bot_db_id)
        )
        user_counts = dict(result.fetchall())

        return [
            {
                'id': b[0],
                'bot_username': b[1] or '',
                'bot_firstname': b[2] or b[1] or '',
                'bot_id': b[3] or '',
                'owner_id': b[4],
                'status': b[5],
                'created_at': b[6] or '',
                'node_id': b.node_id,
                'file_count': b.file_count,
                'today_file_count': today_counts.get(b[0], 0),
                'col_count': col_counts.get(b[0], 0),
                'user_count': user_counts.get(b[0], 0),
            }
            for b in bots
        ], total, page


async def get_platform_export_data() -> Dict:
    """导出平台全部数据（Bot、文件、集合、黑名单）

    ⚠️ 全量载入内存，仅适用于数据量小的场景；
    大数据量导出请使用下方 stream_* 系列流式函数。
    """
    async with get_session() as session:
        from db.models import UserBlacklist
        from db.core import _model_to_dict

        # 所有非删除 Bot
        result = await session.execute(
            select(UserBot).where(UserBot.status != 'deleted').order_by(UserBot.created_at)
        )
        bots = [_model_to_dict(b) for b in result.scalars().all()]

        # 所有文件
        result = await session.execute(
            select(FileMapping).order_by(FileMapping.created_at)
        )
        files = [_model_to_dict(f) for f in result.scalars().all()]

        # 所有集合
        result = await session.execute(
            select(Collection).order_by(Collection.created_at)
        )
        collections = [_model_to_dict(c) for c in result.scalars().all()]

        # 黑名单
        result = await session.execute(
            select(UserBlacklist).order_by(UserBlacklist.created_at)
        )
        blacklist = [_model_to_dict(b) for b in result.scalars().all()]

        return {
            'bots': bots,
            'files': files,
            'collections': collections,
            'blacklist': blacklist,
        }


# ==================== 流式导出（大数据量防内存尖峰） ====================

async def stream_active_bot_files(since_date: str = None):
    """流式获取活跃Bot的文件列表（每次取 500 行）

    用于 /export csv，替代 get_active_bot_files 的全量 fetchall。
    迭代期间避免执行耗时 await，以免长时间占用数据库连接。
    """
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

    async with get_session() as session:
        result = await session.stream(query)
        async for r in result.yield_per(500):
            yield {'code': r[0], 'bot_username': r[1], 'file_type': r[2],
                  'file_size': r[3], 'user_id': r[4], 'created_at': r[5]}


async def stream_export_bots():
    """流式导出所有非删除 Bot 记录"""
    async with get_session() as session:
        result = await session.stream(
            select(UserBot).where(UserBot.status != 'deleted').order_by(UserBot.created_at)
        )
        async for chunk in result.yield_per(500):
            for b in chunk:
                yield {
                    'id': b.id, 'owner_id': b.owner_id, 'bot_id': b.bot_id,
                    'bot_username': b.bot_username, 'bot_firstname': b.bot_firstname,
                    'status': b.status, 'created_at': b.created_at,
                }


async def stream_export_files():
    """流式导出所有文件记录"""
    async with get_session() as session:
        result = await session.stream(
            select(FileMapping).order_by(FileMapping.created_at)
        )
        async for chunk in result.yield_per(500):
            for f in chunk:
                yield {
                    'id': f.id, 'code': f.code, 'bot_username': f.bot_username,
                    'file_type': f.file_type, 'telegram_file_id': f.telegram_file_id,
                    'file_size': f.file_size, 'file_unique_id': f.file_unique_id,
                    'user_id': f.user_id, 'created_at': f.created_at,
                    'is_valid': f.is_valid, 'bot_db_id': f.bot_db_id,
                    'source_chat_id': f.source_chat_id,
                    'source_message_id': f.source_message_id,
                }


async def stream_export_collections():
    """流式导出所有集合记录"""
    async with get_session() as session:
        result = await session.stream(
            select(Collection).order_by(Collection.created_at)
        )
        async for chunk in result.yield_per(500):
            for c in chunk:
                yield {
                    'id': c.id, 'code': c.code, 'bot_username': c.bot_username,
                    'name': c.name, 'user_id': c.user_id, 'file_count': c.file_count,
                    'status': c.status, 'created_at': c.created_at,
                    'updated_at': c.updated_at, 'bot_db_id': c.bot_db_id,
                }


async def stream_export_blacklist():
    """流式导出黑名单"""
    from db.models import UserBlacklist
    async with get_session() as session:
        result = await session.stream(
            select(UserBlacklist).order_by(UserBlacklist.created_at)
        )
        async for chunk in result.yield_per(500):
            for b in chunk:
                yield {
                    'id': b.id, 'user_id': b.user_id, 'reason': b.reason,
                    'created_at': b.created_at,
                }
