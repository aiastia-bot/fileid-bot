"""Run in CI services; explicitly opt in with dedicated disposable endpoints."""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text, select, func
from sqlalchemy.engine import make_url


@pytest_asyncio.fixture
async def mysql_database(monkeypatch):
    url = os.environ.get("TEST_MYSQL_URL")
    if not url:
        pytest.skip("TEST_MYSQL_URL is not configured; no live MySQL was tested")
    parsed = make_url(url)
    assert parsed.database and parsed.database.startswith("fileid_test"), (
        "Only disposable fileid_test* databases are allowed"
    )
    import db.core as core
    from db.models import Base

    await core.close_db()
    monkeypatch.setattr(core, "DB_TYPE", "mysql")
    monkeypatch.setattr(core, "DATABASE_URL", url)
    async with core.get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await core.init_db()
    yield
    async with core.get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await core.close_db()


async def test_mysql_payment_idempotency_and_triggers(mysql_database):
    from db.core import get_session
    from db.models import StarPayment, UserBot
    from services.payments import create_order, receive_payment, fulfill_order
    from db.vip import get_user_vip_info
    from db.files import save_file, get_bot_file_count

    uid = 1234567890123
    order = await create_order(uid, "vip", 1, 1)
    await receive_payment(order["payload"], uid, order["amount"], "XTR", "mysql-charge")
    transaction = {
        "id": "mysql-charge",
        "amount": order["amount"],
        "source": {
            "type": "user",
            "user": {"id": uid},
            "transaction_type": "invoice_payment",
            "invoice_payload": order["payload"],
        },
    }
    bot = SimpleNamespace(
        get_star_transactions=AsyncMock(return_value={"transactions": [transaction]}),
        send_message=AsyncMock(),
    )
    await asyncio.gather(
        fulfill_order(bot, order["payload"]), fulfill_order(bot, order["payload"])
    )
    assert (await get_user_vip_info(uid))["vip_level"] == 1
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(StarPayment)) == 1
        session.add(UserBot(id=1, owner_id=uid, bot_token="test-token"))
        await session.commit()
    args = (
        uid,
        "photo",
        "long-telegram-file-id",
        5_000_000_000,
        "same-id",
        "testbot",
        "test",
        1,
    )
    codes = await asyncio.gather(save_file(*args), save_file(*args))
    assert codes[0] and codes[0] == codes[1]
    assert await get_bot_file_count(1) == 1


async def test_mysql_legacy_integer_upgrade(mysql_database):
    import db.core as core

    async with core.get_engine().begin() as conn:
        await conn.execute(
            text("ALTER TABLE user_bots MODIFY COLUMN owner_id INTEGER NOT NULL")
        )
        await conn.execute(text("DELETE FROM schema_versions WHERE version=1"))
    await core.init_db()
    async with core.get_engine().begin() as conn:
        columns = await conn.run_sync(lambda c: inspect(c).get_columns("user_bots"))
        assert (
            str(next(c["type"] for c in columns if c["name"] == "owner_id")) == "BIGINT"
        )


async def test_real_redis_legacy_import(database, monkeypatch):
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.skip("TEST_REDIS_URL is not configured; no live Redis was tested")
    import redis.asyncio as redis
    from send_queue import SendTask, SendQueue
    from db.core import get_session
    from db.models import SendJob
    import uuid

    name = "fileid_test_" + uuid.uuid4().hex
    client = redis.from_url(url, decode_responses=True)
    task = SendTask(1, [{"file_type": "photo"}])
    key = f"sq:{name}"
    try:
        await client.rpush(key, task.to_json())
        q = SendQueue(name)
        q._redis = SimpleNamespace(_redis=client)
        await q._restore_from_redis()
        assert await client.llen(key) == 0
        async with get_session() as session:
            assert await session.get(SendJob, task.task_id)
        assert q.pending == 1
    finally:
        await client.delete(key)
        await client.aclose()
