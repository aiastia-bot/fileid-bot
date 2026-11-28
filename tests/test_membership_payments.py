import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, func
from telegram.error import BadRequest, TimedOut

from db.core import get_session
from db.models import StarPayment, MembershipAudit, UserBot
from db.vip import get_user_vip_info, get_max_bots_for_user, get_user_vip_level
from services.membership import set_plan, adjust_member, member_feature
from services.payments import (
    create_order,
    checkout_error,
    receive_payment,
    fulfill_order,
    get_order,
    process_pending,
)
from services.entitlements import reconcile_user
from services import runtime


def payment_bot(order, charge="charge-1"):
    transaction = {
        "id": charge,
        "amount": order["amount"],
        "source": {
            "type": "user",
            "user": {"id": order["user_id"]},
            "transaction_type": "invoice_payment",
            "invoice_payload": order["payload"],
        },
    }
    return SimpleNamespace(
        get_star_transactions=AsyncMock(return_value={"transactions": [transaction]}),
        gift_premium_subscription=AsyncMock(),
        refund_star_payment=AsyncMock(),
        send_message=AsyncMock(),
    )


async def paid_order(product="vip", level=1):
    order = await create_order(
        1234567890123, product, 1 if product == "vip" else 3, level
    )
    await receive_payment(
        order["payload"], order["user_id"], order["amount"], "XTR", "charge-1"
    )
    return order


async def test_atomic_duplicate_payment(database):
    order = await paid_order()
    bot = payment_bot(order)
    # Two concurrent callbacks must produce one grant and one ledger row.
    statuses = await asyncio.gather(
        fulfill_order(bot, order["payload"]), fulfill_order(bot, order["payload"])
    )
    assert "completed" in statuses
    first = await get_user_vip_info(order["user_id"])
    await receive_payment(
        order["payload"], order["user_id"], order["amount"], "XTR", "charge-1"
    )
    await fulfill_order(bot, order["payload"])
    assert (await get_user_vip_info(order["user_id"]))["vip_expire_at"] == first[
        "vip_expire_at"
    ]
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(StarPayment)) == 1


async def test_charge_cannot_pay_two_orders(database):
    first = await paid_order()
    second = await create_order(first["user_id"], "vip", 1, 2)
    with pytest.raises(ValueError, match="已被使用"):
        await receive_payment(
            second["payload"], second["user_id"], second["amount"], "XTR", "charge-1"
        )


async def test_snapshot_price_and_checkout_validation(database):
    order = await create_order(1, "vip", 1, 1)
    await set_plan(99, 1, {"monthly_price": 222, "enabled": False})
    assert await checkout_error(order["payload"], 1, order["amount"], "XTR") is None
    assert await checkout_error(order["payload"], 2, order["amount"], "XTR")
    assert await checkout_error(order["payload"], 1, order["amount"], "USD")
    with pytest.raises(ValueError):
        await create_order(1, "vip", 1, 1)


@pytest.mark.parametrize(
    "field,value",
    [("max_bots", -1), ("monthly_price", 0), ("enabled", "yes"), ("name", "")],
)
async def test_plan_validation(database, field, value):
    with pytest.raises(ValueError):
        await set_plan(99, 1, {field: value})


async def test_runtime_entitlements_and_audit(database):
    await adjust_member(99, 1, level=1, months=1, reason="support grant")
    assert await member_feature(1, "forward_mode")
    await set_plan(99, 1, {"max_bots": 7, "file_limit": 123, "forward_mode": False})
    assert await get_max_bots_for_user(1) == 7
    assert not await member_feature(1, "forward_mode")
    from db.files import get_owner_file_limit

    assert await get_owner_file_limit(1) == 123
    before = (await get_user_vip_info(1))["vip_expire_at"]
    await adjust_member(99, 1, days=-2, reason="correction")
    after = (await get_user_vip_info(1))["vip_expire_at"]
    assert datetime.strptime(before, "%Y-%m-%d %H:%M:%S") - datetime.strptime(
        after, "%Y-%m-%d %H:%M:%S"
    ) == timedelta(days=2)
    async with get_session() as session:
        assert (
            await session.scalar(select(func.count()).select_from(MembershipAudit)) == 3
        )


async def test_permanent_admin_grant_and_cancellation(database):
    await adjust_member(99, 1, level=3, months=0)
    info = await get_user_vip_info(1)
    assert info["is_active"] and info["vip_expire_at"] is None
    with pytest.raises(ValueError):
        await create_order(1, "vip", 1, 3)
    await adjust_member(99, 1, level=0, months=0)
    assert (await get_user_vip_info(1))["vip_level"] == 0


