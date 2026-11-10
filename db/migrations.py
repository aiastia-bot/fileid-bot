"""VIP 会员迁移码数据访问层

规则：
- 码有效期 = 源会员剩余有效期（领取时校验源会员仍有效）
- 一次性：领取后码置 used；接收方被标记 users.vip_migrated=1，不能再生成迁移码
  （通过购买/续费/admin 设置获得的新会员会重置该标记）
- 每个用户同时只有一个待领取码：生成新码时自动作废旧码
"""
import logging
import secrets
import string
from datetime import datetime
from typing import Optional, Dict, Tuple

from sqlalchemy import select, update

from db.core import get_session, _model_to_dict
from db.models import VipMigration, User

logger = logging.getLogger(__name__)

# 迁移码长度（不含 mig_ 前缀）
_CODE_LEN = 10
# 码字符集（URL 友好，与礼物领取码一致）
_CODE_ALPHABET = string.ascii_lowercase + string.digits


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _gen_code() -> str:
    """生成 mig_<10位随机码>，碰撞概率极低（36^10 ≈ 3.6e15）"""
    body = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LEN))
    return f"mig_{body}"


def _is_active_member(user) -> bool:
    """User 行是否有有效会员（等级>0 且未过期；无到期时间视为永久）"""
    if not user or (user.vip_level or 0) <= 0:
        return False
    if not user.vip_expire_at:
        return True  # 永久会员
    try:
        return datetime.now() <= datetime.strptime(user.vip_expire_at, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False


async def create_migration(from_user_id: int, vip_level: int, vip_expire_at: Optional[str]) -> Optional[Dict]:
    """生成迁移码（作废该用户旧的待领取码），返回记录 dict"""
    async with get_session() as session:
        try:
            # 作废旧待领取码：一人同时只保留一个有效码
            await session.execute(
                update(VipMigration)
                .where(VipMigration.from_user_id == from_user_id,
                       VipMigration.status == 'pending')
                .values(status='cancelled')
            )

            # 防碰撞：最多重试 5 次
            for _ in range(5):
                code = _gen_code()
                exists = await session.execute(
                    select(VipMigration).where(VipMigration.code == code)
                )
                if exists.scalars().first() is None:
                    break
            else:
                logger.error("生成迁移码碰撞 5 次，放弃")
                return None

            mig = VipMigration(
                code=code,
                from_user_id=from_user_id,
                vip_level=vip_level,
                vip_expire_at=vip_expire_at,
                status='pending',
                created_at=_now(),
            )
            session.add(mig)
            await session.commit()
            logger.info("生成迁移码 %s (from=%s, level=%d)", code, from_user_id, vip_level)
            return _model_to_dict(mig)
        except Exception as e:
            logger.error("创建迁移码失败: %s", e, exc_info=True)
            return None


async def get_migration(code: str) -> Optional[Dict]:
    """按 code 查询迁移码记录"""
    async with get_session() as session:
        result = await session.execute(
            select(VipMigration).where(VipMigration.code == code)
        )
        mig = result.scalars().first()
        return _model_to_dict(mig) if mig else None


async def claim_migration(code: str, to_user_id: int) -> Tuple[str, Optional[Dict]]:
    """领取迁移码：把源账号当前有效会员原样搬运到 to_user_id（剩余时长不变，不叠加）。

    单事务 + 行锁完成校验与搬运，返回 (状态码, 详情)：
      ok               成功，详情含 from_user/to_user/level/expire_at
      invalid          码不存在 / 已使用 / 已作废
      expired          源会员已过期或已失效
      self             不能迁移给自己
      target_has_vip   目标账号已有有效会员
    """
    async with get_session() as session:
        try:
            # Reserve the code with a conditional write before reading source
            # state. FOR UPDATE alone does not lock SQLite reads.
            reserved = await session.execute(update(VipMigration).where(
                VipMigration.code == code, VipMigration.status == 'pending'
            ).values(status='claiming'))
            if not reserved.rowcount:
                return 'invalid', None
            # This write is rolled back on every rejected claim.
            result = await session.execute(
                select(VipMigration)
                .where(VipMigration.code == code)
                .with_for_update()
            )
            mig = result.scalars().first()
            if not mig or mig.status != 'claiming':
                return 'invalid', None

            # 源账号会员必须仍然有效（领取时以实际状态为准，不信任快照）
            res_a = await session.execute(
                select(User).where(User.user_id == mig.from_user_id).with_for_update()
            )
            user_a = res_a.scalars().first()
            if not _is_active_member(user_a) or user_a.vip_migrated:
                return 'expired', None

            if to_user_id == mig.from_user_id:
                return 'self', None

            # 目标账号不能已有有效会员
            res_b = await session.execute(
                select(User).where(User.user_id == to_user_id).with_for_update()
            )
            user_b = res_b.scalars().first()
            if not user_b:
                user_b = User(user_id=to_user_id, vip_level=0, vip_expire_at=None,
                              created_at=_now())
                session.add(user_b)
            if _is_active_member(user_b):
                return 'target_has_vip', None

            # 搬运：B 拿到 A 的等级+到期（原样），B 标记不可再迁移；A 清零回免费
            user_b.vip_level = user_a.vip_level
            user_b.vip_expire_at = user_a.vip_expire_at
            user_b.vip_migrated = 1
            user_a.vip_level = 0
            user_a.vip_expire_at = None

            mig.status = 'used'
            mig.to_user_id = to_user_id
            mig.used_at = _now()

            await session.commit()

            # 清理双方 VIP 缓存
            from db.vip import _get_redis
            r = await _get_redis()
            await r.cache_delete(f"vip_level:{mig.from_user_id}")
            await r.cache_delete(f"vip_info:{mig.from_user_id}")
            await r.cache_delete(f"vip_level:{to_user_id}")
            await r.cache_delete(f"vip_info:{to_user_id}")

            info = {
                'from_user_id': mig.from_user_id,
                'to_user_id': to_user_id,
                'vip_level': user_b.vip_level,
                'vip_expire_at': user_b.vip_expire_at,
            }
            logger.info("迁移码 %s 领取成功: %s -> %s (level=%d)",
                        code, mig.from_user_id, to_user_id, user_b.vip_level)
            return 'ok', info
        except Exception as e:
            logger.error("领取迁移码 %s 失败: %s", code, e, exc_info=True)
            return 'invalid', None
