import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import select, func
from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateTable

from db.core import get_session, init_db
from db.models import Base, UpdateInbox, SendJob, FileMapping, BotFileCount, UserBot
from services.inbox import Inbox, note_error
from services.jobs import save_task, restore_tasks
from services.leases import acquire, release
from send_queue import SendQueue, SendTask, QueueFullError


async def until(predicate, timeout=3):
    async with asyncio.timeout(timeout):
        while not await predicate():
            await asyncio.sleep(0.01)


async def state(model, key):
    async with get_session() as session:
        row = await session.get(model, key)
        return row.state if row else None


async def test_inbox_duplicate_and_restart(database):
    fake = SimpleNamespace(bot=None, process_update=AsyncMock())
    first = Inbox(lambda key: None)
    await asyncio.gather(
        first.accept("master", {"update_id": 42}),
        first.accept("master", {"update_id": 42}),
    )
    async with get_session() as session:
        assert await session.scalar(select(func.count()).select_from(UpdateInbox)) == 1
    restarted = Inbox(lambda key: fake)
    restarted.start()
    try:

        async def done():
            return await state(UpdateInbox, ("master", 42)) == "completed"

        await until(done)
        await restarted.accept("master", {"update_id": 42})
        await asyncio.sleep(0.05)
        fake.process_update.assert_awaited_once()
    finally:
        await restarted.stop()


async def test_ptb_swallowed_error_is_retryable(database):
    async def handler(update):
        note_error(RuntimeError("handler failed"))

    inbox = Inbox(lambda key: SimpleNamespace(bot=None, process_update=handler))
    await inbox.accept("master", {"update_id": 8})
    inbox.start()
    try:

        async def failed_once():
            async with get_session() as session:
                row = await session.get(UpdateInbox, ("master", 8))
                return row.state == "pending" and row.attempts == 1

        await until(failed_once)
    finally:
        await inbox.stop()


async def test_database_lease_fences_workers(database):
    assert await acquire("bot:1", owner="first")
    assert not await acquire("bot:1", owner="second")
    await release("bot:1", owner="second")
    assert not await acquire("bot:1", owner="second")
    await release("bot:1", owner="first")
    assert await acquire("bot:1", owner="second")


async def test_send_restart_without_redis(database, monkeypatch):
    task = SendTask(1, [{"file_type": "photo", "telegram_file_id": "x"}])
    await save_task("bot", task)
    fake_send = AsyncMock(return_value=1)
    monkeypatch.setattr("senders.send_batch", fake_send)
    q = SendQueue("bot")
    q.set_bot(SimpleNamespace())
    q.start()
    try:

        async def done():
            return await state(SendJob, task.task_id) == "completed"

        await until(done)
        fake_send.assert_awaited_once()
        assert await restore_tasks("bot") == []
    finally:
        await q.stop()


async def test_shutdown_preserves_inflight_job(database, monkeypatch):
    started = asyncio.Event()

    async def long_send(*args, **kwargs):
        started.set()
        await asyncio.sleep(100)

    monkeypatch.setattr("senders.send_batch", long_send)
    q = SendQueue("bot")
    q.set_bot(SimpleNamespace())
    q.start()
    task = q.submit_batch_async(1, [{"file_type": "photo"}])
    await asyncio.wait_for(started.wait(), 3)
    await q.stop()
    assert await state(SendJob, task.task_id) in ("pending", "processing")
    assert len(await restore_tasks("bot")) == 1


async def test_partial_send_goes_to_failed_not_replayed(database, monkeypatch):
    monkeypatch.setattr("senders.send_batch", AsyncMock(return_value=1))
    q = SendQueue("bot")
    q.set_bot(SimpleNamespace())
    q.start()
    task = q.submit_batch_async(1, [{"file_type": "photo"}, {"file_type": "photo"}])
    try:
        assert await asyncio.wait_for(task.future, 3) == 1

        async def failed():
            return await state(SendJob, task.task_id) == "failed"

        await until(failed)
        assert await restore_tasks("bot") == []
    finally:
        await q.stop()


