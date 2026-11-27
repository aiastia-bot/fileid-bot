from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from telegram.error import Forbidden, TelegramError

from db.core import get_engine, get_session, init_db
from db.models import UserBot, UserNotice
from handlers.master import gifts, manage, membership_admin
from services import notices
from utils import user_id_html


OWNER = 8944900278
OWNER_LINK = f'<a href="tg://user?id={OWNER}">{OWNER}</a>'


def test_user_id_html():
    assert user_id_html(OWNER) == OWNER_LINK
    assert user_id_html(str(OWNER)) == OWNER_LINK
    assert user_id_html(None) == '—'
    assert user_id_html(-100123) == '-100123'
    assert user_id_html('<unknown>') == '&lt;unknown&gt;'


async def test_notice_migration_preserves_plain_delivery_and_supports_html(database):
    async with get_engine().begin() as conn:
        await conn.execute(text('ALTER TABLE user_notices DROP COLUMN parse_mode'))
        await conn.execute(text('DELETE FROM schema_versions WHERE version=7'))
        await conn.execute(text(
            "INSERT INTO user_notices (key,user_id,body,state,available_at,attempts) "
            "VALUES ('legacy',99,'plain <text> & old','pending',0,0)"
        ))
    await init_db()
    await init_db()

    await notices.enqueue('html', 99, f'主人 {OWNER_LINK}', parse_mode='HTML')
    await notices.enqueue('html', 99, 'duplicate')
    await notices.enqueue('plain', 99, 'plain <text> & new')
    bot = SimpleNamespace(send_message=AsyncMock())
    await notices.deliver(bot)
    calls = {call.kwargs['text']: call.kwargs for call in bot.send_message.await_args_list}
    assert calls == {
        'plain <text> & old': {'chat_id': 99, 'text': 'plain <text> & old'},
        'plain <text> & new': {'chat_id': 99, 'text': 'plain <text> & new'},
        f'主人 {OWNER_LINK}': {'chat_id': 99, 'text': f'主人 {OWNER_LINK}', 'parse_mode': 'HTML'},
    }


async def test_notice_retry_preserves_html(database, monkeypatch):
    monkeypatch.setattr(notices, 'time', SimpleNamespace(time=lambda: 1000))
    await notices.enqueue('retry', 99, OWNER_LINK, parse_mode='HTML')
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=RuntimeError('temporary')))
    await notices.deliver(bot)
    async with get_session() as session:
        row = await session.get(UserNotice, 'retry')
        assert (row.state, row.available_at, row.attempts, row.parse_mode) == ('pending', 1060, 1, 'HTML')

    monkeypatch.setattr(notices, 'time', SimpleNamespace(time=lambda: 1060))
    bot.send_message.side_effect = Forbidden('blocked')
    await notices.deliver(bot)
    bot.send_message.assert_awaited_with(chat_id=99, text=OWNER_LINK, parse_mode='HTML')
    async with get_session() as session:
        row = await session.get(UserNotice, 'retry')
        assert (row.state, row.attempts) == ('blocked', 2)


async def test_admin_cleanup_and_revoked_notices_link_owner(database, monkeypatch):
    from bot_manager import _auto_stop_revoked_bot
    from db import set_platform_setting
    from services.maintenance import auto_purge_dead_bots

    monkeypatch.setattr('config.ADMIN_IDS', [99])
    monkeypatch.setattr('config.AUTO_PURGE_DEAD_BOT_DAYS', 7)
    monkeypatch.setattr('config.AUTO_PURGE_WARN_BEFORE_DAYS', ())
    monkeypatch.setattr('bot_manager.asyncio', SimpleNamespace(sleep=AsyncMock()))
    async with get_session() as session:
        session.add(UserBot(id=1, bot_username='a&b', owner_id=OWNER, bot_token='test-token'))
        await session.commit()
    await _auto_stop_revoked_bot('a&b', {'bot_record': {'id': 1, 'owner_id': OWNER}})
    async with get_session() as session:
        bot = await session.get(UserBot, 1)
        bot.updated_at = (datetime.now() - timedelta(days=8)).strftime('%Y-%m-%d %H:%M:%S')
        await session.commit()
    await set_platform_setting('auto_purge_baseline', '2000-01-01 00:00:00')
    assert await auto_purge_dead_bots() == 1
    async with get_session() as session:
        rows = {r.key: (r.user_id, r.body, r.parse_mode)
                for r in (await session.scalars(select(UserNotice))).all()}
    for key in ('revoked:1:99', 'autopurge_admin:1:99'):
        recipient, body, mode = rows[key]
        assert (recipient, mode) == (99, 'HTML')
        assert OWNER_LINK in body and '@a&amp;b' in body
    recipient, body, mode = rows['autopurge:1:0']
    assert (recipient, mode) == (OWNER, None)
    assert '@a&b' in body and OWNER_LINK not in body


