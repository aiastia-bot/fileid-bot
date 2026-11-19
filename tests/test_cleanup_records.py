"""cleanup_records 节奏回归测试

2026-11-19 发现：旧实现每日最多删 1000 行（单次 limit 无循环），追不上
日增数千条 completed，update_inbox 积累到 9.6 万行成为 CPU 100% 事故
的燃料。修复后循环清到无剩余；failed/pending 永不清理。
"""
import time

import pytest
from sqlalchemy import select

import services.maintenance as maintenance
from db.core import get_session
from db.models import SendJob, UpdateInbox
from services.maintenance import cleanup_records


@pytest.fixture
def tiny_batch(monkeypatch):
    """批大小压到 5，用 12 行数据就能跑通多批循环"""
    monkeypatch.setattr(maintenance, "CLEANUP_BATCH", 5)


async def _seed_inbox(update_id, state, created_at):
    async with get_session() as session:
        session.add(UpdateInbox(
            bot_key="b", update_id=update_id, node_id="standalone", body="{}",
            state=state, attempts=0, available_at=0, error="", created_at=created_at,
        ))
        await session.commit()


async def test_cleanup_removes_all_expired_in_batches(database, tiny_batch):
    now = int(time.time())
    old = now - (30 + 2) * 86400  # DATA_RETENTION_DAYS 默认 30 天
    for i in range(12):           # 12 条超期 completed → 3 批清完
        await _seed_inbox(1000 + i, "completed", old + i)
    await _seed_inbox(2000, "completed", now - 100)   # 未超期：保留
    await _seed_inbox(2001, "failed", old)            # failed：保留供排查
    await _seed_inbox(2002, "pending", old)           # pending：永不清理
    async with get_session() as session:
        session.add(SendJob(
            task_id="t1", bot_name="b", chat_id=1, body="{}",
            state="cancelled", attempts=0, error="", created_at=old, updated_at=old,
        ))
        await session.commit()

    removed = await cleanup_records()

    assert removed == 13  # 12 条超期 completed + 1 条超期 cancelled SendJob
    async with get_session() as session:
        states = dict(
            (row.update_id, row.state)
            for row in (await session.execute(select(UpdateInbox))).scalars()
        )
        assert set(states) == {2000, 2001, 2002}
        assert await session.get(SendJob, "t1") is None  # 超期 cancelled 被清


async def test_cleanup_noop_when_nothing_expired(database):
    now = int(time.time())
    await _seed_inbox(1, "completed", now - 100)
    assert await cleanup_records() == 0
    async with get_session() as session:
        assert len((await session.execute(select(UpdateInbox))).scalars().all()) == 1
