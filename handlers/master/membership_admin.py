"""Admin commands for catalogue, membership, audit trail and order inspection."""

import json
from functools import partial
from sqlalchemy import select, update as sa_update
from config import ADMIN_IDS
from db.core import get_session
from db.models import MembershipAudit, PaymentOrder, UpdateInbox, SendJob
from db import get_user_bots_by_owner
from db.vip import get_user_vip_info, get_all_vip_users
from handlers.master._utils import escape, run_background_operation
from utils import user_id_html
from senders import _retry_send, send_later
from services.membership import (
    get_plans, set_plan, adjust_member,
    generate_redeem_codes, cancel_redeem_codes, list_redeem_codes,
)
from services.entitlements import reconcile_user, reconcile_all
from services.payments import list_orders, get_order, now_text


async def reply(update, text):
    # Keep per-message output below Telegram's limit; use plain text for JSON.
    for offset in range(0, len(text), 3900):
        await _retry_send(
            update.effective_message.reply_text, text[offset : offset + 3900]
        )


async def reply_html_rows(update, rows, empty):
    """按完整行打包；过长的纯文本详情在转义前切分，避免切断链接或实体。"""
    limit = 3900
    page = ''

    async def append(part):
        nonlocal page
        if page and len(page) + len(part) + 1 > limit:
            await _retry_send(update.effective_message.reply_text, page, parse_mode='HTML')
            page = ''
        page = f'{page}\n{part}' if page else part

    for prefix, detail in rows:
        line = prefix + escape(detail)
        if len(line) <= limit:
            await append(line)
        else:
            await append(prefix)
            # escape(quote=False) 最多把一个字符展开成 5 个字符。
            for offset in range(0, len(detail), limit // 5):
                await append(escape(detail[offset : offset + limit // 5]))
    await _retry_send(update.effective_message.reply_text, page or empty, parse_mode='HTML')


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
        await run_background_operation(update, context, ('set-plan',),
                                       partial(_set_plan_and_sync, update, int(args[0]), changes))
    except (ValueError, KeyError) as exc:
        await reply(update, "❌ " + str(exc))


async def _set_plan_and_sync(update, level, changes):
    try:
        result = await set_plan(update.effective_user.id, level, changes)
    except (ValueError, KeyError) as exc:
        await reply(update, "❌ " + str(exc))
        return

    send_later(update.effective_message.reply_text, "⏳ 套餐已更新，正在同步 Bot 额度…")
    await reconcile_all()
    await reply(update, "✅ 套餐已更新并记录审计。\n" + json.dumps(result, ensure_ascii=False))


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
                "/vipadmin bots 用户ID（名下 Bot 统计与列表，含已删除）\n"
                "/vipadmin gen 等级 月份 数量 [有效期天]（生成会员兑换码；月份0=永久）\n"
                "/vipadmin codes [pending|used|cancelled|all]（最近 20 个兑换码）\n"
                "/vipadmin revoke 码|批次 [原因]（作废待兑换码）\n"
                "/vipadmin retry 订单号（仅重新核验，不会重发结果不明的Premium）\n"
                "/vipadmin resolve 订单号 completed|refunded 原因（人工核对后登记结果）\n/setplan 查看和调整套餐",
            )
            return
        if args[0] == "info":
            info = await get_user_vip_info(int(args[1]))
            await _retry_send(update.effective_message.reply_text,
                              f"👤 用户：{user_id_html(info['user_id'])}", parse_mode='HTML')
            await reply(update, json.dumps(info, ensure_ascii=False, indent=2))
            return
        if args[0] == "list":
            users = await get_all_vip_users()
            await reply_html_rows(
                update,
                ((user_id_html(u['user_id']), f" | 等级{u['vip_level']} | {u['vip_expire_at'] or '永久'}")
                 for u in users),
                "暂无会员",
            )
            return
        uid, level = int(args[0]), int(args[1])
        months = int(args[2]) if len(args) > 2 else 12
        reason = " ".join(args[3:]) or "管理员设置会员"
        await run_background_operation(
            update, context, ('adjust-member', uid),
            partial(_adjust_member_and_sync, update, uid, reason, "会员已设置并同步 Bot 额度。",
                    level=level, months=months),
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
        if action == "bots":
            if len(args) < 2:
                raise ValueError("用法：/vipadmin bots 用户ID")
            uid = int(args[1])
            info = await get_user_vip_info(uid)
            bots = await get_user_bots_by_owner(uid, include_deleted=True)
            counts = {}
            for bot in bots:
                counts[bot['status']] = counts.get(bot['status'], 0) + 1
            failed = sum(counts.get(s, 0)
                         for s in ('revoked', 'banned', 'frozen', 'admin_stopped'))
            rows = [(
                f"👤 用户 {user_id_html(uid)}",
                f"（{info['vip_name']}·配额 {info['max_bots']}）名下 Bot 共 {len(bots)} 个："
                f"活跃 {counts.get('active', 0)} · 暂停 {counts.get('paused', 0)}"
                f" · 失效 {failed} · 已删除 {counts.get('deleted', 0)}",
            )]
            labels = {
                'active': '🟢 运行中', 'paused': '⏸ 暂停', 'revoked': '⚠️ Token 失效',
                'banned': '⚠️ 封禁', 'admin_stopped': '⚠️ 管理员已停止',
                'frozen': '⚠️ 已冻结', 'deleted': '🗑 已删除',
            }
            rows += [
                (f"• @{escape(bot['bot_username'])} · {labels.get(bot['status'], bot['status'])}",
                 f" · DB {bot['id']} · 创建 {bot['created_at'][:19]}")
                for bot in bots
            ]
            await reply_html_rows(update, rows, "该用户名下没有 Bot。")
            return
        if action == "gen":
            if len(args) < 4:
                raise ValueError("用法：/vipadmin gen 等级 月份 数量 [有效期天]")
            level, months, count = int(args[1]), int(args[2]), int(args[3])
            expires_days = int(args[4]) if len(args) > 4 else None
            codes, batch = await generate_redeem_codes(
                update.effective_user.id, level, months, count, expires_days)
            months_text = '永久' if months == 0 else f'{months} 个月'
            expiry_text = f'{expires_days} 天内有效' if expires_days else '长期有效'
            header = (
                f"✅ 已生成 {len(codes)} 个兑换码（批次 {batch} · 等级 {level} · "
                f"{months_text} · {expiry_text}）\n"
                "同等级续费时长叠加，跨等级从新周期开始。逐行复制发放：\n"
            )
            await reply(update, header + "\n".join(codes))
            return
        if action == "codes":
            status = args[1].lower() if len(args) > 1 else 'pending'
            if status not in ('pending', 'used', 'cancelled', 'all'):
                raise ValueError("状态为 pending|used|cancelled|all")
            code_rows = await list_redeem_codes(status, limit=20)
            lines = []
            for r in code_rows:
                months_text = '永久' if r['months'] == 0 else f"{r['months']}个月"
                line = (f"{r['code']} | {r['status']} | 等级{r['vip_level']}"
                        f" · {months_text} | 批次 {r['batch']}")
                if r['redeemed_by']:
                    line += f" | 已兑 {r['redeemed_by']} {r['redeemed_at'][:19]}"
                if r['expires_at']:
                    line += f" | 有效期至 {r['expires_at']}"
                lines.append(line)
            await reply(update, "\n".join(lines) or "暂无兑换码")
            return
        if action == "revoke":
            if len(args) < 2:
                raise ValueError("用法：/vipadmin revoke 码|批次 [原因]")
            cancelled = await cancel_redeem_codes(
                update.effective_user.id, args[1], " ".join(args[2:]))
            await reply(update, f"✅ 已作废 {cancelled} 个待兑换码。")
            return
        if action == "orders":
            rows = await list_orders(int(args[1]) if len(args) > 1 else None)
            await reply_html_rows(
                update,
                ((f"{escape(r['payload'])} | {user_id_html(r['user_id'])}",
                  f" | {r['product']} | {r['amount']}⭐ | {r['status']} | {r['error']}")
                 for r in rows),
                "暂无订单",
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
            await reply_html_rows(
                update,
                ((f"{escape(r.created_at)} | 操作者 {user_id_html(r.actor_id)}",
                  f" | {r.action}\n{r.before} → {r.after}\n原因：{r.reason}")
                 for r in rows),
                "暂无调整记录",
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
        await run_background_operation(
            update, context, ('adjust-member', uid),
            partial(_adjust_member_and_sync, update, uid, " ".join(args[3:]), "调整已生效。", **kwargs),
        )
    except (ValueError, IndexError) as exc:
        await reply(update, "❌ " + str(exc))


async def _adjust_member_and_sync(update, uid, reason, completion, **changes):
    try:
        result = await adjust_member(update.effective_user.id, uid, reason=reason, **changes)
    except (ValueError, IndexError) as exc:
        await reply(update, "❌ 参数错误：" + str(exc))
        return

    send_later(update.effective_message.reply_text, "⏳ 会员已更新，正在同步 Bot 额度…")
    await reconcile_user(uid)
    await reply(update, "✅ " + completion + "\n" + json.dumps(result, ensure_ascii=False))


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