async def test_purge_reply_links_backup_owner(monkeypatch):
    monkeypatch.setattr(manage, 'get_user_bot_by_id', AsyncMock(return_value={'bot_username': 'a&b'}))
    monkeypatch.setattr('db.bot_purge_preview', AsyncMock(return_value={'files': 1}))
    monkeypatch.setattr('db.purge_bot_files', AsyncMock(return_value={
        'deleted_files': 1, 'removed_items': 0, 'fixed_collections': 0,
    }))
    monkeypatch.setattr('db.remove_bot_from_backup', AsyncMock(return_value=OWNER))
    query = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock())
    await manage._purge_bot(SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=99)), 1)
    result = query.edit_message_text.await_args
    assert f'主人 {OWNER_LINK}' in result.args[0]
    assert '@a&amp;b' in result.args[0]
    assert result.kwargs['parse_mode'] == 'HTML'


async def test_orders_with_long_details_keep_links_and_escape_html(monkeypatch):
    monkeypatch.setattr(membership_admin, 'ADMIN_IDS', [99])
    rows = [dict(payload='order<&', user_id=OWNER + i, product='vip', amount=10,
                 status='review', error='&' * 850 + '<tail>') for i in range(3)]
    monkeypatch.setattr(membership_admin, 'list_orders', AsyncMock(return_value=rows))
    reply = AsyncMock()
    update = SimpleNamespace(effective_user=SimpleNamespace(id=99),
                             effective_message=SimpleNamespace(reply_text=reply))
    await membership_admin.vipadmin_command(update, SimpleNamespace(args=['orders']))
    messages = [call.args[0] for call in reply.await_args_list]
    assert len(messages) > len(rows)
    assert all(len(message) <= 3900 for message in messages)
    assert all(call.kwargs['parse_mode'] == 'HTML' for call in reply.await_args_list)
    for i in range(3):
        assert any(user_id_html(OWNER + i) in message for message in messages)
    joined = '\n'.join(messages)
    assert joined.count('&amp;') == 3 * 851
    assert joined.count('&lt;tail&gt;') == 3


async def test_stars_transaction_links_nested_users_and_keeps_recipient_action(monkeypatch):
    source = {'type': 'user', 'user': {'id': OWNER, 'first_name': '<name>'}}
    tx = {'amount': 1, 'source': source}
    assert f'&lt;name&gt; ({OWNER_LINK})' in gifts._format_transaction(1, tx)
    assert OWNER_LINK in gifts._format_transaction(2, {'amount': -1, 'receiver': source})
    assert OWNER_LINK in gifts._format_transaction(3, {'amount': 1, 'source': {'type': 'user', 'user_id': OWNER}})
    monkeypatch.setattr(gifts, '_get_star_transactions', AsyncMock(return_value=([tx, tx], [])))
    recent = await gifts._get_recent_paying_users(object())
    keyboard = gifts._build_recipient_keyboard(recent, 'user', 'input', 'back')
    assert recent == [{'user_id': OWNER, 'first_name': '<name>'}]
    assert keyboard.inline_keyboard[0][0].callback_data == f'user|{OWNER}'


@pytest.mark.parametrize('premium', [False, True])
@pytest.mark.parametrize('fails', [False, True])
async def test_gift_progress_and_result_link_recipient(premium, fails):
    status = SimpleNamespace(edit_text=AsyncMock())
    reply = AsyncMock(return_value=status)
    send = AsyncMock(side_effect=TelegramError('bad <gift>') if fails else None)
    update = SimpleNamespace(message=SimpleNamespace(reply_text=reply))
    context = SimpleNamespace(
        user_data={'selected_gift_id': '1', 'selected_gift_name': 'A&B', 'selected_gift_stars': 1,
                   'selected_premium_months': 3, 'selected_premium_stars': 1000},
        bot=SimpleNamespace(send_gift=send, gift_premium_subscription=send),
    )
    await (gifts._do_send_premium if premium else gifts._do_send_gift)(update, context, OWNER)
    assert send.await_args.kwargs['user_id'] == OWNER
    for call in (reply.await_args, status.edit_text.await_args):
        assert call.kwargs['parse_mode'] == 'HTML'
        assert OWNER_LINK in call.args[0]
    if fails:
        assert 'bad &lt;gift&gt;' in status.edit_text.await_args.args[0]
