"""Admin commands for catalogue, membership, audit trail and order inspection."""

import json
from sqlalchemy import select, update as sa_update
from config import ADMIN_IDS
from db.core import get_session
from db.models import MembershipAudit, PaymentOrder, UpdateInbox, SendJob
from db.vip import get_user_vip_info, get_all_vip_users
from senders import _retry_send
from services.membership import get_plans, set_plan, adjust_member
from services.entitlements import reconcile_user, reconcile_all
from services.payments import list_orders, get_order, now_text


async def reply(update, text):
    # Keep per-message output below Telegram's limit; use plain text for JSON.
    for offset in range(0, len(text), 3900):
        await _retry_send(
            update.effective_message.reply_text, text[offset : offset + 3900]
        )


def authorized(update):
    return update.effective_user.id in ADMIN_IDS


async def setplan_command(update, context):
    if not authorized(update):
        await reply(update, "⛔ 此命令仅限管理员使用。")
        return
    args = context.args or []
    try:
        if len(args) <= 1:
            plans = await get_plans()
            if args:
                plans = {int(args[0]): plans[int(args[0])]}
            await reply(
                update,
                json.dumps(plans, ensure_ascii=False, indent=2)
                + "\n用法：/setplan 等级 字段 值 [字段 值 ...]\n"
                "字段：name max_bots file_limit monthly_price yearly_price forward_mode auto_delete enabled\n"
                "开关用 true/false；file_limit=0 表示不限。修改即时影响当前会员；已创建订单保留原价格。",
            )
            return
        if len(args) < 3 or len(args) % 2 == 0:
            raise ValueError("用法：/setplan 等级 字段 值 [字段 值 ...]")
        changes = {}
        for key, value in zip(args[1::2], args[2::2]):
            changes[key] = (
                value
                if key == "name"
                else (
                    value.lower() == "true"
                    if value.lower() in ("true", "false")
                    else int(value)
                )
            )
        result = await set_plan(update.effective_user.id, int(args[0]), changes)
        await reconcile_all()
        await reply(
            update,
            "✅ 套餐已更新并记录审计。\n" + json.dumps(result, ensure_ascii=False),
        )
    except (ValueError, KeyError) as exc:
        await reply(update, "❌ " + str(exc))


async def setvip_command(update, context):
    if not authorized(update):
        await reply(update, "⛔ 此命令仅限管理员使用。")
        return
    args = context.args or []
    try:
        if not args:
            await reply(
                update,
                "会员管理：\n/setvip info 用户ID\n/setvip list\n"
                "/setvip 用户ID 等级 [月份] [原因]\n月份默认12；0为永久；等级0取消会员。设置会替换原有效期。\n"
                "/vipadmin days 用户ID 天数 原因（正数延长、负数扣减）\n"
                "/vipadmin expires 用户ID YYYY-MM-DD 原因（当天23:59:59到期）\n"
                "/vipadmin history 用户ID\n/vipadmin orders [用户ID]\n"
                "/vipadmin retry 订单号（仅重新核验，不会重发结果不明的Premium）\n"
                "/vipadmin resolve 订单号 completed|refunded 原因（人工核对后登记结果）\n/setplan 查看和调整套餐",
            )
            return
        if args[0] == "info":
            await reply(
                update,
                json.dumps(
                    await get_user_vip_info(int(args[1])), ensure_ascii=False, indent=2
                ),
            )
            return
        if args[0] == "list":
            users = await get_all_vip_users()
            await reply(
                update,
                "\n".join(
                    f"{u['user_id']} | 等级{u['vip_level']} | {u['vip_expire_at'] or '永久'}"
                    for u in users
                )
                or "暂无会员",
            )
            return
        uid, level = int(args[0]), int(args[1])
        months = int(args[2]) if len(args) > 2 else 12
        reason = " ".join(args[3:]) or "管理员设置会员"
        result = await adjust_member(
            update.effective_user.id, uid, level=level, months=months, reason=reason
        )
        await reconcile_user(uid)
        await reply(
            update,
            "✅ 会员已设置并同步 Bot 额度。\n" + json.dumps(result, ensure_ascii=False),
        )
    except (ValueError, IndexError) as exc:
        await reply(update, "❌ 参数错误：" + str(exc))


