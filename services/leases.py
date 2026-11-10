"""Database leases keep a bot/queue on one process during worker handover."""

import time
import uuid
from sqlalchemy import update, delete, or_
from sqlalchemy.exc import IntegrityError
from db.core import get_session
from db.models import RuntimeLease

PROCESS_ID = uuid.uuid4().hex


async def acquire(key, owner=PROCESS_ID, ttl=120):
    now = int(time.time())
    async with get_session() as session:
        changed = await session.execute(
            update(RuntimeLease)
            .where(
                RuntimeLease.key == key,
                or_(RuntimeLease.owner == owner, RuntimeLease.expires_at <= now),
            )
            .values(owner=owner, expires_at=now + ttl)
        )
        if changed.rowcount:
            await session.commit()
            return True
        session.add(RuntimeLease(key=key, owner=owner, expires_at=now + ttl))
        try:
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            return False


async def release(key, owner=PROCESS_ID):
    async with get_session() as session:
        await session.execute(
            delete(RuntimeLease).where(
                RuntimeLease.key == key, RuntimeLease.owner == owner
            )
        )
        await session.commit()
