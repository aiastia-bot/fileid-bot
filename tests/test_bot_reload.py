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
    monkeypatch.setattr(bm_module, 'get_user_bot_by_id', AsyncMock(return_value={
        'id': 2, 'bot_username': 'missing', 'status': 'active',
    }))
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


async def test_retry_persistent_failure_backoff(database, monkeypatch, caplog):
    """持续失败的 Bot 日志退避：首轮 WARNING，之后 DEBUG，恢复后重新告警"""
    import logging

    manager = BotManager()
    monkeypatch.setattr(
        bm_module, "get_all_active_user_bots",
        AsyncMock(return_value=[{"id": 2, "bot_username": "flaky"}]),
    )
    monkeypatch.setattr(bm_module, 'get_user_bot_by_id', AsyncMock(return_value={
        'id': 2, 'bot_username': 'flaky', 'status': 'active',
    }))

    async def fake_start(record):
        return record['id'] in manager._apps   # 初始都失败，恢复后成功

    monkeypatch.setattr(manager, "start_bot", fake_start)

    with caplog.at_level(logging.INFO):
        await manager.retry_failed_bots()
    assert '重试加载 1 个未就绪的活跃 Bot' in caplog.text       # 有新面孔 → INFO
    assert '⏳ @flaky 加载失败' in caplog.text                  # 首轮失败 → WARNING

    caplog.clear()
    with caplog.at_level(logging.INFO):
        await manager.retry_failed_bots()
    assert '重试加载' not in caplog.text                        # 汇总降为 DEBUG
    assert 'flaky' not in caplog.text                          # 失败详情降为 DEBUG

    manager._apps[2] = SimpleNamespace()                        # 恢复加载
    with caplog.at_level(logging.INFO):
        recovered = await manager.retry_failed_bots()
    assert recovered == 0   # 已在 _apps 里，不再算 missing
    assert manager._retry_fail_streak == {}                  # 计数已清（本就不在 missing 里）


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


@pytest.fixture
def offline_master(monkeypatch):
    from main import _register_master_handlers
    from services import inbox

    monkeypatch.setattr('config.ADMIN_IDS', [99])
    monkeypatch.setattr('handlers.master.membership_admin.ADMIN_IDS', [99])
    monkeypatch.setattr(inbox, 'INBOX_WORKERS', 1)

    def build(delivery):
        async def get_me(bot, *args, **kwargs):
            user = User(123456, 'Test', True, username='test_bot')
            object.__setattr__(bot, '_bot_user', user)
            return user

        monkeypatch.setattr(ExtBot, 'get_me', get_me)
        replies = asyncio.Queue()
        status_msg = SimpleNamespace(edit_text=AsyncMock(), delete=AsyncMock())

        async def reply(text, **kwargs):
            replies.put_nowait(text)
            return status_msg

        monkeypatch.setattr(Message, 'reply_text', AsyncMock(side_effect=reply))
        app = ApplicationBuilder().token('123456:test-master').concurrent_updates(False).build()
        _register_master_handlers(app)
        webhook = inbox.Inbox(lambda _: app)

        async def send_command(update_id, text, user_id=99):
            body = {
                'update_id': update_id,
                'message': {
                    'message_id': update_id, 'date': 1, 'text': text,
                    'from': {'id': user_id, 'first_name': 'User', 'is_bot': False},
                    'chat': {'id': user_id, 'type': 'private'},
                },
            }
            if text.startswith('/'):
                body['message']['entities'] = [
                    {'type': 'bot_command', 'offset': 0, 'length': len(text.split()[0])},
                ]
            if delivery == 'webhook':
                await webhook.accept('master', body)
            else:
                await app.update_queue.put(Update.de_json(body, app.bot))
            return await asyncio.wait_for(replies.get(), 3)
        return app, webhook, status_msg, send_command

    return build


@pytest.mark.parametrize('delivery', ['polling', 'webhook'])
@pytest.mark.parametrize('startup_ok', [True, False])
async def test_platform_replies_during_admin_bot_start(database, offline_master, delivery, startup_ok):
    from db.core import get_session
    from db.models import UpdateInbox, UserBot
    from services import runtime

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

    app, webhook, status_msg, send_command = offline_master(delivery)

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
            assert '正在处理' in await send_command(3, '/startbot 162')
            assert '正在处理' in await send_command(4, '/stopbot 162')
            assert '正在处理' in await send_command(5, '/delbot @child_bot')
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
    assert not app.bot_data['background_operations']


@pytest.mark.parametrize('delivery', ['polling', 'webhook'])
async def test_platform_replies_while_broadcast_and_export_are_running(database, offline_master, monkeypatch, delivery):
    from handlers.master import admin

    release = asyncio.Event()
    broadcasting = asyncio.Event()
    uploading = asyncio.Event()

    async def send_message(**kwargs):
        broadcasting.set()
        await release.wait()

    async def send_document(**kwargs):
        uploading.set()
        await release.wait()

    broadcast = AsyncMock(side_effect=send_message)
    upload = AsyncMock(side_effect=send_document)
    monkeypatch.setattr(ExtBot, 'send_message', broadcast)
    monkeypatch.setattr(ExtBot, 'send_document', upload)
    monkeypatch.setattr(admin, 'get_all_owner_ids', AsyncMock(return_value=[101, 102]))
    app, webhook, status, send = offline_master(delivery)

    async with app:
        await app.start()
        if delivery == 'webhook':
            webhook.start()
        try:
            assert '正在广播' in await send(1, '/broadcast hello')
            await asyncio.wait_for(broadcasting.wait(), 3)
            assert '正在准备导出' in await send(2, '/export json')
            await asyncio.wait_for(uploading.wait(), 3)
            assert '平台统计' in await send(3, '/platform')
            assert '正在处理' in await send(4, '/broadcast hello')
        finally:
            release.set()
            await webhook.stop()
            await app.stop()

    assert [call.kwargs['chat_id'] for call in broadcast.call_args_list] == [101, 102]
    upload.assert_awaited_once()
    status.delete.assert_awaited_once()
    assert '广播完成' in status.edit_text.call_args.args[0]