async def test_wrong_payer_never_gets_vip(database):
    order = await paid_order()
    bot = payment_bot(order)
    bot.get_star_transactions.return_value["transactions"][0]["source"]["user"][
        "id"
    ] = 8
    assert await fulfill_order(bot, order["payload"]) == "retry"
    assert (await get_user_vip_info(order["user_id"]))["vip_level"] == 0


async def test_premium_duplicate_does_not_spend_twice(database):
    order = await paid_order("premium", 0)
    bot = payment_bot(order)
    await asyncio.gather(
        fulfill_order(bot, order["payload"]), fulfill_order(bot, order["payload"])
    )
    assert (await get_order(order["payload"]))["status"] == "completed"
    bot.gift_premium_subscription.assert_awaited_once()


async def test_premium_timeout_is_review_without_refund(database):
    order = await paid_order("premium", 0)
    bot = payment_bot(order)
    bot.gift_premium_subscription.side_effect = TimedOut()
    assert await fulfill_order(bot, order["payload"]) == "review"
    await process_pending(bot)
    bot.gift_premium_subscription.assert_awaited_once()
    bot.refund_star_payment.assert_not_awaited()


async def test_confirmed_premium_failure_refunds(database):
    order = await paid_order("premium", 0)
    bot = payment_bot(order)
    bot.gift_premium_subscription.side_effect = BadRequest("Cannot gift to this user")
    assert await fulfill_order(bot, order["payload"]) == "refunded"
    bot.refund_star_payment.assert_awaited_once()


async def test_notification_failure_does_not_refund(database):
    order = await paid_order("premium", 0)
    bot = payment_bot(order)
    bot.send_message.side_effect = TimedOut()
    assert await fulfill_order(bot, order["payload"]) == "completed"
    bot.refund_star_payment.assert_not_awaited()


async def test_reconcile_resumes_only_membership_paused(database):
    await adjust_member(99, 1, level=2, months=1)
    async with get_session() as session:
        for i, status in enumerate(["active", "paused", "admin_stopped", "revoked"], 1):
            session.add(
                UserBot(id=i, owner_id=1, bot_token=f"token-{i}", status=status)
            )
        await session.commit()
    manager = SimpleNamespace(
        start_bot=AsyncMock(return_value=True), stop_bot=AsyncMock()
    )
    runtime.configure(bot_manager=manager)
    await reconcile_user(1)
    assert manager.start_bot.await_count == 1
    assert manager.start_bot.call_args.args[0]["id"] == 2
    await set_plan(99, 2, {"max_bots": 1})
    await reconcile_user(1)
    manager.stop_bot.assert_awaited_with(2)


async def test_expiry_removes_features_and_deduplicates_notice(database):
    await adjust_member(99, 1, level=1, months=1)
    await adjust_member(99, 1, expires="2000-01-01 00:00:00")
    assert not await member_feature(1, "forward_mode")
    assert await get_user_vip_level(1) == 0
    assert await get_user_vip_level(1) == 0
    from db.models import UserNotice

    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(UserNotice)) == 1


def test_downgrade_blocked_matrix():
    from services.membership import downgrade_blocked

    active_vip3 = {'vip_level': 3, 'is_active': True, 'vip_expire_at': '2027-01-01 00:00:00'}
    assert downgrade_blocked(active_vip3, 1)  # VIP3 有效期内买 VIP1 → 禁止
    assert downgrade_blocked(active_vip3, 2)
    assert downgrade_blocked(active_vip3, 4)  # 基础版低于 VIP1，同样禁止
    assert not downgrade_blocked(active_vip3, 3)  # 续费同等级允许
    permanent_vip3 = {'vip_level': 3, 'is_active': True, 'vip_expire_at': None}
    assert downgrade_blocked(permanent_vip3, 1)
    expired = {'vip_level': 3, 'is_active': False, 'vip_expire_at': '2026-01-01 00:00:00'}
    assert not downgrade_blocked(expired, 1)  # 已过期可买任意等级
    free = {'vip_level': 0, 'is_active': True, 'vip_expire_at': None}
    assert not downgrade_blocked(free, 1)
    assert not downgrade_blocked(None, 1)


