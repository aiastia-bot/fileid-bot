"""基础版（10⭐/月）+ 免费注册开关 + 免费用户名额 的 DB 层测试

不依赖真实 Telegram / Redis，使用临时 SQLite：
- 用户状态机：免费 → 购买基础版(level 4) → 过期不降级 + max_bots=0 → 续费恢复
- check_vip0_capacity 统一闸门：免费开放不限 / 运行时限额 / 免费关闭拦新用户 / 存量放行
- 运行时设置读取（platform_settings）与缓存失效

运行：
    python -m pytest tests/test_free_tier.py -v
或直接：
    python tests/test_free_tier.py
"""
import asyncio
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

# 让脚本能直接 import 项目根目录的模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ===== 在 import 任何项目模块前，配置临时数据库 =====
# config.py 在 import 时读取环境变量，必须提前设置
_DB_FD, _DB_PATH = tempfile.mkstemp(prefix='fileid_test_free_', suffix='.db')
os.close(_DB_FD)
os.unlink(_DB_PATH)  # 留空文件路径，让引擎自己创建
os.environ['DB_TYPE'] = 'sqlite'
os.environ['DB_PATH'] = _DB_PATH
os.environ['REDIS_URL'] = ''  # 无 Redis，走内存降级
os.environ['MAX_VIP0_USERS'] = '2'  # 回退默认值（可被运行时设置覆盖）

from config import VIP_PLANS, MAX_VIP0_USERS, BASIC_LEVEL  # noqa: E402
from db.core import init_db, get_session  # noqa: E402
from db.models import User, UserBot, PlatformSetting  # noqa: E402
from db.bots import set_platform_setting  # noqa: E402
from db.vip import (  # noqa: E402
    get_or_create_user, update_user_vip, get_user_vip_level,
    get_user_vip_info, get_max_bots_for_user, check_vip0_capacity,
    get_vip0_user_count, is_free_registration_closed,
    get_max_vip0_users_limit, invalidate_free_settings_cache,
)


async def _init():
    await init_db()