async def test_queue_backpressure(monkeypatch):
    monkeypatch.setattr("send_queue.SEND_QUEUE_MAX_PER_USER", 1)
    q = SendQueue("bot", durable=False)
    q.start()
    try:
        q.submit_batch_async(1, [{"file_type": "photo"}])
        with pytest.raises(QueueFullError):
            q.submit_batch_async(1, [{"file_type": "photo"}])
    finally:
        await q.stop()


async def test_worker_numeric_route_and_secret(database, monkeypatch):
    from worker_server import WorkerServer

    monkeypatch.setattr("services.security.WEBHOOK_SECRET", "test-webhook-secret")
    worker = WorkerServer()
    worker.set_bot_manager(
        SimpleNamespace(active_count=0, _loading=False, get_all_apps=lambda: {})
    )
    worker._inbox = Inbox(lambda key: None)
    app = worker.create_app()
    app.on_startup.clear()
    app.on_shutdown.clear()
    async with TestClient(TestServer(app)) as client:
        assert (await client.post("/webhook/123", json={"update_id": 1})).status == 403
        response = await client.post(
            "/webhook/123",
            json={"update_id": 1},
            headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"},
        )
        assert response.status == 200
        assert (await client.post("/webhook/int", json={})).status == 404
        assert (
            await client.post("/internal/stop", json={"bot_db_id": 1})
        ).status == 403
        assert (await client.get("/health")).status == 200
        assert (await client.get("/ready")).status == 200


async def test_master_worker_status_awaited(database, monkeypatch):
    from webhook_server import _register_master_routes

    monkeypatch.setattr("services.security.WORKER_SECRET", "internal-secret")
    scheduler = SimpleNamespace(
        get_worker_status=AsyncMock(return_value=[{"node_id": "w1"}])
    )
    app = web.Application()
    _register_master_routes(app, scheduler)
    async with TestClient(TestServer(app)) as client:
        response = await client.get(
            "/internal/workers", headers={"X-Worker-Secret": "internal-secret"}
        )
        assert response.status == 200
        assert (await response.json())["workers"] == [{"node_id": "w1"}]
        scheduler.get_worker_status.assert_awaited_once()


def test_mysql_schema_compiles():
    for table in Base.metadata.sorted_tables:
        assert str(CreateTable(table).compile(dialect=mysql.dialect()))


async def test_file_dedup_and_count_concurrent(database):
    from db.files import save_file, get_bot_file_count

    async with get_session() as session:
        session.add(UserBot(id=1, owner_id=1, bot_token="test"))
        await session.commit()
    args = (1, "photo", "fileid", 10, "same-identity", "testbot", "test", 1)
    codes = await asyncio.gather(save_file(*args), save_file(*args))
    assert codes[0] == codes[1] and codes[0]
    assert await get_bot_file_count(1) == 1


async def test_migration_claim_concurrent(database):
    from db.migrations import create_migration, claim_migration
    from services.membership import adjust_member
    from db.vip import get_user_vip_info

    await adjust_member(9, 1, level=1, months=1)
    info = await get_user_vip_info(1)
    migration = await create_migration(1, 1, info["vip_expire_at"])
    results = await asyncio.gather(
        claim_migration(migration["code"], 2), claim_migration(migration["code"], 3)
    )
    assert sorted(r[0] for r in results) == ["invalid", "ok"]


async def test_legacy_schema_preserves_codes(tmp_path, monkeypatch):
    import db.core as core

    await core.close_db()
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE file_mappings (id INTEGER PRIMARY KEY, code TEXT UNIQUE, bot_db_id INTEGER, file_unique_id TEXT)"
        )
        conn.executemany(
            "INSERT INTO file_mappings VALUES (?, ?, ?, ?)",
            [(1, "old-a", 7, "same"), (2, "old-b", 7, "same")],
        )
    monkeypatch.setattr(core, "DB_PATH", str(path))
    try:
        await init_db()
        await init_db()
        async with get_session() as session:
            rows = (
                (await session.execute(select(FileMapping).order_by(FileMapping.id)))
                .scalars()
                .all()
            )
            assert [r.code for r in rows] == ["old-a", "old-b"]
            assert [r.file_unique_id for r in rows] == ["same", None]
            assert (await session.get(BotFileCount, 7)).count == 2
    finally:
        await core.close_db()


