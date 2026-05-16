"""VIP 用户管理和星星支付相关数据库函数"""
import logging
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict

from sqlalchemy import select, update, func, text

from db.core import get_session, _model_to_dict
from db.models import User, StarPayment, UserBot, UserBotPref
from config import VIP_PLANS, VIP_FEATURES, MAX_VIP0_USERS

logger = logging.getLogger(__name__)


async def _get_redis():
    """延迟导入获取 Redis 实例"""
    from redis_manager import get_redis
    return await get_redis()


async def get_or_create_user(user_id: int) -> Dict:
    """获取或创建用户记录，返回用户信息字典"""
    async with get_session() as session:
        result = await session.execute(
            select(User).where(User.user_id == user_id)
        )
        user = result.scalar_one_or_none()

        if not user:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            user = User(
                user_id=user_id,
                vip_level=0,
                vip_expire_at=None,
                created_at=now,
            )
            session.add(user)
            await session.commit()
            # 重新查询以获取完整对象
            result = await session.execute(
                select(User).where(User.user_id == user_id)
            )
            user = result.scalar_one()

        return _model_to_dict(user)


async def get_user_vip_level(user_id: int) -> int:
    """获取用户的有效VIP等级（自动检查过期），带缓存"""
    # 尝试从缓存读取
    r = await _get_redis()
    cached = await r.cache_get(f"vip_level:{user_id}")
    if cached is not None:
        return int(cached)

    user = await get_or_create_user(user_id)
    level = user.get('vip_level', 0)
    expire_at = user.get('vip_expire_at')

    if level > 0 and expire_at:
        try:
            expire_dt = datetime.strptime(expire_at, "%Y-%m-%d %H:%M:%S")
            if datetime.now() > expire_dt:
                # VIP 已过期，降回 0
                await _downgrade_expired_user(user_id)
                level = 0
        except ValueError:
            pass

    # 缓存 5 分钟（过期信息缓存在 VIP 过期前会自动失效）
    await r.cache_set(f"vip_level:{user_id}", str(level), ttl=300)
    return level


async def get_user_vip_info(user_id: int) -> Dict:
    """获取用户VIP完整信息（包含是否过期、剩余天数等）"""
    user = await get_or_create_user(user_id)
    level = user.get('vip_level', 0)
    expire_at = user.get('vip_expire_at')
    plan = VIP_PLANS.get(level, VIP_PLANS[0])

    is_active = True
    remaining_days = 0

    if level > 0 and expire_at:
        try:
            expire_dt = datetime.strptime(expire_at, "%Y-%m-%d %H:%M:%S")
            now = datetime.now()
            if now > expire_dt:
                is_active = False
                remaining_days = 0
            else:
                remaining_days = (expire_dt - now).days
        except ValueError:
            is_active = False
    elif level == 0:
        is_active = True  # 免费用户始终"有效"

    return {
        'user_id': user_id,
        'vip_level': level,
        'vip_name': plan['name'],
        'max_bots': plan['max_bots'],
        'vip_expire_at': expire_at,
        'is_active': is_active,
        'remaining_days': remaining_days,
    }


async def get_max_bots_for_user(user_id: int) -> int:
    """获取用户可创建的最大Bot数量"""
    level = await get_user_vip_level(user_id)
    plan = VIP_PLANS.get(level, VIP_PLANS[0])
    return plan['max_bots']


async def update_user_vip(user_id: int, level: int, months: int) -> bool:
    """升级/续费用户VIP，时间叠加"""
    async with get_session() as session:
        try:
            result = await session.execute(
                select(User).where(User.user_id == user_id)
            )
            user = result.scalar_one_or_none()
            if not user:
                return False

            now = datetime.now()

            # 计算过期时间：从当前过期时间或现在开始叠加
            if user.vip_expire_at:
                try:
                    current_expire = datetime.strptime(user.vip_expire_at, "%Y-%m-%d %H:%M:%S")
                    # 如果当前VIP还没过期，从过期时间开始叠加
                    base_time = max(now, current_expire)
                except ValueError:
                    base_time = now
            else:
                base_time = now

            new_expire = base_time + timedelta(days=30 * months)
            user.vip_level = level
            user.vip_expire_at = new_expire.strftime("%Y-%m-%d %H:%M:%S")

            await session.commit()
            logger.info("用户 %s VIP 升级到 %d（%d个月），过期时间: %s",
                       user_id, level, months, user.vip_expire_at)
            # 清除 VIP 缓存
            r = await _get_redis()
            await r.cache_delete(f"vip_level:{user_id}")
            await r.cache_delete(f"vip_info:{user_id}")
            return True
        except Exception as e:
            logger.error("更新用户VIP失败: %s", e)
            return False


async def record_star_payment(user_id: int, amount: int, vip_level: int,
                               months: int, payload: str,
                               telegram_charge_id: str = None) -> Optional[int]:
    """记录星星支付"""
    async with get_session() as session:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            payment = StarPayment(
                user_id=user_id,
                amount=amount,
                vip_level=vip_level,
                months=months,
                payload=payload,
                telegram_charge_id=telegram_charge_id,
                created_at=now,
            )
            session.add(payment)
            await session.commit()
            return payment.id
        except Exception as e:
            logger.error("记录支付失败: %s", e)
            return None