async def _set_user(user_id: int, level: int, expire_at=None):
    """直接写用户表，构造任意 VIP 状态"""
    async with get_session() as session:
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.user_id == user_id))
        user = result.scalar_one_or_none()
        if user:
            user.vip_level = level
            user.vip_expire_at = expire_at
        else:
            session.add(User(user_id=user_id, vip_level=level,
                             vip_expire_at=expire_at,
                             created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        await session.commit()


async def _add_bot(owner_id: int, bot_id: int, status: str = 'active'):
    """直接插入一条 Bot 记录"""
    async with get_session() as session:
        session.add(UserBot(
            owner_id=owner_id,
            bot_token=f'token_{owner_id}_{bot_id}',
            bot_id=bot_id,
            bot_username=f'test{owner_id}_{bot_id}bot',
            bot_firstname='TestBot',
            status=status,
            created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        ))
        await session.commit()


async def _clear_settings():
    async with get_session() as session:
        from sqlalchemy import delete
        await session.execute(delete(PlatformSetting))
        await session.commit()
    await invalidate_free_settings_cache()


async def _reset_data():
    """每个测试前清空 users / user_bots，保证用例隔离（用户 ID 仍各自独立，避免内存缓存串扰）"""
    async with get_session() as session:
        from sqlalchemy import delete
        await session.execute(delete(UserBot))
        await session.execute(delete(User))
        await session.commit()
    await _clear_settings()


class TestBasicPlanLifecycle(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _init()
        await _reset_data()

    async def test_purchase_basic(self):
        """购买基础版 → level 4、有效期约 30 天、max_bots 与免费一致"""
        uid = 1001
        await get_or_create_user(uid)
        ok = await update_user_vip(uid, BASIC_LEVEL, 1)
        self.assertTrue(ok)

        self.assertEqual(await get_user_vip_level(uid), BASIC_LEVEL)
        self.assertEqual(await get_max_bots_for_user(uid), VIP_PLANS[BASIC_LEVEL]['max_bots'])

        info = await get_user_vip_info(uid)
        self.assertTrue(info['is_active'])
        self.assertEqual(info['vip_name'], '基础版')
        expire = datetime.strptime(info['vip_expire_at'], "%Y-%m-%d %H:%M:%S")
        self.assertAlmostEqual(
            (expire - datetime.now()).total_seconds(),
            timedelta(days=30).total_seconds(), delta=60)

    async def test_expired_basic_not_downgraded(self):
        """基础版过期：保持 level 4（不降级），max_bots=0，is_active=False"""
        uid = 1002
        past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        await _set_user(uid, BASIC_LEVEL, past)

        # 惰性检查不降级
        self.assertEqual(await get_user_vip_level(uid), BASIC_LEVEL)
        # 挡住创建/恢复
        self.assertEqual(await get_max_bots_for_user(uid), 0)
        info = await get_user_vip_info(uid)
        self.assertFalse(info['is_active'])
        self.assertEqual(info['vip_name'], '基础版')

    async def test_renewal_after_expiry(self):
        """过期后续费：从当前时间起算（不叠加过去的过期时间），权益恢复"""
        uid = 1003
        past = (datetime.now() - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
        await _set_user(uid, BASIC_LEVEL, past)

        await get_or_create_user(uid)

        ok = await update_user_vip(uid, BASIC_LEVEL, 1)
        self.assertTrue(ok)
        self.assertEqual(await get_max_bots_for_user(uid), VIP_PLANS[BASIC_LEVEL]['max_bots'])

        info = await get_user_vip_info(uid)
        self.assertTrue(info['is_active'])
        expire = datetime.strptime(info['vip_expire_at'], "%Y-%m-%d %H:%M:%S")
        self.assertAlmostEqual(
            (expire - datetime.now()).total_seconds(),
            timedelta(days=30).total_seconds(), delta=60)

    async def test_vip1_expiry_still_downgrades(self):
        """回归：VIP 过期仍降级回 0（原有行为不受影响）"""
        uid = 1004
        past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        await _set_user(uid, 1, past)

        self.assertEqual(await get_user_vip_level(uid), 0)
        self.assertEqual(await get_max_bots_for_user(uid), VIP_PLANS[0]['max_bots'])

        # 降级后 users 表里 expire 被清空
        user = await get_or_create_user(uid)
        self.assertIsNone(user['vip_expire_at'])

    async def test_expired_vips_job_shrinks_candidate_set(self):
        """到期处理任务：通知与降级只做一次，不随 30s 周期反复空跑"""
        from sqlalchemy import delete, select
        from db.models import UserNotice
        from handlers.master.stars import handle_expired_vips, _expired_handled

        basic, vip1 = 1005, 1006
        past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        await _set_user(basic, BASIC_LEVEL, past)   # 基础版过期：不降级，永久留候选集
        await _set_user(vip1, 1, past)              # VIP1 过期：降级出集

        async with get_session() as session:
            await session.execute(delete(UserNotice))
            await session.commit()

        _expired_handled.clear()
        await handle_expired_vips()
        async with get_session() as session:
            notices = (await session.execute(
                select(UserNotice).where(UserNotice.key.like('expired:%')))).scalars().all()
        self.assertEqual({n.user_id for n in notices}, {basic, vip1})
        # VIP1 已降级出集；基础版保留注册身份
        self.assertEqual((await get_or_create_user(vip1))['vip_level'], 0)
        self.assertEqual((await get_or_create_user(basic))['vip_level'], BASIC_LEVEL)

        # 第二轮：候选集已收缩（内存标记），不产生新通知、不再触碰用户
        await handle_expired_vips()
        async with get_session() as session:
            notices2 = (await session.execute(
                select(UserNotice).where(UserNotice.key.like('expired:%')))).scalars().all()
        self.assertEqual(len(notices2), len(notices), '已处理的过期用户不再重复入队')


class TestCapacityGate(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        await _init()
        await _reset_data()

    async def test_open_unlimited_by_default(self):
        """免费开放 + 无限额（清除设置后回退 env=0 表示不限）——先覆盖 env 再清除"""
        await set_platform_setting('max_vip0_users', '0')
        await invalidate_free_settings_cache()
        self.assertFalse(await is_free_registration_closed())
        self.assertEqual(await get_max_vip0_users_limit(), 0)
        self.assertTrue(await check_vip0_capacity(2001))

    async def test_runtime_limit_blocks_new_free_users(self):
        """运行时限额：免费名额占满后拦新用户；VIP/基础版/存量不受限"""
        await set_platform_setting('max_vip0_users', '1')
        await invalidate_free_settings_cache()
        self.assertEqual(await get_max_vip0_users_limit(), 1)

        free_a, newcomer, vip_u, basic_u = 2010, 2011, 2012, 2013
        await _add_bot(free_a, 1)  # 免费名额已占 1/1
        self.assertEqual(await get_vip0_user_count(), 1)

        # 新免费用户被拦
        self.assertFalse(await check_vip0_capacity(newcomer))
        # 存量免费用户（已有 Bot）放行
        self.assertTrue(await check_vip0_capacity(free_a))
        # VIP 用户不受限
        await get_or_create_user(vip_u)
        await update_user_vip(vip_u, 2, 1)
        self.assertTrue(await check_vip0_capacity(vip_u))
        # 基础版用户不受限
        await get_or_create_user(basic_u)
        await update_user_vip(basic_u, BASIC_LEVEL, 1)
        self.assertTrue(await check_vip0_capacity(basic_u))

    async def test_limit_falls_back_to_env(self):
        """未设置运行时限额时回退环境变量 MAX_VIP0_USERS"""
        self.assertEqual(await get_max_vip0_users_limit(), MAX_VIP0_USERS)
        await set_platform_setting('max_vip0_users', '7')
        await invalidate_free_settings_cache()
        self.assertEqual(await get_max_vip0_users_limit(), 7)

    async def test_closed_blocks_new_users_only(self):
        """免费注册关闭：拦无 Bot 的新用户；存量免费用户和付费用户放行"""
        await set_platform_setting('free_registration', 'closed')
        await invalidate_free_settings_cache()
        self.assertTrue(await is_free_registration_closed())

        newcomer, legacy_free, vip_u, basic_u = 2020, 2021, 2022, 2023
        await _add_bot(legacy_free, 1)
        await get_or_create_user(vip_u)
        await update_user_vip(vip_u, 1, 1)
        await get_or_create_user(basic_u)
        await update_user_vip(basic_u, BASIC_LEVEL, 1)

        self.assertFalse(await check_vip0_capacity(newcomer))
        self.assertTrue(await check_vip0_capacity(legacy_free))
        self.assertTrue(await check_vip0_capacity(vip_u))
        self.assertTrue(await check_vip0_capacity(basic_u))

        # 关闭状态下不限额生效（名额逻辑被关闭分支短路）
        await set_platform_setting('max_vip0_users', '0')
        await invalidate_free_settings_cache()
        self.assertFalse(await check_vip0_capacity(newcomer))

    async def test_expired_basic_blocked_by_max_bots(self):
        """过期基础版：闸门放行但 max_bots=0，由 max_bots 检查挡住"""
        uid = 2030
        past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        await _set_user(uid, BASIC_LEVEL, past)

        self.assertTrue(await check_vip0_capacity(uid))
        self.assertEqual(await get_max_bots_for_user(uid), 0)

    async def test_vip0_count_excludes_paid_users(self):
        """免费名额统计口径：VIP/基础版用户的 Bot 不占免费名额"""
        free_u, vip_u, basic_u, expired_basic = 2040, 2041, 2042, 2043
        await _add_bot(free_u, 1)
        await get_or_create_user(vip_u)
        await update_user_vip(vip_u, 3, 1)
        await _add_bot(vip_u, 1)
        await get_or_create_user(basic_u)
        await update_user_vip(basic_u, BASIC_LEVEL, 1)
        await _add_bot(basic_u, 1)
        past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        await _set_user(expired_basic, BASIC_LEVEL, past)
        await _add_bot(expired_basic, 1)

        self.assertEqual(await get_vip0_user_count(), 1)  # 只有 free_u

    async def test_vip0_count_only_running_bots(self):
        """免费名额口径：只有运行中（active）的 Bot 才占名额，暂停/管理员停止的不占"""
        running_u, paused_u, stopped_u = 2060, 2061, 2062
        await _add_bot(running_u, 1, status='active')
        await _add_bot(paused_u, 1, status='paused')
        await _add_bot(stopped_u, 1, status='admin_stopped')

        self.assertEqual(await get_vip0_user_count(), 1)  # 只有 running_u 占名额

        # 暂停的 Bot 恢复运行后开始占名额
        async with get_session() as session:
            from sqlalchemy import update as sa_update
            await session.execute(
                sa_update(UserBot).where(UserBot.owner_id == paused_u).values(status='active'))
            await session.commit()
        self.assertEqual(await get_vip0_user_count(), 2)

    async def test_settings_cache_invalidated(self):
        """修改设置后缓存立即失效，开关即时生效"""
        await set_platform_setting('free_registration', 'closed')
        await invalidate_free_settings_cache()
        self.assertTrue(await is_free_registration_closed())

        await set_platform_setting('free_registration', 'open')
        await invalidate_free_settings_cache()
        self.assertFalse(await is_free_registration_closed())
        self.assertTrue(await check_vip0_capacity(2050))


if __name__ == '__main__':
    unittest.main(verbosity=2)