def test_backup_restore_check(tmp_path):
    from scripts.backup import backup

    source, dest = tmp_path / "source.db", tmp_path / "backup.db"
    with sqlite3.connect(source) as conn:
        conn.execute("CREATE TABLE example (id INTEGER)")
        conn.execute("INSERT INTO example VALUES (42)")
    assert backup(source, dest) == 1
    with sqlite3.connect(dest) as conn:
        assert conn.execute("SELECT id FROM example").fetchone()[0] == 42
    with pytest.raises(ValueError):
        backup(source, dest)


async def test_legacy_redis_recovery_persists_before_removal(database, monkeypatch):
    import fakeredis.aioredis

    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    task = SendTask(1, [{"file_type": "photo"}])
    await client.rpush("sq:bot", task.to_json())
    q = SendQueue("bot")
    q._redis = SimpleNamespace(_redis=client)

    async def unavailable(*args):
        raise RuntimeError("Database temporarily unavailable")

    with monkeypatch.context() as scoped:
        scoped.setattr("services.jobs.save_task", unavailable)
        with pytest.raises(RuntimeError):
            await q._restore_from_redis()
        assert await client.llen("sq:bot") == 1
    await q._restore_from_redis()
    assert await client.llen("sq:bot") == 0
    assert await state(SendJob, task.task_id) == "pending"
    await client.aclose()


async def test_rehome_preserves_accepted_updates(database):
    from services.inbox import rehome

    inbox = Inbox(lambda key: None)
    await inbox.accept("7", {"update_id": 1})
    await rehome("7", "new-worker")
    async with get_session() as session:
        row = await session.get(UpdateInbox, ("7", 1))
        assert row.node_id == "new-worker" and row.state == "pending"


async def test_strict_quota_is_atomic(database, monkeypatch):
    from services.membership import set_plan
    from db.files import save_file, get_bot_file_count

    monkeypatch.setattr("config.FILE_QUOTA_POLICY", "reject")
    await set_plan(99, 0, {"file_limit": 1})
    async with get_session() as session:
        session.add(UserBot(id=1, owner_id=1, bot_token="test-token"))
        await session.commit()
    first = await save_file(1, "photo", "file-a", 1, "a", "bot", "bot", 1)
    assert first
    assert await save_file(1, "photo", "file-a", 1, "a", "bot", "bot", 1) == first
    assert await save_file(1, "photo", "file-b", 1, "b", "bot", "bot", 1) is None
    assert await get_bot_file_count(1) == 1


async def test_webhook_uses_common_initialization_and_jobs(database, monkeypatch):
    from unittest.mock import MagicMock
    import main
    from webhook_server import _build_on_startup, _build_on_shutdown

    monkeypatch.setattr(main, "ADMIN_IDS", [])
    monkeypatch.setattr("webhook_server.WEBHOOK_SECRET", "test-webhook-secret")
    manager = SimpleNamespace(
        _loading=False,
        _ready=False,
        master_bot_username="",
        get_all_apps=lambda: {},
        load_all=AsyncMock(return_value=0),
        start_webhook_monitor=AsyncMock(),
        stop_all=AsyncMock(),
    )
    bot = SimpleNamespace(
        username="test_master", set_my_commands=AsyncMock(), set_webhook=AsyncMock()
    )
    app = SimpleNamespace(
        bot=bot,
        bot_data={"bot_manager": manager},
        initialize=AsyncMock(),
        start=AsyncMock(),
        stop=AsyncMock(),
        shutdown=AsyncMock(),
        post_init=main.post_init,
        post_stop=main.post_stop,
        post_shutdown=main.post_shutdown,
        job_queue=SimpleNamespace(run_repeating=MagicMock()),
    )
    webapp = {}
    await _build_on_startup(app, manager)(webapp)
    await webapp["startup_task"]
    assert manager._ready
    manager.load_all.assert_awaited_once()
    manager.start_webhook_monitor.assert_awaited_once()
    assert app.job_queue.run_repeating.call_count == 3
    bot.set_my_commands.assert_awaited_once()
    assert bot.set_webhook.call_args.kwargs["drop_pending_updates"] is False
    await _build_on_shutdown(app, manager)(webapp)
    app.stop.assert_awaited_once()
    manager.stop_all.assert_awaited_once()
    app.shutdown.assert_awaited_once()


