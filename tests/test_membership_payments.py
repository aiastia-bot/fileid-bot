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
