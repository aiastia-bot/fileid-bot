"""Webhook receipt is durable before HTTP 200; processing has bounded retries."""

import asyncio
import contextvars
import json
import logging
import time
from types import SimpleNamespace
from sqlalchemy import select, update, or_, and_, func, bindparam
from sqlalchemy.exc import IntegrityError
from telegram import Update
from telegram.error import Forbidden
from db.core import get_session
from db.models import UpdateInbox
from config import (
    INBOX_WORKERS,
    INBOX_MAX_ATTEMPTS,
    WEBHOOK_UPDATE_TIMEOUT,
    NODE_ID,
    ROLE,
)

logger = logging.getLogger(__name__)
processing_error = contextvars.ContextVar("processing_error", default=None)


def note_error(error):
    box = processing_error.get()
    if box is not None:
        box.append(error)


def is_permanent_user_error(exc: Exception) -> bool:
    """用户拉黑 Bot 或账号已注销时，重试无法送达。"""
    text = str(exc).lower()
    return isinstance(exc, Forbidden) and (
        "blocked by the user" in text or "user is deactivated" in text
    )


class Inbox:
    def __init__(self, resolve_app):
        self.resolve_app = resolve_app
        self.node_id = NODE_ID if ROLE == "worker" else ROLE
        self.tasks = []
        self.wake = asyncio.Event()
        self.locks = {}
        self.running = False
        self._rr = 0                # 按 bot 轮转的取任务起点
        self._dead_bot_warned = {}  # bot_key -> 上次告警时间（防刷屏）
        # 启动竞态：bot 应用异步加载完成前 resolve_app 全部落空，属正常现象
        self.suppress_dead_bot_warnings = False
        # 公平轮转查询语句只构建一次：32 个 worker 每秒轮询，每次重建 ORM 代理
        # 曾是 CPU 热点（2026-11-19 事故）；语句对象复用可命中 SQLAlchemy 编译缓存
        now_b = bindparam("now")
        rn = func.row_number().over(
            partition_by=UpdateInbox.bot_key,
            order_by=[UpdateInbox.created_at, UpdateInbox.update_id],
        ).label("rn")
        sub = (
            select(UpdateInbox)
            .where(
                UpdateInbox.node_id == bindparam("node_id"),
                or_(
                    and_(UpdateInbox.state == "pending", UpdateInbox.available_at <= now_b),
                    and_(UpdateInbox.state == "processing", UpdateInbox.available_at <= now_b),
                ),
            )
            .add_columns(rn)
            .subquery()
        )
        self._next_stmt = (
            select(sub)
            .where(sub.c.rn == 1)
            .order_by(sub.c.created_at, sub.c.update_id)
            .limit(300)
        )

    async def accept(self, bot_key, body):
        if not isinstance(body, dict) or type(body.get("update_id")) is not int:
            raise ValueError("Invalid update")
        async with get_session() as session:
            session.add(
                UpdateInbox(
                    bot_key=str(bot_key),
                    update_id=body["update_id"],
                    node_id=self.node_id,
                    body=json.dumps(body),
                    state="pending",
                    attempts=0,
                    available_at=0,
                    error="",
                    created_at=int(time.time()),
                )
            )
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                # Duplicate updates are already durably accepted.
        self.wake.set()

    def start(self):
        self.running = True
        self.tasks = [asyncio.create_task(self._loop()) for _ in range(INBOX_WORKERS)]

    async def stop(self):
        self.running = False
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    def _note_dead_bot(self, bot_key):
        """Bot 应用未加载时其积压会被无限跳过——必须让运维看得见（每小时最多一次）"""
        if self.suppress_dead_bot_warnings:
            return  # 启动加载期 resolve_app 落空是暂态，不是死 Bot
        now = time.time()
        if now - self._dead_bot_warned.get(bot_key, 0) >= 3600:
            self._dead_bot_warned[bot_key] = now
            logger.error(
                "Bot %s 的应用未加载，其积压 update 全部被跳过；"
                "如持续出现请重启容器重新加载 Bot", bot_key)

    async def _try_claim(self, session, row, now):
        """尝试领取一行；成功返回 job 元组，不可处理返回 None"""
        app = self.resolve_app(row.bot_key)
        if app is None:
            self._note_dead_bot(row.bot_key)
            return None
        body = json.loads(row.body)
        message = body.get("message", body.get("edited_message", {}))
        callback = body.get("callback_query", {})
        chat = message.get("chat", callback.get("message", {}).get("chat", {}))
        # Serialize one conversation, while other users and /stop can run.
        user = chat.get(
            "id",
            callback.get(
                "from", body.get("pre_checkout_query", {}).get("from", {})
            ).get("id", "system"),
        )
        is_control = (
            message.get("text", "").split(" ", 1)[0].split("@")[0]
            in ("/stop", "/queue")
            or callback.get("data") == "stop_auto"
        )
        lock_key = (
            row.bot_key,
            str(user),
            "control" if is_control else "conversation",
        )
        if self.locks.get(lock_key, False):
            return None
        changed = await session.execute(
            update(UpdateInbox)
            .where(
                UpdateInbox.bot_key == row.bot_key,
                UpdateInbox.update_id == row.update_id,
                UpdateInbox.state == row.state,
                UpdateInbox.available_at == row.available_at,
            )
            .values(
                state="processing",
                available_at=now + int(WEBHOOK_UPDATE_TIMEOUT) + 30,
                attempts=UpdateInbox.attempts + 1,
            )
        )
        if not changed.rowcount:
            return None
        self.locks[lock_key] = True
        await session.commit()
        return (
            row.bot_key,
            row.update_id,
            row.body,
            row.attempts,
            app,
            lock_key,
        )

    async def _next(self):
        now = int(time.time())
        async with get_session() as session:
            # 候选 = 每个 bot 的最老一条待处理 update（窗口函数按 bot 分组取头）：
            # 单一 bot 的洪峰或死 Bot 积压都无法推迟其他 bot 的响应
            # （row_number 需 SQLite 3.25+ / MySQL 8+）
            rows = (
                await session.execute(
                    self._next_stmt, {"node_id": self.node_id, "now": now}
                )
            ).all()
            if not rows:
                return None
            # 轮转起点：候选仍按全局年龄排序，但每次调用从不同 bot 起步，
            # 高频 bot 不会被无限偏袒
            offset = self._rr % len(rows)
            self._rr += 1
            for r in rows[offset:] + rows[:offset]:
                job = await self._try_claim(session, SimpleNamespace(**r._mapping), now)
                if job:
                    return job
        return None

    async def _loop(self):
        while self.running:
            job = None
            try:
                job = await self._next()
                if job is None:
                    self.wake.clear()
                    try:
                        await asyncio.wait_for(self.wake.wait(), timeout=1)
                    except asyncio.TimeoutError:
                        pass
                    continue
                key, uid, body, attempts, app, lock_key = job
                errors = []
                token = processing_error.set(errors)
                try:
                    await asyncio.wait_for(
                        app.process_update(Update.de_json(json.loads(body), app.bot)),
                        timeout=WEBHOOK_UPDATE_TIMEOUT,
                    )
                    if errors:
                        raise errors[0]
                finally:
                    processing_error.reset(token)
                await self._finish(key, uid, "completed")
            except asyncio.CancelledError:
                if job:
                    await self._finish(
                        job[0], job[1], "pending", "Shutdown interrupted processing"
                    )
                raise
            except Exception as exc:
                if job and is_permanent_user_error(exc):
                    # 拉黑/销号：重试永远失败，直接终结，不刷重试日志
                    logger.info("用户侧永久错误，跳过重试: %s", exc)
                    await self._finish(job[0], job[1], "completed", str(exc))
                elif isinstance(exc, asyncio.TimeoutError):
                    # 处理超时是已知暂态（限速排队满等），单行日志即可，不刷堆栈
                    if job:
                        state = "failed" if job[3] >= INBOX_MAX_ATTEMPTS else "pending"
                        logger.warning(
                            "Inbox update 处理超时(%ss)，%s: bot=%s update=%s",
                            WEBHOOK_UPDATE_TIMEOUT,
                            "已达重试上限，放弃" if state == "failed" else "稍后重试",
                            job[0], job[1],
                        )
                        await self._finish(
                            job[0], job[1], state, str(exc), min(300, 2 ** job[3])
                        )
                    else:
                        await asyncio.sleep(1)
                else:
                    logger.exception("Inbox processing failed")
                    if job:
                        state = "failed" if job[3] >= INBOX_MAX_ATTEMPTS else "pending"
                        await self._finish(
                            job[0], job[1], state, str(exc), min(300, 2 ** job[3])
                        )
                    else:
                        await asyncio.sleep(1)
            finally:
                if job:
                    self.locks.pop(job[5], None)

    async def _finish(self, key, uid, state, error="", delay=0):
        async with get_session() as session:
            await session.execute(
                update(UpdateInbox)
                .where(UpdateInbox.bot_key == key, UpdateInbox.update_id == uid)
                .values(
                    state=state,
                    error=error[:2000],
                    available_at=int(time.time()) + delay,
                )
            )
            await session.commit()


async def rehome(bot_key, node_id):
    """Move accepted work along with its bot; processing leases keep their delay."""
    async with get_session() as session:
        await session.execute(
            update(UpdateInbox)
            .where(
                UpdateInbox.bot_key == str(bot_key),
                UpdateInbox.state.in_(["pending", "processing"]),
            )
            .values(node_id=node_id)
        )
        await session.commit()