async def test_buy_vip_downgrade_blocked(database, monkeypatch):
    from handlers.master import stars

    async def fake_info(user_id):
        return {'user_id': user_id, 'vip_level': 3, 'vip_name': 'VIP 3',
                'is_active': True, 'vip_expire_at': '2027-01-01 00:00:00'}

    monkeypatch.setattr('db.vip.get_user_vip_info', fake_info)
    create_order = AsyncMock(return_value={'payload': 'p', 'amount': 100})
    monkeypatch.setattr('services.payments.create_order', create_order)
    send_invoice = AsyncMock()
    context = SimpleNamespace(bot=SimpleNamespace(send_invoice=send_invoice))

    query = SimpleNamespace(answer=AsyncMock(), data='buy_vip|1|1',
                            message=SimpleNamespace(reply_text=AsyncMock()))
    await stars.buy_vip_callback(
        SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=99)), context)
    assert query.answer.await_count == 2  # 开场应答一次 + 降级警告一次
    blocked_call = query.answer.await_args_list[-1]
    assert blocked_call.kwargs.get('show_alert') is True
    assert '不能购买更低等级' in blocked_call.args[0]
    create_order.assert_not_awaited()
    send_invoice.assert_not_awaited()

    # 同等级续费不受限
    query2 = SimpleNamespace(answer=AsyncMock(), data='buy_vip|3|1',
                             message=SimpleNamespace(reply_text=AsyncMock()))
    await stars.buy_vip_callback(
        SimpleNamespace(callback_query=query2, effective_user=SimpleNamespace(id=99)), context)
    create_order.assert_awaited_once()
    send_invoice.assert_awaited_once()


async def test_buy_denied_callback_routes_to_alert(database, monkeypatch):
    from handlers.master import stars

    async def fake_info(user_id):
        return {'user_id': user_id, 'vip_level': 3, 'vip_name': 'VIP 3',
                'is_active': True, 'vip_expire_at': None}

    monkeypatch.setattr('db.vip.get_user_vip_info', fake_info)
    query = SimpleNamespace(answer=AsyncMock(), data='buy_denied|1',
                            message=SimpleNamespace(reply_text=AsyncMock()))
    await stars.vip_callback_router(
        SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=99)),
        SimpleNamespace())
    assert query.answer.await_args.kwargs.get('show_alert') is True
    assert '永久' in query.answer.await_args.args[0]

    # 会员过期后再点旧按钮：提示可以买了而不是报错
    async def expired_info(user_id):
        return {'user_id': user_id, 'vip_level': 3, 'vip_name': 'VIP 3',
                'is_active': False, 'vip_expire_at': '2026-01-01 00:00:00'}

    monkeypatch.setattr('db.vip.get_user_vip_info', expired_info)
    query2 = SimpleNamespace(answer=AsyncMock(), data='buy_denied|1',
                             message=SimpleNamespace(reply_text=AsyncMock()))
    await stars.vip_callback_router(
        SimpleNamespace(callback_query=query2, effective_user=SimpleNamespace(id=99)),
        SimpleNamespace())
    assert '重新发送 /vip' in query2.answer.await_args.args[0]