async def get_payment_history(user_id: int, limit: int = 10) -> List[Dict]:
    """获取用户支付历史"""
    async with get_session() as session:
        result = await session.execute(
            select(StarPayment)
            .where(StarPayment.user_id == user_id)
            .order_by(StarPayment.created_at.desc())
            .limit(limit)
        )
        payments = result.scalars().all()
        return [_model_to_dict(p) for p in payments]


async def _downgrade_expired_user(user_id: int) -> bool:
    """将过期用户降回VIP 0"""
    async with get_session() as session:
        try:
            await session.execute(
                update(User)
                .where(User.user_id == user_id)
                .values(vip_level=0, vip_expire_at=None)
            )
            await session.commit()
            logger.info("用户 %s VIP 已过期，降回 VIP 0", user_id)
            # 清除 VIP 缓存
            r = await _get_redis()
            await r.cache_delete(f"vip_level:{user_id}")
            await r.cache_delete(f"vip_info:{user_id}")
            return True
        except Exception as e:
            logger.error("降级用户VIP失败: %s", e)
            return False


async def get_expiring_users(days: int = 3) -> List[Dict]:
    """获取即将过期的VIP用户列表（默认3天内）"""
    async with get_session() as session:
        now = datetime.now()
        threshold = now + timedelta(days=days)
        threshold_str = threshold.strftime("%Y-%m-%d %H:%M:%S")
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")

        result = await session.execute(
            select(User).where(
                User.vip_level > 0,
                User.vip_expire_at != None,  # noqa: E711
                User.vip_expire_at > now_str,
                User.vip_expire_at <= threshold_str,
            )
        )
        users = result.scalars().all()
        return [_model_to_dict(u) for u in users]


async def get_expired_users() -> List[Dict]:
    """获取所有已过期但未降级的VIP用户"""
    async with get_session() as session:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        result = await session.execute(
            select(User).where(
                User.vip_level > 0,
                User.vip_expire_at != None,  # noqa: E711
                User.vip_expire_at <= now_str,
            )
        )
        users = result.scalars().all()
        return [_model_to_dict(u) for u in users]


async def get_active_bots_count_by_owner(owner_id: int) -> int:
    """获取用户活跃Bot数量（不含已删除的）"""
    async with get_session() as session:
        result = await session.execute(
            select(func.count()).select_from(UserBot).where(
                UserBot.owner_id == owner_id,
                UserBot.status != 'deleted'
            )
        )
        return result.scalar() or 0


async def get_active_bots_by_owner(owner_id: int) -> List[Dict]:
    """获取用户所有活跃Bot（不含已删除的），按创建时间排序"""
    async with get_session() as session:
        result = await session.execute(
            select(UserBot).where(
                UserBot.owner_id == owner_id,
                UserBot.status != 'deleted'
            ).order_by(UserBot.created_at)
        )
        bots = result.scalars().all()
        return [_model_to_dict(b) for b in bots]


async def pause_user_bot(bot_db_id: int) -> bool:
    """暂停用户Bot（状态改为 paused）"""
    async with get_session() as session:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await session.execute(
                update(UserBot)
                .where(UserBot.id == bot_db_id)
                .values(status='paused', updated_at=now)
            )
            await session.commit()
            return True
        except Exception as e:
            logger.error("暂停Bot失败: %s", e)
            return False


async def resume_user_bot(bot_db_id: int) -> bool:
    """恢复暂停的用户Bot（状态改为 active）"""
    async with get_session() as session:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await session.execute(
                update(UserBot)
                .where(UserBot.id == bot_db_id)
                .values(status='active', updated_at=now)
            )
            await session.commit()
            return True
        except Exception as e:
            logger.error("恢复Bot失败: %s", e)
            return False


async def get_vip0_user_count() -> int:
    """获取当前 VIP 0（免费）用户数量（有 Bot 的用户）"""
    async with get_session() as session:
        result = await session.execute(
            select(func.count(func.distinct(UserBot.owner_id))).select_from(UserBot)
            .where(UserBot.status != 'deleted')
        )
        return result.scalar() or 0


async def check_vip0_capacity(user_id: int) -> bool:
    """检查 VIP 0 用户是否还能创建 Bot（受 MAX_VIP0_USERS 限制）
    返回 True 表示可以创建，False 表示已满
    已有 Bot 的老用户不受此限制
    """
    if MAX_VIP0_USERS <= 0:
        return True  # 不限制

    level = await get_user_vip_level(user_id)
    if level > 0:
        return True  # VIP 用户不受此限制

    # 已有 Bot 的用户不受限制（已占用名额，允许在其 max_bots 内继续操作）
    existing_bots = await get_active_bots_count_by_owner(user_id)
    if existing_bots > 0:
        return True

    count = await get_vip0_user_count()
    return count < MAX_VIP0_USERS


