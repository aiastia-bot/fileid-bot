"""管理端会员工具：/vipadmin bots 名下 Bot 统计 + 兑换码生命周期"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from db.core import get_session
from db.models import MembershipAudit, UserBot, VipRedeemCode
from handlers.master import membership_admin
from services import membership


async def test_generate_and_redeem_lifecycle(database):
    codes, batch = await membership.generate_redeem_codes(99, 3, 6, 3)
    assert len(codes) == len(set(codes)) == 3
    assert all(code.startswith('vip_') for code in codes)

    async with get_session() as session:
        rows = (await session.execute(
            select(VipRedeemCode).where(VipRedeemCode.batch == batch))).scalars().all()
    assert len(rows) == 3 and all(r.status == 'pending' for r in rows)

    user = 8177075430
    status, detail = await membership.redeem_code(user, codes[0])
    assert status == 'ok'
    assert (detail['level'], detail['months']) == (3, 6)
    first_expiry = datetime.strptime(detail['info']['vip_expire_at'], '%Y-%m-%d %H:%M:%S')
    assert abs((first_expiry - datetime.now()).days - 180) <= 2

    # 同等级续费叠加；跨码重复使用被拒；无效码不存在；大小写不敏感
    status, detail = await membership.redeem_code(user, codes[1])
    assert status == 'ok'
    second_expiry = datetime.strptime(detail['info']['vip_expire_at'], '%Y-%m-%d %H:%M:%S')
    assert abs((second_expiry - first_expiry).days - 180) <= 2
    status, _ = await membership.redeem_code(user, codes[0])
    assert status == 'used'
    status, _ = await membership.redeem_code(user, 'vip_missing0000')
    assert status == 'invalid'
    status, detail = await membership.redeem_code(user, codes[2].upper())
    assert status == 'ok'
    third_expiry = datetime.strptime(detail['info']['vip_expire_at'], '%Y-%m-%d %H:%M:%S')
    assert abs((third_expiry - second_expiry).days - 180) <= 2

    # 永久会员兑换不烧码，码仍是 pending
    await membership.adjust_member(99, user, level=3, months=0)  # 开通永久
    codes, _ = await membership.generate_redeem_codes(99, 2, 1, 1)
    status, _ = await membership.redeem_code(user, codes[0])
    assert status == 'permanent'
    async with get_session() as session:
        row = (await session.execute(
            select(VipRedeemCode).where(VipRedeemCode.code == codes[0]))).scalar_one()
    assert row.status == 'pending'
    status, _ = await membership.redeem_code(778, codes[0])
    assert status == 'ok'  # 别人仍可正常使用

    async with get_session() as session:
        audits = (await session.execute(select(MembershipAudit).where(
            MembershipAudit.user_id == user))).scalars().all()
    assert any('兑换码' in a.reason for a in audits)

    for bad in ((99, 0, 6, 1), (99, 3, 121, 1), (99, 3, 1, 51), (99, 3, 1, 0), (99, 3, 1, -5)):
        with pytest.raises(ValueError):
            await membership.generate_redeem_codes(*bad)


async def test_redeem_expired_and_cancel(database):
    codes, _ = await membership.generate_redeem_codes(99, 2, 3, 1, expires_days=1)
    async with get_session() as session:
        row = (await session.execute(
            select(VipRedeemCode).where(VipRedeemCode.code == codes[0]))).scalar_one()
        row.expires_at = '2000-01-01 00:00:00'
        await session.commit()
    status, _ = await membership.redeem_code(777, codes[0])
    assert status == 'expired'

    batch_codes, batch = await membership.generate_redeem_codes(99, 1, 1, 2)
    assert await membership.cancel_redeem_codes(99, batch) == 2
    status, _ = await membership.redeem_code(777, batch_codes[0])
    assert status == 'cancelled'

    single, _ = await membership.generate_redeem_codes(99, 1, 1, 2)
    assert await membership.cancel_redeem_codes(99, single[0]) == 1
    assert await membership.cancel_redeem_codes(99, single[0]) == 0

    # 已兑换的码不受作废影响
    used, _ = await membership.generate_redeem_codes(99, 1, 1, 1)
    status, _ = await membership.redeem_code(777, used[0])
    assert status == 'ok'
    assert await membership.cancel_redeem_codes(99, used[0]) == 0

    listing = await membership.list_redeem_codes('cancelled', limit=10)
    assert all(r['status'] == 'cancelled' for r in listing)


async def test_vipadmin_bots_lists_all_statuses(database, monkeypatch):
    monkeypatch.setattr(membership_admin, 'ADMIN_IDS', [99])
    owner = 8177075430
    async with get_session() as session:
        session.add_all([
            UserBot(id=1, bot_username='alivebot', owner_id=owner, bot_token='t1',
                    status='active', created_at='2026-01-01 00:00:00'),
            UserBot(id=2, bot_username='pausebot', owner_id=owner, bot_token='t2',
                    status='paused', created_at='2026-02-01 00:00:00'),
            UserBot(id=3, bot_username='deadbot', owner_id=owner, bot_token='t3',
                    status='revoked', created_at='2026-03-01 00:00:00'),
            UserBot(id=4, bot_username='gonebot', owner_id=owner, bot_token='t4',
                    status='deleted', created_at='2026-04-01 00:00:00'),
        ])
        await session.commit()

    reply = AsyncMock()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=99),
                             effective_message=SimpleNamespace(reply_text=reply))
    await membership_admin.vipadmin_command(
        update, SimpleNamespace(args=['bots', str(owner)]))

    texts = [call.args[0] for call in reply.await_args_list]
    joined = '\n'.join(texts)
    assert f'<a href="tg://user?id={owner}">{owner}</a>' in joined
    assert '名下 Bot 共 4 个' in joined
    assert '活跃 1' in joined and '暂停 1' in joined
    assert '失效 1' in joined and '已删除 1' in joined
    for name in ('alivebot', 'pausebot', 'deadbot', 'gonebot'):
        assert any(name in text for text in texts)
    assert all(call.kwargs['parse_mode'] == 'HTML' for call in reply.await_args_list)
    # 无 Bot 的用户
    reply.reset_mock()
    await membership_admin.vipadmin_command(update, SimpleNamespace(args=['bots', '42']))
    assert '共 0 个' in reply.await_args_list[0].args[0]


async def test_vipadmin_gen_and_revoke_flow(database, monkeypatch):
    monkeypatch.setattr(membership_admin, 'ADMIN_IDS', [99])
    reply = AsyncMock()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=99),
                             effective_message=SimpleNamespace(reply_text=reply))
    await membership_admin.vipadmin_command(
        update, SimpleNamespace(args=['gen', '2', '12', '2', '30']))
    gen_text = reply.await_args_list[0].args[0]
    assert '已生成 2 个兑换码' in gen_text and 'vip_' in gen_text

    codes = [line for line in gen_text.split('\n') if line.startswith('vip_')]
    await membership_admin.vipadmin_command(
        update, SimpleNamespace(args=['codes', 'pending']))
    listing = reply.await_args_list[-1].args[0]
    assert all(code in listing for code in codes)

    await membership_admin.vipadmin_command(
        update, SimpleNamespace(args=['revoke', codes[0], '发放错了']))
    assert '已作废 1 个' in reply.await_args_list[-1].args[0]
    status, _ = await membership.redeem_code(777, codes[0])
    assert status == 'cancelled'
    status, _ = await membership.redeem_code(777, codes[1])
    assert status == 'ok'
