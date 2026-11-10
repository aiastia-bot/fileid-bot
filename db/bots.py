"""用户Bot管理相关数据库函数（Async SQLAlchemy）"""
import logging
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict

from sqlalchemy import select, update, func, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from db.core import get_session, _model_to_dict
from db.models import UserBot, UserBlacklist, PlatformSetting

logger = logging.getLogger(__name__)

# ==================== 黑名单查询缓存 ====================
# blacklist_check_handler 是主 Bot 每条 update 都会走的中间件，
# 不缓存时等于每条消息查一次 DB。TTL 60s，黑名单增删时主动失效。
_blacklist_cache: Dict[int, tuple] = {}  # user_id -> (is_blacklisted, ts)
_BLACKLIST_TTL = 60
_BLACKLIST_CACHE_MAX = 20000


async def add_user_bot(owner_id: int, bot_token: str, bot_id: int,
                       bot_username: str, bot_firstname: str) -> Optional[int]:
    async with get_session() as session:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 复用已删除的记录
            result = await session.execute(
                select(UserBot).where(
                    UserBot.bot_id == bot_id,
                    UserBot.status == 'deleted'
                ).limit(1)
            )
            deleted_bot = result.scalar_one_or_none()

            if deleted_bot:
                deleted_bot.owner_id = owner_id
                deleted_bot.bot_token = bot_token
                deleted_bot.bot_username = bot_username
                deleted_bot.bot_firstname = bot_firstname
                deleted_bot.status = 'active'
                deleted_bot.updated_at = now
                deleted_bot.node_id = 'local'
                await session.commit()
                logger.info("Reused deleted bot record (bot_db_id=%d, telegram_id=%d)", deleted_bot.id, bot_id)
                return deleted_bot.id

            bot = UserBot(
                owner_id=owner_id, bot_token=bot_token, bot_id=bot_id,
                bot_username=bot_username, bot_firstname=bot_firstname,
                status='active', created_at=now, updated_at=now
            )
            session.add(bot)
            await session.commit()
            return bot.id
        except Exception as e:
            if 'unique' in str(e).lower() or 'integrity' in str(e).lower():
                logger.error("Bot Token already exists")
                return None
            logger.error("Failed to add user bot: %s", e)
            return None


async def get_all_active_user_bots() -> List[Dict]:
    """获取所有活跃的用户Bot"""
    async with get_session() as session:
        result = await session.execute(
            select(UserBot).where(UserBot.status == 'active').order_by(UserBot.created_at)
        )
        bots = result.scalars().all()
        return [_model_to_dict(b) for b in bots]


async def get_all_owner_ids(status: str = None) -> List[int]:
    """获取Bot所有者ID（去重）
    
    Args:
        status: 筛选状态，如 'active'。不传则返回所有非删除的。
    """
    async with get_session() as session:
        if status:
            result = await session.execute(
                select(UserBot.owner_id).where(UserBot.status == status).distinct()
            )
        else:
            result = await session.execute(
                select(UserBot.owner_id).where(UserBot.status != 'deleted').distinct()
            )
        return [r[0] for r in result.fetchall()]


async def get_user_bots_by_owner(owner_id: int) -> List[Dict]:
    """获取用户的所有Bot"""
    async with get_session() as session:
        result = await session.execute(
            select(UserBot).where(
                UserBot.owner_id == owner_id,
                UserBot.status != 'deleted'
            ).order_by(UserBot.created_at)
        )
        bots = result.scalars().all()
        return [_model_to_dict(b) for b in bots]


async def get_user_bot_by_id(bot_db_id: int) -> Optional[Dict]:
    """根据数据库ID获取用户Bot"""
    async with get_session() as session:
        result = await session.execute(
            select(UserBot).where(UserBot.id == bot_db_id)
        )
        bot = result.scalar_one_or_none()
        return _model_to_dict(bot) if bot else None


async def get_user_bot_by_token(bot_token: str) -> Optional[Dict]:
    """根据Token获取用户Bot"""
    async with get_session() as session:
        result = await session.execute(
            select(UserBot).where(
                UserBot.bot_token == bot_token,
                UserBot.status != 'deleted'
            )
        )
        bot = result.scalar_one_or_none()
        return _model_to_dict(bot) if bot else None


