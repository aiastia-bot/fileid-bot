"""用户Bot管理相关数据库函数（Async SQLAlchemy）"""
import logging
from datetime import datetime
from typing import Optional, List, Dict

from sqlalchemy import select, update, func, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from db_core import get_session, _model_to_dict
from models import UserBot, UserBlacklist, PlatformSetting

logger = logging.getLogger(__name__)


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


async def update_user_bot_token(bot_db_id: int, new_token: str, new_bot_id: int = None) -> bool:
    """更新用户Bot的Token"""
    async with get_session() as session:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            values: Dict = {'bot_token': new_token, 'status': 'active', 'updated_at': now}
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


async def get_user_bot_by_username(bot_username: str) -> Optional[Dict]:
    """根据 bot_username 获取活跃用户Bot"""
    async with get_session() as session:
        result = await session.execute(
            select(UserBot).where(
                UserBot.bot_username == bot_username,
                UserBot.status != 'deleted'
            ).order_by(UserBot.created_at.desc()).limit(1)
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
    from db_core import get_engine
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
                return True
            return False
        except Exception as e:
            logger.error("移除黑名单失败: %s", e)
            return False


async def is_user_blacklisted(user_id: int) -> bool:
    """检查用户是否在黑名单中"""
    async with get_session() as session:
        result = await session.execute(
            select(UserBlacklist.id).where(UserBlacklist.user_id == user_id).limit(1)
        )
        return result.scalar_one_or_none() is not None


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