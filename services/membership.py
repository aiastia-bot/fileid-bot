"""Runtime plan catalogue and audited membership changes."""

import json
import logging
import secrets
import string
from datetime import datetime, timedelta
from sqlalchemy import or_, select, update as sa_update
from db.core import get_session
from db.models import User, PlatformSetting, MembershipAudit, VipRedeemCode
from config import (
    VIP_PLANS,
    VIP_FEATURES,
    BASIC_LEVEL,
    FREE_BOT_FILES_LIMIT,
    BASIC_BOT_FILES_LIMIT,
)

logger = logging.getLogger(__name__)

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


def downgrade_blocked(vip_info: dict, target_level: int) -> bool:
    """会员有效期内禁止购买更低等级（同等级续费与升级不受限）。

    等级高低按 PLAN_RANK 比较：基础版(4) < VIP1 < VIP2 < VIP3。
    """
    if not vip_info or not vip_info.get('is_active'):
        return False
    return PLAN_RANK.get(target_level, 0) < PLAN_RANK.get(vip_info.get('vip_level'), 0)


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


# ==================== 会员兑换码 ====================
# 管理员 /vipadmin gen 批量生成 → 用户 /redeem 兑换。一次性，可按批次作废。
# 兑换走 adjust_member（写审计、同等级续费叠加、跨等级开新周期、同步额度）。

_REDEEM_CODE_LEN = 12
_REDEEM_ALPHABET = string.ascii_lowercase + string.digits
REDEEM_MAX_BATCH = 50


class _PermanentMemberError(Exception):
    """永久会员尝试兑换，码位不消耗"""


class _DowngradeRedeemError(Exception):
    """高等级会员尝试兑换低等级码，码位不消耗"""


def _new_redeem_code() -> str:
    body = "".join(secrets.choice(_REDEEM_ALPHABET) for _ in range(_REDEEM_CODE_LEN))
    return f"vip_{body}"


async def generate_redeem_codes(actor_id, level, months, count, expires_days=None,
                                recipients=None):
    """批量生成兑换码，返回 (codes, batch)。months=0 表示永久会员。

    recipients 为定向赠码的收件人列表（与 count 等长、去重、均为正整数），
    写入 recipient_uid 供面板防重复发送与审计；普通生成传 None。
    """
    if level not in VIP_PLANS or level <= 0:
        raise ValueError("等级无效（1-4）")
    if not 0 <= months <= 120:
        raise ValueError("月份必须为 0-120，0 表示永久")
    if not 1 <= count <= REDEEM_MAX_BATCH:
        raise ValueError(f"单次生成数量为 1-{REDEEM_MAX_BATCH}")
    if expires_days is not None and expires_days < 1:
        raise ValueError("码有效期必须为正整数天")
    if recipients is not None:
        if (not isinstance(recipients, list) or len(recipients) != count
                or len(set(recipients)) != count
                or any(not isinstance(r, int) or r <= 0 for r in recipients)):
            raise ValueError("recipients 参数无效")
    batch = datetime.now().strftime("%m%d%H%M%S") + ''.join(
        secrets.choice(_REDEEM_ALPHABET) for _ in range(4))
    expires_at = (
        (datetime.now() + timedelta(days=expires_days)).strftime("%Y-%m-%d %H:%M:%S")
        if expires_days
        else None
    )
    codes = []
    async with get_session() as session:
        for index in range(count):
            for _attempt in range(5):  # 防碰撞重试
                code = _new_redeem_code()
                exists = await session.execute(
                    select(VipRedeemCode.id).where(VipRedeemCode.code == code)
                )
                if exists.scalar() is None:
                    break
            else:
                raise ValueError("生成兑换码碰撞 5 次，请重试")
            session.add(VipRedeemCode(
                code=code, vip_level=level, months=months, batch=batch,
                status='pending', created_by=actor_id, created_at=now_text(),
                expires_at=expires_at,
                recipient_uid=recipients[index] if recipients else None,
            ))
            codes.append(code)
        session.add(MembershipAudit(
            actor_id=actor_id, user_id=0, action='redeem:gen',
            before='', after=json.dumps(
                {'batch': batch, 'level': level, 'months': months,
                 'count': len(codes), 'expires_at': expires_at}),
            reason=f"生成兑换码批次 {batch}", created_at=now_text(),
        ))
        await session.commit()
    return codes, batch


