"""失败 Bot 自动重载回归测试

启动加载只跑一次，网络抖动导致的加载失败此前会死到下次重启
（2026-11-18：bot 232 积压 11 分钟无人处理即此因）。
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import bot_manager as bm_module
from bot_manager import BotManager


async def test_retry_only_missing_bots(database, monkeypatch):
    """只重试「活跃但未加载」的 Bot，不动已在运行的"""
    manager = BotManager()
    monkeypatch.setattr(
        bm_module, "get_all_active_user_bots",
        AsyncMock(return_value=[
            {"id": 1, "bot_username": "loaded"},
            {"id": 2, "bot_username": "missing"},
        ]),
    )
    started = []

    async def fake_start(bot_record):
        started.append(bot_record["id"])
        return bot_record["id"] == 2   # 2 号成功，1 号（已在跑）不会进来

    monkeypatch.setattr(manager, "start_bot", fake_start)
    manager._apps[1] = SimpleNamespace()   # 1 号已在运行

    recovered = await manager.retry_failed_bots()

    assert recovered == 1
    assert started == [2]


async def test_retry_noop_when_all_loaded(database, monkeypatch):
    """全部就绪时不做任何启动动作"""
    manager = BotManager()
    monkeypatch.setattr(
        bm_module, "get_all_active_user_bots",
        AsyncMock(return_value=[{"id": 1, "bot_username": "loaded"}]),
    )
    started = []

    async def fake_start(bot_record):
        started.append(bot_record["id"])
        return True

    monkeypatch.setattr(manager, "start_bot", fake_start)
    manager._apps[1] = SimpleNamespace()

    assert await manager.retry_failed_bots() == 0
    assert started == []


async def test_retry_skipped_while_initial_load(database, monkeypatch):
    """初次加载进行中（_loading=True）不并发重试"""
    manager = BotManager()
    manager._loading = True
    called = AsyncMock()
    monkeypatch.setattr(bm_module, "get_all_active_user_bots", called)

    assert await manager.retry_failed_bots() == 0
    called.assert_not_awaited()
