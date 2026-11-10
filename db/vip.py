"""VIP 用户管理和星星支付相关数据库函数"""
import logging
import time
from datetime import datetime, timedelta
from typing import Optional, List, Dict

from sqlalchemy import select, update, func, text
from sqlalchemy.exc import IntegrityError

from db.core import get_session, _model_to_dict
from db.models import User, StarPayment, UserBot, UserBotPref
from db.bots import get_platform_setting
from config import (VIP_PLANS, VIP_FEATURES, MAX_VIP0_USERS, BASIC_LEVEL,
                    FREE_BOT_FILES_LIMIT, BASIC_BOT_FILES_LIMIT)

logger = logging.getLogger(__name__)


async def _get_redis():
    """延迟导入获取 Redis 实例"""
    from redis_manager import get_redis
    return await get_redis()


# ==================== 免费注册运行时设置 ====================
# 存 platform_settings 表，管理员用 /setfree 命令随时切换，无需重启。
# 读取走 Redis 缓存（30s），/setfree 修改时主动失效。

_SETTING_CACHE_TTL = 30


async def is_free_registration_closed() -> bool:
    """免费注册是否已关闭（关闭后新用户需购买基础版 10⭐/月）"""
    r = await _get_redis()
    cached = await r.cache_get("setting:free_registration")
    if cached is not None:
        return cached == "closed"

    value = await get_platform_setting("free_registration", "")
    closed = value == "closed"
    await r.cache_set("setting:free_registration", "closed" if closed else "open", ttl=_SETTING_CACHE_TTL)
    return closed


async def get_max_vip0_users_limit() -> int:
    """免费用户数量上限：优先运行时设置（/setfree limit），未设置回退环境变量 MAX_VIP0_USERS"""
    r = await _get_redis()
    cached = await r.cache_get("setting:max_vip0_users")
    if cached is not None:
        try:
            return int(cached)
        except ValueError:
            pass

    value = await get_platform_setting("max_vip0_users", "")
    try:
        limit = int(value) if value else MAX_VIP0_USERS
    except ValueError:
        limit = MAX_VIP0_USERS
    await r.cache_set("setting:max_vip0_users", str(limit), ttl=_SETTING_CACHE_TTL)
    return limit


async def _get_bot_files_limit(setting_key: str, env_default: int, cache_key: str) -> int:
    """Bot 文件数上限通用读取：运行时设置 > 环境变量默认值（0 = 不限制）"""
    r = await _get_redis()
    cached = await r.cache_get(cache_key)
    if cached is not None:
        try:
            return int(cached)
        except ValueError:
            pass

    value = await get_platform_setting(setting_key, "")
    try:
        limit = int(value) if value else env_default
    except ValueError:
        limit = env_default
    await r.cache_set(cache_key, str(limit), ttl=_SETTING_CACHE_TTL)
    return limit


async def get_free_bot_files_limit() -> int:
    """免费用户单个 Bot 的文件数上限（/setfree files N 调整，默认 20000）"""
    return await _get_bot_files_limit("free_bot_files", FREE_BOT_FILES_LIMIT, "setting:free_bot_files")


async def get_basic_bot_files_limit() -> int:
    """基础版用户单个 Bot 的文件数上限（/setfree basicfiles N 调整，默认 50000）"""
    return await _get_bot_files_limit("basic_bot_files", BASIC_BOT_FILES_LIMIT, "setting:basic_bot_files")


async def invalidate_free_settings_cache() -> None:
    """清除免费注册/名额/文件限额设置缓存（/setfree 修改后调用，立即生效）"""
    r = await _get_redis()
    await r.cache_delete("setting:free_registration")
    await r.cache_delete("setting:max_vip0_users")
    await r.cache_delete("setting:free_bot_files")
    await r.cache_delete("setting:basic_bot_files")


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
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
            # 重新查询以获取完整对象
            result = await session.execute(
                select(User).where(User.user_id == user_id)
            )
            user = result.scalar_one()

        return _model_to_dict(user)


async def get_user_vip_level(user_id: int) -> int:
    """Compute expiry from current state; no stale entitlement cache past expiry."""
    info = await get_user_vip_info(user_id)
    if not info['is_active'] and info['vip_level'] != BASIC_LEVEL:
        async with get_session() as session:
            from services.notices import enqueue
            await enqueue(f"expired:{user_id}:{info['vip_expire_at']}", user_id,
                          '⚠️ 你的会员已到期，使用 /vip 续费。因会员额度暂停的 Bot 将在续费后恢复。', session)
            await session.execute(update(User).where(User.user_id == user_id, User.vip_level == info['vip_level'],
                User.vip_expire_at == info['vip_expire_at']).values(vip_level=0, vip_expire_at=None))
            await session.commit()
    # Keep BASIC_LEVEL for expired basic accounts (registration identity).
    return info['vip_level'] if info['vip_level'] == BASIC_LEVEL else info['effective_level']


async def user_can_use_backup(user_id: int) -> bool:
    """级联备份是 VIP 2/3 专属特权（按 VIP_FEATURES 判定，管理员另行放行）"""
    level = await get_user_vip_level(user_id)
    return bool(VIP_FEATURES.get(level, {}).get('backup'))