async def test_newbot_pending_token_keeps_platform_responsive(database, offline_master, monkeypatch):
    from db import get_user_bot_by_telegram_id
    from services import runtime

    validating = asyncio.Event()
    finish_validation = asyncio.Event()

    async def get_child_info():
        validating.set()
        await finish_validation.wait()
        return User(222222, 'Child', True, username='child_bot')

    child = SimpleNamespace(get_me=AsyncMock(side_effect=get_child_info), shutdown=AsyncMock())
    monkeypatch.setattr('services.telegram_api.create_bot', lambda token: child)
    manager = SimpleNamespace(active_count=0, start_bot=AsyncMock(return_value=True))
    runtime.configure(bot_manager=manager)
    app, webhook, status, send = offline_master('webhook')

    async with app:
        await app.start()
        webhook.start()
        try:
            assert '创建新 Bot' in await send(1, '/newbot')
            assert '显示名称' in await send(2, 'child_bot')
            assert '创建信息确认' in await send(3, 'Child')
            assert 'Token 格式不正确' in await send(4, 'invalid')
            assert '正在校验 Token' in await send(5, '222222:' + 'A' * 30)
            await asyncio.wait_for(validating.wait(), 3)
            assert '平台统计' in await send(6, '/platform')
            assert '请等待处理结果' in await send(7, '/cancel')
            manager.start_bot.assert_not_awaited()
        finally:
            finish_validation.set()
            await webhook.stop()
            await app.stop()

    record = await get_user_bot_by_telegram_id(222222)
    assert record['owner_id'] == 99
    manager.start_bot.assert_awaited_once()
    assert '创建成功并已启动' in status.edit_text.call_args.args[0]


@pytest.mark.parametrize('delivery', ['polling', 'webhook'])
async def test_managed_bot_update_registers_bot_for_my_bots(database, offline_master, monkeypatch, delivery):
    from db import get_user_bot_by_telegram_id
    from services import runtime

    owner_id, bot_id = 101, 222222
    get_token = AsyncMock(return_value='222222:test-child')
    monkeypatch.setattr(ExtBot, 'get_managed_bot_token', get_token)
    attempted_start = asyncio.Event()

    async def start_bot(record):
        attempted_start.set()
        return False

    manager = SimpleNamespace(get_all_apps=dict, start_bot=AsyncMock(side_effect=start_bot))
    runtime.configure(bot_manager=manager)
    app, webhook, _, send = offline_master(delivery)
    body = {
        'update_id': 1,
        'managed_bot': {
            'user': {'id': owner_id, 'first_name': 'Owner', 'is_bot': False},
            'bot': {'id': bot_id, 'first_name': 'Child', 'is_bot': True, 'username': 'child_bot'},
        },
    }

    async with app:
        await app.start()
        if delivery == 'webhook':
            webhook.start()
        try:
            if delivery == 'webhook':
                await webhook.accept('master', body)
            else:
                await app.update_queue.put(Update.de_json(body, app.bot))
            await asyncio.wait_for(attempted_start.wait(), 3)
            assert '@child_bot' in await send(2, '/mybots', user_id=owner_id)
            assert '你还没有添加任何 Bot' in await send(3, '/mybots', user_id=102)
        finally:
            await webhook.stop()
            await app.stop()

    get_token.assert_awaited_once_with(user_id=bot_id)
    record = await get_user_bot_by_telegram_id(bot_id)
    assert record['owner_id'] == owner_id
    manager.start_bot.assert_awaited_once()


async def test_platform_replies_during_backup_and_plan_sync(database, offline_master, monkeypatch):
    from handlers.master import backup, membership_admin
    from services.membership import get_plan

    finish = asyncio.Event()
    backup_started = asyncio.Event()
    plan_started = asyncio.Event()

    async def sync_backup(owner):
        backup_started.set()
        await finish.wait()
        return '回填完成'

    async def sync_plan():
        plan_started.set()
        await finish.wait()

    monkeypatch.setattr(backup, 'BACKUP_ENABLED', True)
    monkeypatch.setattr(backup, 'ADMIN_IDS', [99])
    monkeypatch.setattr(backup, 'request_backup_sync', sync_backup)
    monkeypatch.setattr(membership_admin, 'reconcile_all', sync_plan)
    app, webhook, status, send = offline_master('webhook')

    async with app:
        await app.start()
        webhook.start()
        try:
            assert '正在发起存量回填' in await send(1, '/backup sync')
            await asyncio.wait_for(backup_started.wait(), 3)
            assert '正在同步 Bot 额度' in await send(2, '/setplan 2 max_bots 7')
            await asyncio.wait_for(plan_started.wait(), 3)
            assert '平台统计' in await send(3, '/platform')
            assert '正在处理' in await send(4, '/backup off')
            assert (await get_plan(2))['max_bots'] == 7
        finally:
            finish.set()
            await webhook.stop()
            await app.stop()

    status.edit_text.assert_awaited_once_with('回填完成')
    assert any('套餐已更新并记录审计' in call.args[0] for call in Message.reply_text.call_args_list)