async def test_upgrade_credit_calculation(database):
    from datetime import timedelta
    from services.payments import upgrade_credit

    uid = 5566001
    info = {'vip_level': 1, 'is_active': True,
            'vip_expire_at': (datetime.now() + timedelta(days=20, minutes=5)).strftime(
                '%Y-%m-%d %H:%M:%S')}
    # 无支付记录：按月付价兜底 int(20 天 × 100⭐/30 天) = 66
    credit, days = await upgrade_credit(uid, info, 500)
    assert (days, credit) == (20, 66)

    # 最近一笔实付是年付 → 日均价按实付折算，折扣不漏算
    async with get_session() as session:
        session.add(StarPayment(user_id=uid, amount=1000, vip_level=1, months=12,
                                payload='upg-p1', telegram_charge_id='c-upg-1',
                                created_at=datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        await session.commit()
    credit2, _ = await upgrade_credit(uid, info, 500)
    assert credit2 == int(20 * 1000 / 360)  # 55

    expired = {'vip_level': 1, 'is_active': False, 'vip_expire_at': info['vip_expire_at']}
    assert await upgrade_credit(uid, expired, 500) == (0, 0)
    assert await upgrade_credit(uid, {'vip_level': 0, 'is_active': True,
                                      'vip_expire_at': None}, 500) == (0, 0)


async def test_create_order_upgrade_discount(database):
    order = await create_order(5566002, 'vip', 1, 3, discount_stars=66)
    assert (order['amount'], order['discount']) == (434, 66)
    with pytest.raises(ValueError):
        await create_order(5566002, 'vip', 1, 3, discount_stars=500)  # 倒挂：抵扣≥标价
    with pytest.raises(ValueError):
        await create_order(5566002, 'vip', 1, 3, discount_stars=-5)
    with pytest.raises(ValueError):
        await create_order(5566002, 'premium', 3, 0, discount_stars=10)  # 仅会员支持抵扣


async def test_buy_vip_upgrade_discount_flow(database, monkeypatch):
    from datetime import timedelta
    from handlers.master import stars
    from services.payments import get_order

    async def fake_info(user_id):
        return {'user_id': user_id, 'vip_level': 1, 'vip_name': 'VIP 1',
                'is_active': True,
                'vip_expire_at': (datetime.now() + timedelta(days=20, minutes=5)).strftime(
                    '%Y-%m-%d %H:%M:%S')}

    monkeypatch.setattr('db.vip.get_user_vip_info', fake_info)
    send_invoice = AsyncMock()
    context = SimpleNamespace(bot=SimpleNamespace(send_invoice=send_invoice))
    query = SimpleNamespace(answer=AsyncMock(), data='buy_vip|3|1',
                            message=SimpleNamespace(reply_text=AsyncMock()))
    await stars.buy_vip_callback(
        SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=99)), context)
    send_invoice.assert_awaited_once()
    kwargs = send_invoice.await_args.kwargs
    assert kwargs['prices'][0].amount == 434  # 500 − int(20×100/30)
    assert '已抵扣当前会员剩余价值 66⭐（剩余 20 天）' in kwargs['description']
    stored = await get_order(kwargs['payload'])
    assert (stored['amount'], stored['discount']) == (434, 66)


async def test_buy_vip_upgrade_inverted_blocked(database, monkeypatch):
    from datetime import timedelta
    from handlers.master import stars

    async def fake_info(user_id):
        return {'user_id': user_id, 'vip_level': 2, 'vip_name': 'VIP 2',
                'is_active': True,
                'vip_expire_at': (datetime.now() + timedelta(days=200, minutes=5)).strftime(
                    '%Y-%m-%d %H:%M:%S')}

    monkeypatch.setattr('db.vip.get_user_vip_info', fake_info)
    create_order_mock = AsyncMock()
    monkeypatch.setattr('services.payments.create_order', create_order_mock)
    query = SimpleNamespace(answer=AsyncMock(), data='buy_vip|3|1',
                            message=SimpleNamespace(reply_text=AsyncMock()))
    await stars.buy_vip_callback(
        SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=99)),
        SimpleNamespace(bot=SimpleNamespace(send_invoice=AsyncMock())))
    last = query.answer.await_args_list[-1]
    assert last.kwargs.get('show_alert') is True
    assert '剩余价值已超过目标价格' in last.args[0]
    create_order_mock.assert_not_awaited()


async def test_upgrade_order_audited_on_fulfill(database):
    order = await create_order(5566003, 'vip', 1, 3, discount_stars=66)
    await receive_payment(order['payload'], order['user_id'], order['amount'],
                          'XTR', 'charge-upg-2')
    bot = payment_bot(order, charge='charge-upg-2')
    assert await fulfill_order(bot, order['payload']) == 'completed'
    async with get_session() as session:
        audits = (await session.execute(
            select(MembershipAudit).where(
                MembershipAudit.user_id == order['user_id'],
                MembershipAudit.action == 'member:upgrade'))).scalars().all()
    assert len(audits) == 1 and '抵扣 66⭐' in audits[0].reason
    info = await get_user_vip_info(order['user_id'])
    assert info['vip_level'] == 3 and info['is_active']


async def test_payment_discount_migration(database):
    from db.core import init_db, get_engine
    from sqlalchemy import text

    async with get_engine().begin() as conn:
        await conn.execute(text('ALTER TABLE payment_orders DROP COLUMN discount'))
        await conn.execute(text('DELETE FROM schema_versions WHERE version=8'))
    await init_db()
    await init_db()  # 幂等
    async with get_engine().begin() as conn:
        cols = [row[1] for row in
                (await conn.execute(text('PRAGMA table_info(payment_orders)'))).fetchall()]
    assert 'discount' in cols
