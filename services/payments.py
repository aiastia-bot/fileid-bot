"""Unique order ledger, atomic VIP fulfilment and recoverable payment checks."""

import logging
import time
import uuid
from datetime import datetime, timedelta
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from telegram.error import BadRequest, NetworkError, TelegramError
from db.core import get_session, _model_to_dict
from db.models import PaymentOrder, StarPayment, User
from services.membership import get_plan, grant, invalidate_user, now_text
from config import BASIC_LEVEL, PREMIUM_GIFT_PRICES, PREMIUM_USER_MARKUP, VIP_PLANS

logger = logging.getLogger(__name__)


async def create_order(user_id, product, months, level=0):
    if product == "vip":
        if level not in (1, 2, 3, BASIC_LEVEL) or months not in (1, 12):
            raise ValueError("无效的会员方案")
        plan = await get_plan(level)
        if not plan["enabled"]:
            raise ValueError("该套餐已停止销售")
        from db.vip import is_free_registration_closed, get_user_vip_info

        if level == BASIC_LEVEL and not await is_free_registration_closed():
            raise ValueError("基础版暂未开放购买")
        info = await get_user_vip_info(user_id)
        if (
            info["vip_level"] == level
            and info["is_active"]
            and info["vip_expire_at"] is None
        ):
            raise ValueError("当前为永久会员，无需续费")
        amount = plan["yearly_price"] if months == 12 else plan["monthly_price"]
    elif product == "premium" and months in PREMIUM_GIFT_PRICES:
        amount = PREMIUM_GIFT_PRICES[months] + PREMIUM_USER_MARKUP
    else:
        raise ValueError("无效的商品")
    payload = "order_" + uuid.uuid4().hex
    async with get_session() as session:
        order = PaymentOrder(
            payload=payload,
            user_id=user_id,
            product=product,
            level=level,
            months=months,
            amount=amount,
            status="created",
            attempts=0,
            next_retry=0,
            error="",
            created_at=now_text(),
            updated_at=now_text(),
        )
        session.add(order)
        await session.commit()
        return _model_to_dict(order)


async def get_order(payload):
    async with get_session() as session:
        row = await session.get(PaymentOrder, payload)
        return _model_to_dict(row) if row else None


async def checkout_error(payload, user_id, amount, currency):
    from db.bots import is_user_blacklisted

    if await is_user_blacklisted(user_id):
        return "当前账号无法购买，请联系管理员"
    order = await get_order(payload)
    if not order:
        return "订单不存在，请重新发起购买"
    if order["user_id"] != user_id or currency != "XTR" or order["amount"] != amount:
        return "付款账号、金额或币种与订单不符"
    if order["status"] != "created":
        return "订单已支付或正在处理，请查看支付记录"
    if datetime.now() - datetime.strptime(
        order["created_at"], "%Y-%m-%d %H:%M:%S"
    ) > timedelta(hours=24):
        return "订单已过期，请重新发起购买"
    return None


