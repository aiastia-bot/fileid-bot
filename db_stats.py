"""统计相关数据库函数（Async SQLAlchemy）"""
import logging
from typing import Dict, List

from sqlalchemy import select, func, text

from db_core import get_session
from models import FileMapping, Collection, UserBot

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

        # 总用户数（去重 owner_id）
        result = await session.execute(
            select(func.count(func.distinct(UserBot.owner_id))).select_from(UserBot)
            .where(UserBot.status != 'deleted')
        )
        total_users = result.scalar() or 0

        # 总文件数
        result = await session.execute(
            select(func.count()).select_from(FileMapping)
        )
        total_files = result.scalar() or 0

        # 总集合数
        result = await session.execute(
            select(func.count()).select_from(Collection).where(Collection.status == 'completed')
        )
        total_collections = result.scalar() or 0

        return {
            'bot_count': active_bots,
            'total_bots': total_bots,
            'owner_count': total_users,
            'file_count': total_files,
            'col_count': total_collections,
        }


async def get_platform_bot_details() -> List[Dict]:
    """获取平台级 Bot 详情列表（含文件数统计）"""
    async with get_session() as session:
        result = await session.execute(
            select(UserBot).where(UserBot.status != 'deleted').order_by(UserBot.bot_username)
        )
        bots = result.scalars().all()

        details = []
        for bot in bots:
            file_count = 0
            col_count = 0
            user_count = 0
            try:
                r = await session.execute(
                    select(func.count()).select_from(FileMapping).where(FileMapping.bot_db_id == bot.id)
                )
                file_count = r.scalar() or 0
            except Exception:
                pass
            try:
                r = await session.execute(
                    select(func.count()).select_from(Collection).where(
                        Collection.bot_db_id == bot.id, Collection.status == 'completed'
                    )
                )
                col_count = r.scalar() or 0
            except Exception:
                pass
            try:
                r = await session.execute(
                    select(func.count(func.distinct(FileMapping.user_id))).select_from(FileMapping)
                    .where(FileMapping.bot_db_id == bot.id)
                )
                user_count = r.scalar() or 0
            except Exception:
                pass

            details.append({
                'id': bot.id,
                'bot_username': bot.bot_username or '',
                'bot_firstname': bot.bot_firstname or bot.bot_username or '',
                'bot_id': bot.bot_id or '',
                'owner_id': bot.owner_id,
                'status': bot.status,
                'created_at': bot.created_at or '',
                'file_count': file_count,
                'col_count': col_count,
                'user_count': user_count,
            })

        return details


async def get_platform_export_data() -> Dict:
    """导出平台全部数据（Bot、文件、集合、黑名单）"""
    async with get_session() as session:
        from models import UserBlacklist
        from db_core import _model_to_dict

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