async def get_paused_bots_by_owner(owner_id: int) -> List[Dict]:
    """获取用户暂停的Bot列表"""
    async with get_session() as session:
        result = await session.execute(
            select(UserBot).where(
                UserBot.owner_id == owner_id,
                UserBot.status == 'paused'
            ).order_by(UserBot.created_at)
        )
        bots = result.scalars().all()
        return [_model_to_dict(b) for b in bots]


# ===== 转发保护相关函数 =====
# forward_mode: 0=默认允许, -1=禁止转发, 1=用户自定义
# 使用内存缓存减少数据库查询

_forward_mode_cache: dict = {}      # {bot_db_id: (mode, timestamp)}
_user_pref_cache: dict = {}          # {(user_id, bot_db_id): (protect, timestamp)}
_CACHE_TTL = 300                     # 缓存 5 分钟


def _cache_get(cache: dict, key):
    """从缓存读取，过期返回 None"""
    entry = cache.get(key)
    if entry and time.time() - entry[1] < _CACHE_TTL:
        return entry[0]
    return None


def _cache_set(cache: dict, key, value):
    """写入缓存"""
    cache[key] = (value, time.time())


def _cache_del(cache: dict, key):
    """删除缓存"""
    cache.pop(key, None)


async def get_bot_forward_mode(bot_db_id: int) -> int:
    """获取 Bot 的转发模式（带缓存），返回整数：0=允许, -1=禁止, 1=用户自定义"""
    cached = _cache_get(_forward_mode_cache, bot_db_id)
    if cached is not None:
        return cached

    async with get_session() as session:
        result = await session.execute(
            select(UserBot.forward_mode).where(UserBot.id == bot_db_id)
        )
        mode = result.scalar_one_or_none()
        mode = mode if mode is not None else 0
    _cache_set(_forward_mode_cache, bot_db_id, mode)
    return mode


async def set_bot_forward_mode(bot_db_id: int, mode: int) -> bool:
    """设置 Bot 的转发模式并更新缓存"""
    async with get_session() as session:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await session.execute(
                update(UserBot)
                .where(UserBot.id == bot_db_id)
                .values(forward_mode=mode, updated_at=now)
            )
            await session.commit()
            _cache_set(_forward_mode_cache, bot_db_id, mode)
            # Bot 模式变更时，清除该 Bot 所有用户偏好缓存
            _user_pref_cache.update(
                {k: v for k, v in list(_user_pref_cache.items()) if k[1] != bot_db_id}
            )
            # 实际上是删除，重建缓存
            to_del = [k for k in _user_pref_cache if k[1] == bot_db_id]
            for k in to_del:
                del _user_pref_cache[k]
            return True
        except Exception as e:
            logger.error("设置转发模式失败: %s", e)
            return False


async def get_user_forward_protect(user_id: int, bot_db_id: int) -> int:
    """获取用户对某个 Bot 的转发保护偏好（带缓存），0=不保护(允许), 1=保护(禁止)"""
    cache_key = (user_id, bot_db_id)
    cached = _cache_get(_user_pref_cache, cache_key)
    if cached is not None:
        return cached

    async with get_session() as session:
        result = await session.execute(
            select(UserBotPref).where(
                UserBotPref.user_id == user_id,
                UserBotPref.bot_db_id == bot_db_id
            )
        )
        pref = result.scalar_one_or_none()
        protect = pref.forward_protect if pref else 0
    _cache_set(_user_pref_cache, cache_key, protect)
    return protect


async def set_user_forward_protect(user_id: int, bot_db_id: int, protect: int) -> bool:
    """设置用户对某个 Bot 的转发保护偏好并更新缓存"""
    async with get_session() as session:
        try:
            result = await session.execute(
                select(UserBotPref).where(
                    UserBotPref.user_id == user_id,
                    UserBotPref.bot_db_id == bot_db_id
                )
            )
            pref = result.scalar_one_or_none()
            if pref:
                pref.forward_protect = protect
            else:
                pref = UserBotPref(
                    user_id=user_id,
                    bot_db_id=bot_db_id,
                    forward_protect=protect,
                )
                session.add(pref)
            await session.commit()
            _cache_set(_user_pref_cache, (user_id, bot_db_id), protect)
            return True
        except Exception as e:
            logger.error("设置用户转发保护偏好失败: %s", e)
            return False


async def should_protect_content(user_id: int, bot_db_id: int) -> bool:
    """判断是否应该对发给该用户的图片/视频添加转发保护（带缓存）

    逻辑：
    - forward_mode == 0  → 不保护（默认允许）
    - forward_mode == -1 → 保护（禁止转发）
    - forward_mode == 1  → 查用户偏好表 user_bot_prefs
    """
    forward_mode = await get_bot_forward_mode(bot_db_id)  # 带缓存，通常 0 次DB查询

    if forward_mode == 0:
        return False
    elif forward_mode == -1:
        return True
    elif forward_mode == 1:
        protect = await get_user_forward_protect(user_id, bot_db_id)  # 带缓存
        return bool(protect)
    return False
