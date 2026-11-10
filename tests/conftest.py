"""No test reads/writes a user's configured database or Telegram credentials."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["DB_TYPE"] = "sqlite"
os.environ["DB_PATH"] = str(Path(tempfile.mkdtemp(prefix="fileid-tests-")) / "suite.db")
os.environ["REDIS_URL"] = ""
os.environ["SEND_MIN_INTERVAL"] = "0"
os.environ["SEND_BATCH_DELAY"] = "0"
os.environ["BOT_MIN_API_INTERVAL"] = "0"
os.environ["SEND_INDIVIDUAL_DELAY"] = "0"
os.environ["PTB_TIMEDELTA"] = "1"

import pytest_asyncio


@pytest_asyncio.fixture
async def database(tmp_path, monkeypatch):
    import db.core as core
    from redis_manager import RedisManager
    import redis_manager

    await core.close_db()
    monkeypatch.setattr(core, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(core, "DB_TYPE", "sqlite")
    monkeypatch.setattr(redis_manager, "_instance", RedisManager())
    await core.init_db()
    yield
    from send_queue import stop_all_queues
    from services import runtime

    await stop_all_queues()
    runtime.configure()
    await core.close_db()