async def vipadmin_command(update, context):
    if not authorized(update):
        await reply(update, "⛔ 此命令仅限管理员使用。")
        return
    args = context.args or []
    if not args:
        await setvip_command(update, context)
        return
    try:
        action = args[0]
        if action == "orders":
            rows = await list_orders(int(args[1]) if len(args) > 1 else None)
            await reply(
                update,
                "\n".join(
                    f"{r['payload']} | {r['user_id']} | {r['product']} | {r['amount']}⭐ | {r['status']} | {r['error']}"
                    for r in rows
                )
                or "暂无订单",
            )
            return
        if action == "history":
            uid = int(args[1])
            async with get_session() as session:
                rows = (
                    (
                        await session.execute(
                            select(MembershipAudit)
                            .where(MembershipAudit.user_id == uid)
                            .order_by(MembershipAudit.id.desc())
                            .limit(20)
                        )
                    )
                    .scalars()
                    .all()
                )
                await reply(
                    update,
                    "\n".join(
                        f"{r.created_at} | 操作者{r.actor_id} | {r.action}\n{r.before} → {r.after}\n原因：{r.reason}"
                        for r in rows
                    )
                    or "暂无调整记录",
                )
            return
        if action in ("retry", "resolve"):
            order = await get_order(args[1])
            if not order:
                raise ValueError("订单不存在")
            if action == "retry":
                if order["status"] not in ("review", "retry", "received") or (
                    order["product"] == "premium" and order["status"] == "review"
                ):
                    raise ValueError(
                        "该订单不能安全重试；Premium 结果不明时请人工对账后 resolve"
                    )
                status = "received"
                reason = "管理员重新核验"
            else:
                if (
                    len(args) < 4
                    or args[2] not in ("completed", "refunded")
                    or order["status"] not in ("review", "refund_pending")
                ):
                    raise ValueError(
                        "仅可登记待核查订单，需提供 completed/refunded 和核对原因"
                    )
                status, reason = args[2], " ".join(args[3:])
            async with get_session() as session:
                changed = await session.execute(
                    sa_update(PaymentOrder)
                    .where(
                        PaymentOrder.payload == args[1],
                        PaymentOrder.status == order["status"],
                    )
                    .values(
                        status=status, next_retry=0, error="", updated_at=now_text()
                    )
                )
                if not changed.rowcount:
                    raise ValueError("订单状态已改变，请重新查看")
                session.add(
                    MembershipAudit(
                        actor_id=update.effective_user.id,
                        user_id=order["user_id"],
                        action="order:" + action,
                        before=order["status"],
                        after=status,
                        reason=reason,
                        created_at=now_text(),
                    )
                )
                await session.commit()
            await reply(
                update,
                "✅ 已记录。resolve 只登记对账结果；不会再次发放或退款。"
                if action == "resolve"
                else "✅ 已安排重新核验。",
            )
            return
        if action not in ("days", "expires") or len(args) < 4:
            raise ValueError("用法：/vipadmin days|expires 用户ID 数值 原因")
        uid = int(args[1])
        kwargs = (
            {"days": int(args[2])}
            if action == "days"
            else {"expires": args[2] + " 23:59:59"}
        )
        result = await adjust_member(
            update.effective_user.id, uid, reason=" ".join(args[3:]), **kwargs
        )
        await reconcile_user(uid)
        await reply(
            update, "✅ 调整已生效。\n" + json.dumps(result, ensure_ascii=False)
        )
    except (ValueError, IndexError) as exc:
        await reply(update, "❌ " + str(exc))


async def tasks_command(update, context):
    if not authorized(update):
        await reply(update, "⛔ 此命令仅限管理员使用。")
        return
    async with get_session() as session:
        inbox = (
            (
                await session.execute(
                    select(UpdateInbox).where(UpdateInbox.state == "failed").limit(10)
                )
            )
            .scalars()
            .all()
        )
        jobs = (
            (
                await session.execute(
                    select(SendJob).where(SendJob.state == "failed").limit(10)
                )
            )
            .scalars()
            .all()
        )
        await reply(
            update,
            "失败消息：\n"
            + "\n".join(f"{r.bot_key}/{r.update_id}: {r.error}" for r in inbox)
            + "\n失败发送：\n"
            + "\n".join(
                f"{r.task_id} @{r.bot_name} chat={r.chat_id}: {r.error}" for r in jobs
            )
            + "\n部分发送或超时结果不明的任务请核对后只补发缺失文件，避免重复发送。",
        )