async def test_queue_acceptance_precedes_sending(database, monkeypatch):
    send_started = asyncio.Event()

    async def send(*args, **kwargs):
        send_started.set()
        return 1

    monkeypatch.setattr("senders.send_batch", send)
    q = SendQueue("bot")
    q.set_bot(SimpleNamespace())
    q.start()
    try:
        task = q.submit_batch_async(1, [{"file_type": "photo"}])
        await task.accepted
        async with get_session() as session:
            assert await session.get(SendJob, task.task_id)
        await asyncio.wait_for(task.future, 3)
    finally:
        await q.stop()


async def test_null_legacy_identities_can_be_backfilled_without_losing_codes(database):
    async with get_session() as session:
        session.add(
            UserBot(
                id=7,
                owner_id=1,
                bot_token="legacy-token",
                bot_username="legacybot",
                created_at="2000-01-01 00:00:00",
            )
        )
        session.add(
            FileMapping(
                code="claimed",
                bot_db_id=7,
                bot_username="legacybot",
                file_unique_id="same",
                file_type="photo",
                telegram_file_id="a",
            )
        )
        session.add(
            FileMapping(
                code="unclaimed-a",
                bot_db_id=None,
                bot_username="legacybot",
                file_unique_id="same",
                file_type="photo",
                telegram_file_id="b",
            )
        )
        session.add(
            FileMapping(
                code="unclaimed-b",
                bot_db_id=None,
                bot_username="legacybot",
                file_unique_id="same",
                file_type="photo",
                telegram_file_id="c",
            )
        )
        await session.commit()
    await init_db()
    async with get_session() as session:
        rows = (
            (await session.execute(select(FileMapping).order_by(FileMapping.id)))
            .scalars()
            .all()
        )
        assert len(rows) == 3 and all(r.bot_db_id == 7 for r in rows)
        assert [r.file_unique_id for r in rows] == ["same", None, None]
        assert (await session.get(BotFileCount, 7)).count == 3


def test_credentials_are_redacted_from_logs():
    import logging
    from services.logging import RedactingFormatter

    token = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijk"
    record = logging.LogRecord(
        "test",
        logging.ERROR,
        "",
        0,
        "Failed https://api.telegram.org/bot%s/sendMessage mysql://user:password@host/db",
        (token,),
        None,
    )
    result = RedactingFormatter().format(record)
    assert token not in result and "password" not in result


async def test_full_worker_accepts_idempotent_start(database, monkeypatch):
    from worker_server import WorkerServer

    monkeypatch.setattr("services.security.WORKER_SECRET", "internal-secret")
    monkeypatch.setattr("worker_server.MAX_BOTS_PER_WORKER", 1)
    worker = WorkerServer()
    worker.set_bot_manager(
        SimpleNamespace(active_count=1, get_all_apps=lambda: {7: object()})
    )
    app = worker.create_app()
    app.on_startup.clear()
    app.on_shutdown.clear()
    async with TestClient(TestServer(app)) as client:
        response = await client.post(
            "/internal/start",
            json={"bot_db_id": 7, "bot_token": "existing"},
            headers={"X-Worker-Secret": "internal-secret"},
        )
        assert response.status == 200
        assert (await response.json())["already_running"] is True
