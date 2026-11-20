"""失败 Bot 自动重载回归测试

启动加载只跑一次，网络抖动导致的加载失败此前会死到下次重启
（2026-11-18：bot 232 积压 11 分钟无人处理即此因）。
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Message, Update, User
from telegram.ext import ApplicationBuilder, ExtBot

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


async def test_startup_reports_quota_rejection(monkeypatch, caplog):
    from db import bots, vip

    monkeypatch.setattr(vip, 'get_max_bots_for_user', AsyncMock(return_value=1))
    monkeypatch.setattr(bots, 'get_user_bots_by_owner', AsyncMock(return_value=[
        {'id': 1, 'status': 'active'},
        {'id': 162, 'status': 'active'},
    ]))
    manager = BotManager()
    assert not await manager.start_bot({'id': 162, 'owner_id': 1, 'bot_username': 'testbot'})
    assert '@testbot (db_id=162)' in caplog.text
    assert '配额已满（其他活跃 1 / 上限 1）' in caplog.text


async def test_startup_reports_stage_and_blank_timeout(monkeypatch, caplog):
    manager = BotManager()

    def fail_initialize(token):
        raise TimeoutError()

    monkeypatch.setattr(manager, '_create_user_bot_app', fail_initialize)
    assert not await manager._start_bot(
        {'id': 162, 'bot_token': 'test-token', 'bot_username': 'testbot'}, max_retries=0,
    )
    assert '初始化 Telegram Bot' in caplog.text
    assert 'TimeoutError' in caplog.text


@pytest.mark.parametrize('delivery', ['polling', 'webhook'])
@pytest.mark.parametrize('startup_ok', [True, False])
async def test_platform_replies_during_admin_bot_start(database, monkeypatch, delivery, startup_ok):
    from db.core import get_session
    from db.models import UpdateInbox, UserBot
    from main import _register_master_handlers
    from services import inbox, runtime

    monkeypatch.setattr('config.ADMIN_IDS', [99])
    monkeypatch.setattr(inbox, 'INBOX_WORKERS', 1)
    async with get_session() as session:
        session.add(UserBot(id=162, owner_id=99, bot_id=200, bot_username='child_bot',
                            bot_token='test-child', status='admin_stopped'))
        await session.commit()

    starting = asyncio.Event()
    finish_start = asyncio.Event()

    async def start_bot(record):
        starting.set()
        await finish_start.wait()
        if not startup_ok:
            raise RuntimeError('startup failed')
        return True

    manager = SimpleNamespace(get_all_apps=dict, active_count=0, stop_bot=AsyncMock(),
                              start_bot=AsyncMock(side_effect=start_bot))
    runtime.configure(bot_manager=manager)

    async def get_me(bot, *args, **kwargs):
        user = User(123456, 'Test', True, username='test_bot')
        object.__setattr__(bot, '_bot_user', user)
        return user

    monkeypatch.setattr(ExtBot, 'get_me', get_me)
    replies = asyncio.Queue()
    status_msg = SimpleNamespace(edit_text=AsyncMock())

    async def reply(text, **kwargs):
        replies.put_nowait(text)
        return status_msg

    monkeypatch.setattr(Message, 'reply_text', AsyncMock(side_effect=reply))
    app = ApplicationBuilder().token('123456:test-master').concurrent_updates(False).build()
    _register_master_handlers(app)
    webhook = inbox.Inbox(lambda _: app)

    async def send_command(update_id, text):
        body = {
            'update_id': update_id,
            'message': {
                'message_id': update_id, 'date': 1, 'text': text,
                'from': {'id': 99, 'first_name': 'Admin', 'is_bot': False},
                'chat': {'id': 99, 'type': 'private'},
                'entities': [{'type': 'bot_command', 'offset': 0, 'length': len(text.split()[0])}],
            },
        }
        if delivery == 'webhook':
            await webhook.accept('master', body)
        else:
            await app.update_queue.put(Update.de_json(body, app.bot))
        return await asyncio.wait_for(replies.get(), 3)

    async with app:
        await app.start()
        if delivery == 'webhook':
            webhook.start()
        try:
            assert '正在启动 @child_bot' in await send_command(1, '/startbot @child_bot')
            await asyncio.wait_for(starting.wait(), 3)
            assert '平台统计' in await send_command(2, '/platform')
            if delivery == 'webhook':
                async with get_session() as session:
                    row = await session.get(UpdateInbox, ('master', 1))
                    assert (row.state, row.attempts) == ('completed', 1)
            assert '正在启停' in await send_command(3, '/startbot 162')
            assert '正在启停' in await send_command(4, '/stopbot 162')
            manager.start_bot.assert_awaited_once()
            manager.stop_bot.assert_awaited_once_with(162)
            status_msg.edit_text.assert_not_awaited()
        finally:
            finish_start.set()
            await webhook.stop()
            await app.stop()

    status_msg.edit_text.assert_awaited_once()
    expected = 'Bot 启动成功' if startup_ok else '启动失败'
    assert expected in status_msg.edit_text.call_args.args[0]
    assert not app.bot_data['admin_bot_operations']