async def import_legacy_order(payload, user_id, amount):
    """Accept only well-formed old invoice receipts; new checkout requires a saved order."""
    parts = payload.split("_")
    try:
        product = parts[0]
        if product == "vip" and len(parts) == 5:
            level, months, expected_user, stamp = map(int, parts[1:])
            if level not in (1, 2, 3, BASIC_LEVEL) or months not in (1, 12):
                return
            plan = VIP_PLANS[level]
            expected = plan["yearly_price"] if months == 12 else plan["monthly_price"]
        elif product == "premium" and len(parts) == 4:
            months, expected_user, stamp = map(int, parts[1:])
            level = 0
            expected = PREMIUM_GIFT_PRICES[months] + PREMIUM_USER_MARKUP
        else:
            return
        if expected_user != user_id or expected != amount or stamp <= 0:
            return
    except (ValueError, KeyError):
        return
    async with get_session() as session:
        session.add(
            PaymentOrder(
                payload=payload,
                user_id=user_id,
                product=product,
                level=level,
                months=months,
                amount=amount,
                status="created",
                attempts=0,
                next_retry=0,
                error="",
                created_at=now_text(),
                updated_at=now_text(),
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()


async def receive_payment(payload, user_id, amount, currency, charge_id):
    """Persist first. Receipt of one charge can change at most one order."""
    if not await get_order(payload):
        await import_legacy_order(payload, user_id, amount)
    async with get_session() as session:
        order = await session.get(PaymentOrder, payload)
        if (
            not order
            or order.user_id != user_id
            or order.amount != amount
            or currency != "XTR"
            or not charge_id
        ):
            raise ValueError("支付与订单不符，请联系管理员")
        if order.charge_id:
            if order.charge_id != charge_id:
                raise ValueError("订单存在不同的交易号，需要人工核查")
            return order.status
        legacy = (
            await session.execute(
                select(StarPayment.id)
                .where(StarPayment.telegram_charge_id == charge_id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if legacy:
            raise ValueError("交易已存在于历史账单，需要人工核查")
        try:
            result = await session.execute(
                update(PaymentOrder)
                .where(
                    PaymentOrder.payload == payload,
                    PaymentOrder.status == "created",
                    PaymentOrder.charge_id.is_(None),
                )
                .values(charge_id=charge_id, status="received", updated_at=now_text())
            )
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise ValueError("交易号已被使用") from None
        if not result.rowcount:
            return (await get_order(payload))["status"]
        return "received"


async def verify_payment(bot, charge_id, user_id, amount, payload):
    """Paginate rather than relying on a fixed recent-50 window."""
    started = time.monotonic()
    offset = 0
    while time.monotonic() - started < 30:
        result = await bot.get_star_transactions(
            offset=offset, limit=100, read_timeout=5
        )
        data = result.to_dict() if hasattr(result, "to_dict") else result
        transactions = data.get("transactions", [])
        for tx in transactions:
            if tx["id"] == charge_id:
                source = tx.get("source", {})
                return (
                    tx.get("amount") == amount
                    and source.get("type") == "user"
                    and source.get("user", {}).get("id") == user_id
                    and source.get("transaction_type") == "invoice_payment"
                    and source.get("invoice_payload") == payload
                )
        if len(transactions) < 100:
            break
        offset += 100
    return False


async def _state(payload, status, error="", *, retry=0):
    async with get_session() as session:
        await session.execute(
            update(PaymentOrder)
            .where(PaymentOrder.payload == payload)
            .values(
                status=status,
                error=error[:2000],
                next_retry=int(time.time()) + retry,
                updated_at=now_text(),
            )
        )
        await session.commit()


async def fulfill_order(bot, payload):
    async with get_session() as session:
        claimed = await session.execute(
            update(PaymentOrder)
            .where(
                PaymentOrder.payload == payload,
                PaymentOrder.status.in_(["received", "retry"]),
                PaymentOrder.next_retry <= int(time.time()),
            )
            .values(
                status="verifying",
                attempts=PaymentOrder.attempts + 1,
                updated_at=now_text(),
            )
        )
        await session.commit()
        if not claimed.rowcount:
            return (await get_order(payload))["status"]
    order = await get_order(payload)
    try:
        verified = await verify_payment(
            bot, order["charge_id"], order["user_id"], order["amount"], payload
        )
        if not verified:
            raise ValueError("交易尚未查到或校验不匹配")
    except Exception as exc:
        status = "review" if order["attempts"] >= 8 else "retry"
        await _state(
            payload, status, str(exc), retry=min(3600, 30 * 2 ** order["attempts"])
        )
        return status
    if order["product"] == "vip":
        try:
            async with get_session() as session:
                # Claim inside the same transaction as the grant (also on SQLite).
                changed = await session.execute(
                    update(PaymentOrder)
                    .where(
                        PaymentOrder.payload == payload,
                        PaymentOrder.status == "verifying",
                    )
                    .values(status="completed", updated_at=now_text(), error="")
                )
                if not changed.rowcount:
                    return "completed"
                user = await session.get(User, order["user_id"], with_for_update=True)
                if not user:
                    user = User(
                        user_id=order["user_id"],
                        vip_level=0,
                        vip_migrated=0,
                        created_at=now_text(),
                    )
                    session.add(user)
                grant(user, order["level"], order["months"])
                session.add(
                    StarPayment(
                        user_id=order["user_id"],
                        amount=order["amount"],
                        vip_level=order["level"],
                        months=order["months"],
                        payload=payload,
                        telegram_charge_id=order["charge_id"],
                        created_at=now_text(),
                    )
                )
                await session.commit()
        except Exception as exc:
            await _state(payload, "review", str(exc))
            return "review"
        try:
            await invalidate_user(order["user_id"])
        except Exception:
            logger.exception("Grant committed; cache invalidation deferred")
    else:
        # Persist intent BEFORE external spending. A crash/timeout here requires
        # reconciliation; never automatically send a second subscription.
        await _state(payload, "sending")
        try:
            await bot.gift_premium_subscription(
                user_id=order["user_id"],
                month_count=order["months"],
                star_count=PREMIUM_GIFT_PRICES[order["months"]],
            )
        except BadRequest as exc:
            await _state(payload, "refund_pending", str(exc))
            return await refund_order(bot, payload)
        except NetworkError as exc:
            await _state(payload, "review", "Premium 结果不确定：" + str(exc))
            return "review"
        except TelegramError as exc:
            await _state(payload, "review", str(exc))
            return "review"
        await _state(payload, "completed")
    # Notification failures do not roll back successful grants or trigger refunds.
    try:
        if order["product"] == "vip":
            from services.entitlements import reconcile_user

            await reconcile_user(order["user_id"])
        await bot.send_message(
            chat_id=order["user_id"],
            text="✅ 支付已完成，会员权益已生效。使用 /vip 或 /premium 查看。",
        )
    except Exception:
        logger.exception(
            "Payment completed; notification/reconciliation deferred: %s", payload
        )
    return "completed"


async def refund_order(bot, payload):
    order = await get_order(payload)
    if order["status"] != "refund_pending":
        return order["status"]
    try:
        await bot.refund_star_payment(
            user_id=order["user_id"], telegram_payment_charge_id=order["charge_id"]
        )
    except Exception as exc:
        await _state(payload, "refund_pending", str(exc), retry=300)
        return "refund_pending"
    await _state(payload, "refunded")
    return "refunded"


async def process_pending(bot):
    # Only verification is safely retryable after a crash. External spending is not.
    cutoff = (datetime.now() - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    async with get_session() as session:
        await session.execute(
            update(PaymentOrder)
            .where(PaymentOrder.status == "verifying", PaymentOrder.updated_at < cutoff)
            .values(status="retry")
        )
        await session.execute(
            update(PaymentOrder)
            .where(PaymentOrder.status == "sending", PaymentOrder.updated_at < cutoff)
            .values(status="review", error="发放中断，需核对 Telegram 交易后处理")
        )
        await session.commit()
        rows = (
            (
                await session.execute(
                    select(PaymentOrder)
                    .where(
                        PaymentOrder.status.in_(
                            ["received", "retry", "refund_pending"]
                        ),
                        PaymentOrder.next_retry <= int(time.time()),
                    )
                    .order_by(PaymentOrder.created_at)
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        orders = [_model_to_dict(o) for o in rows]
    for order in orders:
        if order["status"] == "refund_pending":
            await refund_order(bot, order["payload"])
        else:
            await fulfill_order(bot, order["payload"])


async def list_orders(user_id=None, limit=20):
    async with get_session() as session:
        query = (
            select(PaymentOrder).order_by(PaymentOrder.created_at.desc()).limit(limit)
        )
        if user_id is not None:
            query = query.where(PaymentOrder.user_id == user_id)
        return [_model_to_dict(o) for o in (await session.execute(query)).scalars()]