async def get_user_bot_by_telegram_id(bot_id: int) -> Optional[Dict]:
    """根据Telegram Bot ID获取用户Bot"""
    async with get_session() as session:
        result = await session.execute(
            select(UserBot).where(
                UserBot.bot_id == bot_id,
                UserBot.status != 'deleted'
            )
        )
        bot = result.scalar_one_or_none()
        return _model_to_dict(bot) if bot else None


async def is_bot_admin_stopped(bot_id: int) -> bool:
    """检查某个 Telegram Bot ID 是否被系统停止（包括已删除的记录，防止换账号绕过）"""
    async with get_session() as session:
        result = await session.execute(
            select(UserBot.id).where(
                UserBot.bot_id == bot_id,
                UserBot.status == 'admin_stopped'
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None


async def update_user_bot_status(bot_db_id: int, status: str) -> bool:
    """更新用户Bot状态"""
    async with get_session() as session:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await session.execute(
                update(UserBot)
                .where(UserBot.id == bot_db_id)
                .values(status=status, updated_at=now)
            )
            await session.commit()
            return True
        except Exception as e:
            logger.error("更新Bot状态失败: %s", e)
            return False


async def update_user_bot_token(bot_db_id: int, new_token: str, new_bot_id: int = None, keep_status: bool = False) -> bool:
    """更新用户Bot的Token
    
    Args:
        keep_status: 如果为 True，保持当前状态不变（用于被系统停止的Bot更新Token）
    """
    async with get_session() as session:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            values: Dict = {'bot_token': new_token, 'updated_at': now}
            if not keep_status:
                values['status'] = 'active'
            if new_bot_id is not None:
                values['bot_id'] = new_bot_id
            await session.execute(
                update(UserBot).where(UserBot.id == bot_db_id).values(**values)
            )
            await session.commit()
            return True
        except Exception as e:
            if 'unique' in str(e).lower() or 'integrity' in str(e).lower():
                logger.error("更新Token失败：Token已存在")
                return False
            logger.error("更新Bot Token失败: %s", e)
            return False


async def delete_user_bot(bot_db_id: int) -> bool:
    """软删除用户Bot"""
    return await update_user_bot_status(bot_db_id, 'deleted')


async def update_user_bot_node(bot_db_id: int, node_id: str) -> bool:
    """更新用户 Bot 分配的节点"""
    async with get_session() as session:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await session.execute(
                update(UserBot)
                .where(UserBot.id == bot_db_id)
                .values(node_id=node_id, updated_at=now)
            )
            await session.commit()
            return True
        except Exception as e:
            logger.error("更新 Bot 节点分配失败: %s", e)
            return False


async def get_user_bot_by_username(bot_username: str, include_deleted: bool = False) -> Optional[Dict]:
    """根据 bot_username 获取用户Bot（默认只返回活跃 Bot；include_deleted 用于清理场景）"""
    async with get_session() as session:
        query = select(UserBot).where(UserBot.bot_username == bot_username)
        if not include_deleted:
            query = query.where(UserBot.status != 'deleted')
        result = await session.execute(
            query.order_by(UserBot.created_at.desc()).limit(1)
        )
        bot = result.scalar_one_or_none()
        return _model_to_dict(bot) if bot else None


async def unban_user_bots(owner_id: int) -> bool:
    """恢复被 ban 的用户的所有 Bot 为 active"""
    async with get_session() as session:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await session.execute(
                update(UserBot)
                .where(UserBot.owner_id == owner_id, UserBot.status == 'banned')
                .values(status='active', updated_at=now)
            )
            await session.commit()
            return True
        except Exception as e:
            logger.error("恢复用户Bot失败: %s", e)
            return False


async def get_active_bots_by_node(node_id: str) -> List[Dict]:
    """获取指定节点上的所有活跃 Bot"""
    async with get_session() as session:
        result = await session.execute(
            select(UserBot).where(
                UserBot.status == 'active',
                UserBot.node_id == node_id
            ).order_by(UserBot.created_at)
        )
        bots = result.scalars().all()
        return [_model_to_dict(b) for b in bots]


# ==================== 黑名单管理 ====================

async def add_to_blacklist(user_id: int, reason: str = '') -> bool:
    """添加用户到黑名单"""
    from db.core import get_engine
    from config import DB_TYPE
    async with get_session() as session:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            # 使用 merge（INSERT ... ON DUPLICATE KEY UPDATE 效果）
            existing_result = await session.execute(
                select(UserBlacklist).where(UserBlacklist.user_id == user_id)
            )
            existing = existing_result.scalar_one_or_none()
            if existing:
                existing.reason = reason
                existing.created_at = now
            else:
                session.add(UserBlacklist(user_id=user_id, reason=reason, created_at=now))
            await session.commit()
            _blacklist_cache.pop(user_id, None)  # 立即失效
            return True
        except Exception as e:
            logger.error("添加黑名单失败: %s", e)
            return False


async def remove_from_blacklist(user_id: int) -> bool:
    """从黑名单移除用户"""
    async with get_session() as session:
        try:
            result = await session.execute(
                select(UserBlacklist).where(UserBlacklist.user_id == user_id)
            )
            entry = result.scalar_one_or_none()
            if entry:
                await session.delete(entry)
                await session.commit()
                _blacklist_cache.pop(user_id, None)  # 立即失效
                return True
            return False
        except Exception as e:
            logger.error("移除黑名单失败: %s", e)
            return False


async def is_user_blacklisted(user_id: int) -> bool:
    """检查用户是否在黑名单中（带 60s TTL 缓存，黑名单增删时主动失效）"""
    now = time.time()
    entry = _blacklist_cache.get(user_id)
    if entry is not None and now - entry[1] < _BLACKLIST_TTL:
        return entry[0]

    async with get_session() as session:
        result = await session.execute(
            select(UserBlacklist.id).where(UserBlacklist.user_id == user_id).limit(1)
        )
        blacklisted = result.scalar_one_or_none() is not None

    # 容量保护：超限时先清过期项，仍超限则丢最旧的一半
    if len(_blacklist_cache) >= _BLACKLIST_CACHE_MAX:
        expired = [k for k, (_, ts) in _blacklist_cache.items() if now - ts >= _BLACKLIST_TTL]
        for k in expired:
            del _blacklist_cache[k]
        if len(_blacklist_cache) >= _BLACKLIST_CACHE_MAX:
            for k in sorted(_blacklist_cache, key=lambda k: _blacklist_cache[k][1])[:_BLACKLIST_CACHE_MAX // 2]:
                del _blacklist_cache[k]

    _blacklist_cache[user_id] = (blacklisted, now)
    return blacklisted


async def get_blacklist() -> List[Dict]:
    """获取黑名单列表"""
    async with get_session() as session:
        result = await session.execute(
            select(UserBlacklist).order_by(UserBlacklist.created_at.desc())
        )
        entries = result.scalars().all()
        return [_model_to_dict(e) for e in entries]


async def get_blacklist_count() -> int:
    """获取黑名单用户数量"""
    async with get_session() as session:
        result = await session.execute(
            select(func.count()).select_from(UserBlacklist)
        )
        return result.scalar() or 0


# ==================== 平台设置 ====================

async def get_platform_setting(key: str, default: str = '') -> str:
    """获取平台设置"""
    async with get_session() as session:
        result = await session.execute(
            select(PlatformSetting).where(PlatformSetting.key == key)
        )
        setting = result.scalar_one_or_none()
        return setting.value if setting else default


async def set_platform_setting(key: str, value: str) -> bool:
    """设置平台设置"""
    async with get_session() as session:
        try:
            result = await session.execute(
                select(PlatformSetting).where(PlatformSetting.key == key)
            )
            setting = result.scalar_one_or_none()
            if setting:
                setting.value = value
            else:
                session.add(PlatformSetting(key=key, value=value))
            await session.commit()
            return True
        except Exception as e:
            logger.error("设置平台配置失败: %s", e)
            return False

async def get_dead_bots_past_grace(days: int, limit: int = 20,
                                   not_before: str = None) -> List[Dict]:
    """死亡超过宽限期的 Bot（revoked/deleted/banned），供自动清理使用

    updated_at 是 "%Y-%m-%d %H:%M:%S" 字符串，字典序即时间序。
    not_before：只考虑该时刻之后死亡的 Bot（部署基线，保护存量数据）。
    admin_stopped / paused 属临时状态，不参与自动清理。
    """
    if days <= 0:
        return []
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    async with get_session() as session:
        query = select(UserBot).where(
            UserBot.status.in_(['revoked', 'deleted', 'banned']),
            UserBot.updated_at.isnot(None),
            UserBot.updated_at < cutoff,
        )
        if not_before:
            query = query.where(UserBot.updated_at >= not_before)
        rows = (await session.execute(
            query.order_by(UserBot.updated_at).limit(limit)
        )).scalars().all()
    return [_model_to_dict(r) for r in rows]