async def redeem_code(user_id, code):
    """兑换会员，返回 (状态码, 详情)。

      ok          兑换成功，详情含等级/时长/兑换后的会员信息
      invalid     码不存在
      used        已被兑换
      cancelled   已作废
      expired     超过码有效期
      permanent   永久会员无需兑换（不烧码）
      downgrade   当前等级更高，低等级码不可用（不烧码）
    条件 UPDATE 原子占位：并发兑换同一码时只有一个用户成功。
    发放语义：同等级时长叠加；升级码从当前到期日起算（剩余时长并入新等级，
    一分钟不浪费）；低等级码对有效期内的高等级会员拒兑；months=0 直接开通永久。
    """
    code = str(code).strip().lower()
    if not code:
        return 'invalid', None
    async with get_session() as session:
        claimed = await session.execute(
            sa_update(VipRedeemCode)
            .where(
                VipRedeemCode.code == code,
                VipRedeemCode.status == 'pending',
                or_(VipRedeemCode.expires_at.is_(None),
                    VipRedeemCode.expires_at > now_text()),
            )
            .values(status='used', redeemed_by=user_id, redeemed_at=now_text())
        )
        await session.commit()
        if not claimed.rowcount:
            row = (await session.execute(
                select(VipRedeemCode).where(VipRedeemCode.code == code)
            )).scalars().first()
            if not row:
                return 'invalid', None
            if row.status == 'used':
                return 'used', None
            if row.status == 'cancelled':
                return 'cancelled', None
            return 'expired', None
        record = (await session.execute(
            select(VipRedeemCode).where(VipRedeemCode.code == code)
        )).scalars().first()

    try:
        async with get_session() as session:
            user = (await session.execute(
                select(User).where(User.user_id == user_id).with_for_update()
            )).scalars().first()
            if not user:
                user = User(user_id=user_id, vip_level=0, vip_expire_at=None,
                            vip_migrated=0, created_at=now_text())
                session.add(user)
            if user.vip_level > 0 and user.vip_expire_at is None:
                raise _PermanentMemberError()  # 永久会员无需兑换
            now = datetime.now()
            active = user.vip_level > 0 and (
                user.vip_expire_at is None or
                datetime.strptime(user.vip_expire_at, '%Y-%m-%d %H:%M:%S') > now)
            if active and PLAN_RANK.get(record.vip_level, 0) < PLAN_RANK.get(user.vip_level, 0):
                raise _DowngradeRedeemError()  # 降级码拒兑，码位不消耗
            before = {"level": user.vip_level, "expires": user.vip_expire_at}
            if record.months == 0:
                user.vip_level = record.vip_level
                user.vip_expire_at = None
                user.vip_migrated = 0
            elif active and PLAN_RANK.get(record.vip_level, 0) > PLAN_RANK.get(user.vip_level, 0):
                # 升级码：剩余时长自动并入新等级（从当前到期日起算，一分钟不浪费）
                base = max(now, datetime.strptime(user.vip_expire_at, '%Y-%m-%d %H:%M:%S'))
                user.vip_level = record.vip_level
                user.vip_migrated = 0
                user.vip_expire_at = (base + timedelta(
                    days=30 * record.months)).strftime('%Y-%m-%d %H:%M:%S')
            else:
                grant(user, record.vip_level, record.months)
            after = {"level": user.vip_level, "expires": user.vip_expire_at}
            session.add(MembershipAudit(
                actor_id=user_id, user_id=user_id, action='redeem:apply',
                before=json.dumps(before), after=json.dumps(after),
                reason=f"兑换码 {code}", created_at=now_text(),
            ))
            await session.commit()
    except _PermanentMemberError:
        await _unclaim_redeem(code, user_id)
        return 'permanent', None
    except _DowngradeRedeemError:
        await _unclaim_redeem(code, user_id)
        return 'downgrade', None
    except Exception:
        await _unclaim_redeem(code, user_id)
        raise
    await invalidate_user(user_id)

    from services.entitlements import reconcile_user

    try:
        await reconcile_user(user_id)
    except Exception:
        logger.exception("兑换码兑换后同步 Bot 额度失败（会员已生效）: user=%s", user_id)
    from db.vip import get_user_vip_info

    return 'ok', {'code': code, 'level': record.vip_level,
                  'months': record.months, 'after': after,
                  'info': await get_user_vip_info(user_id)}


async def _unclaim_redeem(code, user_id):
    """发放失败时退还码位，避免临时故障白烧一个码"""
    async with get_session() as session:
        await session.execute(
            sa_update(VipRedeemCode)
            .where(VipRedeemCode.code == code, VipRedeemCode.status == 'used',
                   VipRedeemCode.redeemed_by == user_id)
            .values(status='pending', redeemed_by=None, redeemed_at=None))
        await session.commit()


async def cancel_redeem_codes(actor_id, code_or_batch, reason=''):
    """作废待兑换码；vip_ 开头按单个码，否则按批次。返回作废数量。"""
    code_or_batch = str(code_or_batch).strip().lower()
    target = (VipRedeemCode.code == code_or_batch if code_or_batch.startswith('vip_')
              else VipRedeemCode.batch == code_or_batch)
    async with get_session() as session:
        result = await session.execute(
            sa_update(VipRedeemCode)
            .where(target, VipRedeemCode.status == 'pending')
            .values(status='cancelled'))
        session.add(MembershipAudit(
            actor_id=actor_id, user_id=0, action='redeem:cancel',
            before='', after=code_or_batch,
            reason=(reason or '作废兑换码')[:500], created_at=now_text(),
        ))
        await session.commit()
        return result.rowcount


async def list_redeem_codes(status=None, limit=20):
    """按状态倒序列出最近的兑换码（None = 全部状态）"""
    async with get_session() as session:
        query = select(VipRedeemCode).order_by(VipRedeemCode.id.desc()).limit(limit)
        if status and status != 'all':
            query = query.where(VipRedeemCode.status == status)
        rows = (await session.execute(query)).scalars().all()
        return [{
            'code': r.code, 'vip_level': r.vip_level, 'months': r.months,
            'batch': r.batch, 'status': r.status, 'created_at': r.created_at,
            'expires_at': r.expires_at, 'redeemed_by': r.redeemed_by,
            'redeemed_at': r.redeemed_at, 'recipient_uid': r.recipient_uid,
        } for r in rows]