async def get_user_vip_info(user_id: int) -> Dict:
    from services.membership import get_plan
    user = await get_or_create_user(user_id)
    level = user.get('vip_level', 0)
    expire_at = user.get('vip_expire_at')
    active = True
    remaining = 0
    if level > 0 and expire_at:
        try:
            expiry = datetime.strptime(expire_at, "%Y-%m-%d %H:%M:%S")
            active = expiry > datetime.now()
            remaining = max(0, (expiry-datetime.now()).days)
        except ValueError:
            active = False
    effective = level if active else 0
    plan = await get_plan(level)
    effective_plan = await get_plan(effective)
    maximum = effective_plan['max_bots']
    if level == BASIC_LEVEL and not active:
        maximum = 0
    return dict(user_id=user_id, vip_level=level, effective_level=effective,
                vip_name=plan['name'], max_bots=maximum, vip_expire_at=expire_at,
                is_active=active, remaining_days=remaining)


async def get_max_bots_for_user(user_id: int) -> int:
    return (await get_user_vip_info(user_id))['max_bots']


async def update_user_vip(user_id: int, level: int, months: int) -> bool:
    """升级/续费用户VIP，时间叠加"""
    if level not in VIP_PLANS or months < 0 or months > 120:
        return False
    async with get_session() as session:
        try:
            result = await session.execute(
                select(User).where(User.user_id == user_id).with_for_update()
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
            user.vip_expire_at = None if level == 0 or months == 0 else new_expire.strftime("%Y-%m-%d %H:%M:%S")
            # 新授予的会员可再迁移（清除「迁移收到」一次性标记）
            user.vip_migrated = 0

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


async def get_all_vip_users() -> List[Dict]:
    """获取所有付费会员用户（vip_level>0，含基础版与已过期），按等级降序"""
    async with get_session() as session:
        result = await session.execute(
            select(User).where(User.vip_level > 0)
            .order_by(User.vip_level.desc(), User.created_at)
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
    """获取当前免费（VIP 0）用户数量，口径：
    - 按用户去重（不是按 Bot 数量）
    - 只统计名下有运行中（active）Bot 的用户——暂停/管理员停止/封禁/删除的 Bot 不占名额
    - VIP / 基础版用户的 Bot 不占免费名额
    """
    async with get_session() as session:
        result = await session.execute(
            select(func.count(func.distinct(UserBot.owner_id)))
            .select_from(UserBot)
            .outerjoin(User, UserBot.owner_id == User.user_id)
            .where(
                UserBot.status == 'active',
                func.coalesce(User.vip_level, 0) == 0,
            )
        )
        return result.scalar() or 0


async def check_vip0_capacity(user_id: int) -> bool:
    """检查用户是否还能免费创建 Bot（统一闸门：免费注册开关 + 名额上限）
    返回 True 表示可以创建，False 表示被拦
    规则：
    - VIP / 基础版用户不受限制（基础版过期由 max_bots=0 挡住）
    - 已有 Bot 的存量免费用户不受限制（老用户保留）
    - 免费注册关闭后，拦住无 Bot 的新用户
    - 免费注册开放时，受免费用户数量上限限制（运行时设置 /setfree limit，回退 env）
    """
    level = await get_user_vip_level(user_id)
    if level > 0:
        return True  # VIP / 基础版用户不受此限制

    # 已有 Bot 的用户不受限制（已占用名额，允许在其 max_bots 内继续操作）
    existing_bots = await get_active_bots_count_by_owner(user_id)
    if existing_bots > 0:
        return True

    # 免费注册已关闭：新用户需购买基础版（10⭐/月）或 VIP
    if await is_free_registration_closed():
        return False

    limit = await get_max_vip0_users_limit()
    if limit <= 0:
        return True  # 不限制

    count = await get_vip0_user_count()
    return count < limit


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
# 容量上限：key 数只增不减会随 用户×Bot 组合无限增长（慢性内存泄漏），
# 超限时先清过期项，仍超限则按写入时间丢最旧的一半
_CACHE_MAX = 20000


def _cache_get(cache: dict, key):
    """从缓存读取，过期返回 None"""
    entry = cache.get(key)
    if entry and time.time() - entry[1] < _CACHE_TTL:
        return entry[0]
    return None


def _cache_set(cache: dict, key, value):
    """写入缓存（带容量保护）"""
    if len(cache) >= _CACHE_MAX:
        now = time.time()
        expired = [k for k, (_, ts) in cache.items() if now - ts >= _CACHE_TTL]
        for k in expired:
            del cache[k]
        if len(cache) >= _CACHE_MAX:
            for k in sorted(cache, key=lambda k: cache[k][1])[:_CACHE_MAX // 2]:
                del cache[k]
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
            for k in [k for k in _user_pref_cache if k[1] == bot_db_id]:
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


# ===== 自动删除相关函数 =====

_auto_delete_cache: dict = {}  # {bot_db_id: (seconds, timestamp)}


async def get_bot_auto_delete(bot_db_id: int) -> int:
    """获取 Bot 的自动删除延迟秒数（带缓存），0=不删除"""
    cached = _cache_get(_auto_delete_cache, bot_db_id)
    if cached is not None:
        return cached

    async with get_session() as session:
        result = await session.execute(
            select(UserBot.auto_delete).where(UserBot.id == bot_db_id)
        )
        seconds = result.scalar_one_or_none()
        seconds = seconds if seconds is not None else 0
    _cache_set(_auto_delete_cache, bot_db_id, seconds)
    return seconds


async def set_bot_auto_delete(bot_db_id: int, seconds: int) -> bool:
    """设置 Bot 的自动删除延迟秒数并更新缓存"""
    async with get_session() as session:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await session.execute(
                update(UserBot)
                .where(UserBot.id == bot_db_id)
                .values(auto_delete=seconds, updated_at=now)
            )
            await session.commit()
            _cache_set(_auto_delete_cache, bot_db_id, seconds)
            logger.info("Bot %s 自动删除设置为 %d 秒", bot_db_id, seconds)
            return True
        except Exception as e:
            logger.error("设置自动删除失败: %s", e)
            return False
