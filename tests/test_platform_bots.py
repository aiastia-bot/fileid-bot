from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telegram.error import BadRequest

from db.core import get_session
from db.models import Collection, FileMapping, UserBot
from handlers.master import admin


async def test_platform_bots_pages_edit_one_message_and_preserve_filters(database, monkeypatch):
    monkeypatch.setattr('config.ADMIN_IDS', [99])
    monkeypatch.setattr(admin, 'get_bot_manager', lambda: None)
    overview_query = AsyncMock()
    monkeypatch.setattr(admin, 'get_platform_stats', overview_query)

    page_size = admin.PLATFORM_BOTS_PAGE_SIZE
    async with get_session() as session:
        session.add_all([
            UserBot(id=i, bot_username=f'pagebot{i:02d}', bot_firstname='<文件 Bot>',
                    owner_id=99, bot_id=1000 + i, bot_token=f'test-{i}')
            for i in range(1, page_size + 2)
        ])
        session.add_all([
            UserBot(id=100, bot_username='emptybot', owner_id=99, bot_token='empty'),
            UserBot(id=101, bot_username='revokedbot', owner_id=99, bot_token='revoked', status='revoked'),
            UserBot(id=102, bot_username='deletedbot', owner_id=99, bot_token='deleted', status='deleted'),
            UserBot(id=103, bot_username='bannedbot', owner_id=99, bot_token='banned', status='banned'),
        ])
        session.add_all([
            FileMapping(bot_db_id=i, code=f'file{i}', file_type='photo',
                        telegram_file_id=f'id{i}', user_id=1, created_at='2000-01-01')
            for i in range(1, page_size + 2)
        ])
        session.add(FileMapping(bot_db_id=1, code='today', file_type='photo',
                                telegram_file_id='today', user_id=1, created_at=date.today().isoformat()))
        session.add(Collection(bot_db_id=1, code='collection', status='completed'))
        await session.commit()

    reply = AsyncMock()
    send_message = AsyncMock()
    message = SimpleNamespace(reply_text=reply)
    admin_user = SimpleNamespace(id=99)
    update = SimpleNamespace(effective_user=admin_user, message=message)
    context = SimpleNamespace(args=['bots'], bot=SimpleNamespace(send_message=send_message))
    await admin.platform_stats_cmd(update, context)

    reply.assert_awaited_once()
    first_page = reply.call_args.args[0]
    assert f'第 1/2 页 · 共 {page_size + 1} 个' in first_page
    assert '@pagebot01' in first_page and '&lt;文件 Bot&gt;' in first_page
    assert '文件 2 · 今日 +1 · 集合 1 · 用户 1' in first_page
    assert '@emptybot' not in first_page
    next_page = reply.call_args.kwargs['reply_markup'].inline_keyboard[0][-1]

    query = SimpleNamespace(data=next_page.callback_data, message=message, from_user=admin_user,
                            answer=AsyncMock(), edit_message_text=AsyncMock())
    callback_update = SimpleNamespace(callback_query=query)
    await admin.platform_bots_callback(callback_update, context)
    query.edit_message_text.assert_awaited_once()
    second_page = query.edit_message_text.call_args.args[0]
    assert '第 2/2 页' in second_page
    assert f'@pagebot{page_size + 1:02d}' in second_page and '@pagebot01' not in second_page

    for status, username in [('revoked', 'revokedbot'), ('banned', 'bannedbot'), ('all', 'emptybot')]:
        query.data = f'platform_bots|{status}|1'
        await admin.platform_bots_callback(callback_update, context)
        filtered = query.edit_message_text.call_args.args[0]
        assert f'@{username}' in filtered and '@deletedbot' not in filtered

    send_message.assert_not_awaited()
    overview_query.assert_not_awaited()


async def test_platform_bots_callback_checks_admin_and_tolerates_unchanged_page(monkeypatch):
    monkeypatch.setattr('config.ADMIN_IDS', [99])
    page_query = AsyncMock(return_value=([], 0, 1))
    monkeypatch.setattr(admin, 'get_platform_bot_page', page_query)
    query = SimpleNamespace(
        data='platform_bots|active|1', message=object(), from_user=SimpleNamespace(id=7),
        answer=AsyncMock(), edit_message_text=AsyncMock(side_effect=BadRequest('Message is not modified')),
    )
    await admin.platform_bots_callback(SimpleNamespace(callback_query=query), None)
    assert query.answer.call_args.kwargs['show_alert'] is True
    page_query.assert_not_awaited()
    query.edit_message_text.assert_not_awaited()

    query.from_user.id = 99
    await admin.platform_bots_callback(SimpleNamespace(callback_query=query), None)
    assert '暂无有文件记录的活跃 Bot' in query.edit_message_text.call_args.args[0]
