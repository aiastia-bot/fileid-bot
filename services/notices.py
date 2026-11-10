"""Expiry notices have stable keys and retryable delivery."""

import time
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from telegram.error import Forbidden
from db.core import get_session
from db.models import UserNotice


async def enqueue(key, user_id, body, session=None):
    if session is None:
        async with get_session() as own_session:
            await enqueue(key, user_id, body, own_session)
            try:
                await own_session.commit()
            except IntegrityError:
                await own_session.rollback()
        return
    if await session.get(UserNotice, key) is None:
        session.add(
            UserNotice(
                key=key,
                user_id=user_id,
                body=body,
                state="pending",
                available_at=0,
                attempts=0,
            )
        )


async def deliver(bot):
    now = int(time.time())
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(UserNotice)
                    .where(
                        UserNotice.state.in_(["pending", "sending"]),
                        UserNotice.available_at <= now,
                    )
                    .limit(20)
                )
            )
            .scalars()
            .all()
        )
        jobs = [(r.key, r.user_id, r.body, r.attempts) for r in rows]
    for key, uid, body, attempts in jobs:
        async with get_session() as session:
            claim = await session.execute(
                update(UserNotice)
                .where(
                    UserNotice.key == key,
                    UserNotice.state.in_(["pending", "sending"]),
                    UserNotice.available_at <= now,
                )
                .values(
                    state="sending",
                    available_at=now + 120,
                    attempts=UserNotice.attempts + 1,
                )
            )
            await session.commit()
            if not claim.rowcount:
                continue
        try:
            await bot.send_message(chat_id=uid, text=body)
            state, delay = "completed", 0
        except Forbidden:
            state, delay = "blocked", 0
        except Exception:
            state, delay = (
                ("failed" if attempts >= 7 else "pending"),
                min(3600, 60 * 2**attempts),
            )
        async with get_session() as session:
            await session.execute(
                update(UserNotice)
                .where(UserNotice.key == key)
                .values(state=state, available_at=now + delay)
            )
            await session.commit()
