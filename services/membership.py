"""Runtime plan catalogue and audited membership changes."""

import json
from datetime import datetime, timedelta
from sqlalchemy import select, update as sa_update
from db.core import get_session
from db.models import User, PlatformSetting, MembershipAudit
from config import (
    VIP_PLANS,
    VIP_FEATURES,
    BASIC_LEVEL,
    FREE_BOT_FILES_LIMIT,
    BASIC_BOT_FILES_LIMIT,
)

PLAN_RANK = {0: 0, BASIC_LEVEL: 1, 1: 2, 2: 3, 3: 4}


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


async def get_plans():
    plans = {}
    for level, default in VIP_PLANS.items():
        plans[level] = dict(
            default,
            enabled=True,
            file_limit=FREE_BOT_FILES_LIMIT
            if level == 0
            else BASIC_BOT_FILES_LIMIT
            if level == BASIC_LEVEL
            else 0,
            forward_mode=VIP_FEATURES[level]["forward_mode"],
            auto_delete=VIP_FEATURES[level]["forward_mode"],
        )
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(PlatformSetting).where(PlatformSetting.key.like("plan:%"))
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            level = int(row.key.split(":")[1])
            if level in plans:
                plans[level].update(json.loads(row.value))
        # Preserve /setfree files and basicfiles as aliases of catalogue limits.
        for key, level in [("free_bot_files", 0), ("basic_bot_files", BASIC_LEVEL)]:
            row = await session.get(PlatformSetting, key)
            if row is not None:
                plans[level]["file_limit"] = int(row.value)
    return plans


async def get_plan(level):
    plans = await get_plans()
    return plans.get(level, plans[0])


async def set_plan(actor_id, level, changes, reason="套餐调整"):
    if level not in VIP_PLANS:
        raise ValueError("等级必须为 0-4")
    before = await get_plan(level)
    allowed = {
        "name",
        "max_bots",
        "monthly_price",
        "yearly_price",
        "file_limit",
        "forward_mode",
        "auto_delete",
        "enabled",
    }
    for key, value in changes.items():
        if key not in allowed:
            raise ValueError(f"不支持的字段：{key}")
        if key == "name":
            if not isinstance(value, str) or not 1 <= len(value) <= 32:
                raise ValueError("名称长度为 1-32 字符")
        elif key in ("forward_mode", "auto_delete", "enabled"):
            if not isinstance(value, bool):
                raise ValueError("功能开关使用 true/false")
        elif type(value) is not int or value < 0 or value > 100000000:
            raise ValueError("额度和价格必须为 0-100000000 的整数")
        if level != 0 and key in ("monthly_price", "yearly_price") and value == 0:
            raise ValueError("付费套餐价格必须大于 0")
        if level == 0 and key in ("monthly_price", "yearly_price") and value != 0:
            raise ValueError("免费套餐价格必须为 0")
    after = {**before, **changes}
    async with get_session() as session:
        row = await session.get(PlatformSetting, f"plan:{level}", with_for_update=True)
        if row:
            row.value = json.dumps(after, ensure_ascii=False)
        else:
            session.add(
                PlatformSetting(
                    key=f"plan:{level}", value=json.dumps(after, ensure_ascii=False)
                )
            )
        if "file_limit" in changes and level in (0, BASIC_LEVEL):
            key = "free_bot_files" if level == 0 else "basic_bot_files"
            setting = await session.get(PlatformSetting, key)
            if setting:
                setting.value = str(changes["file_limit"])
            else:
                session.add(PlatformSetting(key=key, value=str(changes["file_limit"])))
        session.add(
            MembershipAudit(
                actor_id=actor_id,
                user_id=0,
                action=f"plan:{level}",
                before=json.dumps(before, ensure_ascii=False),
                after=json.dumps(after, ensure_ascii=False),
                reason=reason,
                created_at=now_text(),
            )
        )
        await session.commit()
    from db.vip import invalidate_free_settings_cache

    await invalidate_free_settings_cache()
    return after


async def invalidate_user(user_id):
    from db.vip import _get_redis

    r = await _get_redis()
    for key in (f"vip_level:{user_id}", f"vip_info:{user_id}"):
        await r.cache_delete(key)


def grant(user, level, months):
    """Same-tier renewal stacks. Cross-tier changes start a new term.

    This prevents a cheap remaining subscription being converted into a costly
    tier for free. Invoices and the UI explicitly describe the rule.
    """
    now = datetime.now()
    base = now
    if user.vip_level == level and user.vip_expire_at:
        base = max(now, datetime.strptime(user.vip_expire_at, "%Y-%m-%d %H:%M:%S"))
    if user.vip_level == level and user.vip_expire_at is None and user.vip_level > 0:
        raise ValueError("当前为永久会员，无需续费；如需调整请联系管理员")
    user.vip_level = level
    user.vip_expire_at = (base + timedelta(days=30 * months)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    user.vip_migrated = 0


async def adjust_member(
    actor_id,
    user_id,
    *,
    level=None,
    months=None,
    days=None,
    expires=None,
    reason="管理员调整",
):
    if user_id <= 0 or (level is not None and level not in VIP_PLANS):
        raise ValueError("用户 ID 或等级无效")
    if months is not None and not 0 <= months <= 120:
        raise ValueError("月份必须为 0-120，0 表示永久")
    if days is not None and not -36500 <= days <= 36500:
        raise ValueError("天数范围为 -36500 到 36500")
    async with get_session() as session:
        await session.execute(
            sa_update(User)
            .where(User.user_id == user_id)
            .values(vip_level=User.vip_level)
        )
        user = await session.get(User, user_id, with_for_update=True)
        if not user:
            user = User(
                user_id=user_id,
                vip_level=0,
                vip_expire_at=None,
                vip_migrated=0,
                created_at=now_text(),
            )
            session.add(user)
        before = {"level": user.vip_level, "expires": user.vip_expire_at}
        if level is not None:
            user.vip_level = level
            user.vip_migrated = 0
        if user.vip_level == 0:
            if days is not None or expires is not None:
                raise ValueError("免费用户请先设置付费等级")
            user.vip_expire_at = None
        elif months is not None:
            user.vip_expire_at = (
                None
                if months == 0
                else (datetime.now() + timedelta(days=30 * months)).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
            )
        elif days is not None:
            if not user.vip_expire_at:
                raise ValueError("永久会员请先指定到期时间；免费用户请先设置付费等级")
            base = datetime.strptime(user.vip_expire_at, "%Y-%m-%d %H:%M:%S")
            user.vip_expire_at = (base + timedelta(days=days)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        elif expires is not None:
            user.vip_expire_at = datetime.strptime(
                expires, "%Y-%m-%d %H:%M:%S"
            ).strftime("%Y-%m-%d %H:%M:%S")
        after = {"level": user.vip_level, "expires": user.vip_expire_at}
        session.add(
            MembershipAudit(
                actor_id=actor_id,
                user_id=user_id,
                action="member:adjust",
                before=json.dumps(before),
                after=json.dumps(after),
                reason=reason[:500],
                created_at=now_text(),
            )
        )
        await session.commit()
    await invalidate_user(user_id)
    return after


async def member_feature(owner_id, name):
    from db.vip import get_user_vip_info

    info = await get_user_vip_info(owner_id)
    return bool((await get_plan(info["effective_level"])).get(name, False))
