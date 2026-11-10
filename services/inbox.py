"""Webhook receipt is durable before HTTP 200; processing has bounded retries."""

import asyncio
import contextvars
import json
import logging
import time
from sqlalchemy import select, update, or_, and_
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


class Inbox:
    def __init__(self, resolve_app):
        self.resolve_app = resolve_app
        self.node_id = NODE_ID if ROLE == "worker" else ROLE
        self.tasks = []
        self.wake = asyncio.Event()
        self.locks = {}
        self.running = False

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

    async def _next(self):
        now = int(time.time())
        async with get_session() as session:
            rows = (
                (
                    await session.execute(
                        select(UpdateInbox)
                        .where(
                            UpdateInbox.node_id == self.node_id,
                            or_(
                                and_(
                                    UpdateInbox.state == "pending",
                                    UpdateInbox.available_at <= now,
                                ),
                                and_(
                                    UpdateInbox.state == "processing",
                                    UpdateInbox.available_at <= now,
                                ),
                            ),
                        )
                        .order_by(UpdateInbox.created_at, UpdateInbox.update_id)
                        .limit(100)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                app = self.resolve_app(row.bot_key)
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
                if app is None or self.locks.get(lock_key, False):
                    continue
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
                if changed.rowcount:
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
        return None

    @staticmethod
    def _is_permanent_user_error(exc: Exception) -> bool:
        """用户侧永久错误：重试永远不会成功，直接终结该 update

        拉黑（bot was blocked by the user）/ 销号（user is deactivated）。
        """
        text = str(exc).lower()
        return isinstance(exc, Forbidden) and (
            "blocked by the user" in text or "user is deactivated" in text
        )

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
                if job and self._is_permanent_user_error(exc):
                    # 拉黑/销号：重试永远失败，直接终结，不刷重试日志
                    logger.info("用户侧永久错误，跳过重试: %s", exc)
                    await self._finish(job[0], job[1], "completed", str(exc))
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
