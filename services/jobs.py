"""Database is the durable send-task source, including without Redis."""

import time
import json
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from db.core import get_session
from db.models import SendJob


async def save_task(bot_name, task):
    async with get_session() as session:
        session.add(
            SendJob(
                task_id=task.task_id,
                bot_name=bot_name,
                chat_id=task.chat_id,
                body=task.to_json(),
                state="pending",
                attempts=0,
                error="",
                created_at=time.time_ns(),
                updated_at=int(time.time()),
            )
        )
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()


async def restore_tasks(bot_name):
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(SendJob)
                    .where(
                        SendJob.bot_name == bot_name,
                        SendJob.state.in_(["pending", "processing"]),
                    )
                    .order_by(SendJob.created_at, SendJob.task_id)
                )
            )
            .scalars()
            .all()
        )
        return [json.dumps(dict(json.loads(r.body), attempts=r.attempts)) for r in rows]


async def finish_task(task_id, state="completed", error=""):
    async with get_session() as session:
        await session.execute(
            update(SendJob)
            .where(SendJob.task_id == task_id)
            .values(state=state, error=error[:2000], updated_at=int(time.time()))
        )
        await session.commit()


async def begin_task(task_id):
    async with get_session() as session:
        result = await session.execute(
            update(SendJob)
            .where(
                SendJob.task_id == task_id, SendJob.state.in_(["pending", "processing"])
            )
            .values(
                state="processing",
                attempts=SendJob.attempts + 1,
                updated_at=int(time.time()),
            )
        )
        await session.commit()
        return bool(result.rowcount)
