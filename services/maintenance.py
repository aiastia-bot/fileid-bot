"""Bounded housekeeping. Failed tasks, orders and audits remain for inspection."""

import time
from sqlalchemy import delete
from config import DATA_RETENTION_DAYS
from db.core import get_session
from db.models import UpdateInbox, SendJob


async def cleanup_records():
    cutoff = int(time.time()) - DATA_RETENTION_DAYS * 86400
    async with get_session() as session:
        # Batches limit write locks on large installations.
        for model, key, age, terminal in (
            (
                UpdateInbox,
                (UpdateInbox.bot_key, UpdateInbox.update_id),
                UpdateInbox.created_at,
                ["completed"],
            ),
            (
                SendJob,
                (SendJob.task_id,),
                SendJob.updated_at,
                ["completed", "cancelled"],
            ),
        ):
            from sqlalchemy import select, tuple_

            rows = (
                await session.execute(
                    select(*key)
                    .where(age < cutoff, model.state.in_(terminal))
                    .limit(1000)
                )
            ).all()
            if rows:
                await session.execute(delete(model).where(tuple_(*key).in_(rows)))
        await session.commit()
