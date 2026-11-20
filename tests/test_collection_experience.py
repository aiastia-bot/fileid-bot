"""集合分页只读取一页，顺序、副本解析、失败重试和停止按钮保持正确。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import event, select, text

from db.core import get_engine, get_session, init_db
from db.models import Collection, CollectionItem, FileMapping
from db.collections import (
    batch_add_codes_to_collection, delete_collection,
    get_collection_files_for_bot, get_collection_files_page_for_bot,
    get_collection_items_codes, get_collection_summary_for_bot,
)
from handlers import callbacks


async def seed_collection(count=12):
    async with get_session() as session:
        session.add(Collection(code='col', name='测试', user_id=7, bot_db_id=1))
        session.add_all([
            FileMapping(code=f'code{i}', telegram_file_id=f'fid{i}', file_type='photo',
                        bot_db_id=1, file_unique_id=f'uid{i}', is_valid=1)
            for i in range(count)
        ])
        session.add_all([
            CollectionItem(collection_code='col', file_code=f'code{i}', sort_order=i)
            for i in range(count)
        ])
        await session.commit()


async def test_pagination_loads_only_one_page(database):
    await seed_collection(1000)
    loaded, statements = [], []

    def on_load(target, context):
        loaded.append(target.code)

    def on_execute(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(FileMapping, 'load', on_load)
    event.listen(get_engine().sync_engine, 'before_cursor_execute', on_execute)
    try:
        result = await get_collection_files_page_for_bot('col', 1, page=100)
    finally:
        event.remove(FileMapping, 'load', on_load)
        event.remove(get_engine().sync_engine, 'before_cursor_execute', on_execute)
    assert result['total'] == 1000 and result['total_pages'] == 200
    assert [row['code'] for row in result['files']] == [f'code{i}' for i in range(495, 500)]
    assert len(loaded) == 5
    assert len(statements) == 2
    assert 'LIMIT' in statements[-1]


async def test_collection_card_does_not_load_file_metadata(database):
    await seed_collection(1000)
    loaded = []

    def on_load(target, context):
        loaded.append(target.code)

    event.listen(FileMapping, 'load', on_load)
    try:
        summary = await get_collection_summary_for_bot('col', 1)
    finally:
        event.remove(FileMapping, 'load', on_load)
    assert summary == {'total': 1000, 'missing': 0, 'type_counts': {'photo': 1000}}
    assert not loaded


async def test_pagination_resolves_backups_and_preserves_duplicate_items(database):
    await seed_collection(4)
    async with get_session() as session:
        session.add(FileMapping(code='backup', telegram_file_id='local-fid',
                                file_type='photo', bot_db_id=2, file_unique_id='uid0'))
        session.add(CollectionItem(collection_code='col', file_code='code0', sort_order=4))
        # 第二个源代码指向同一份文件，应保留两个代码对应的集合项。
        session.add(FileMapping(code='alias', telegram_file_id='other-fid',
                                file_type='photo', bot_db_id=3, file_unique_id='uid0'))
        session.add(CollectionItem(collection_code='col', file_code='alias', sort_order=5))
        await session.commit()
    page = await get_collection_files_page_for_bot('col', 2, per_page=2)
    assert page['total'] == 3
    assert [row['telegram_file_id'] for row in page['files']] == ['local-fid'] * 2
    last = await get_collection_files_page_for_bot('col', 2, page=999, per_page=2)
    assert last['page'] == 2 and len(last['files']) == 1
    full, missing = await get_collection_files_for_bot('col', 2)
    assert len(full) == 3 and missing == 3
    assert await get_collection_summary_for_bot('col', 2) == {
        'total': 3, 'missing': 3, 'type_counts': {'photo': 3}}


async def test_invalid_and_legacy_files_have_consistent_pages(database):
    await seed_collection(4)
    async with get_session() as session:
        files = (await session.execute(select(FileMapping).order_by(FileMapping.id))).scalars().all()
        files[0].is_valid = 0
        files[1].bot_db_id = None
        await session.commit()
    for bot_id in (None, 1, 2):
        full, missing = await get_collection_files_for_bot('col', bot_id)
        result = await get_collection_files_page_for_bot('col', bot_id, page=-1)
        assert [f['code'] for f in result['files']] == [f['code'] for f in full]
        assert result['total'] == len(full) and result['page'] == 1
        summary = await get_collection_summary_for_bot('col', bot_id)
        assert summary['total'] == len(full) and summary['missing'] == missing
    empty = await get_collection_files_page_for_bot('missing', 1)
    assert empty == {'files': [], 'total': 0, 'page': 1, 'total_pages': 0}


async def test_batch_pack_preserves_input_order_and_bulk_delete(database):
    await seed_collection(5)
    async with get_session() as session:
        session.add(Collection(code='pack', name='打包', user_id=7, bot_db_id=1))
        await session.commit()
    result = await batch_add_codes_to_collection('pack', ['code3', 'code1', 'code3', 'missing', 'code2'], 1)
    assert result == {'added': 3, 'invalid': 1, 'duplicate': 0}
    assert await get_collection_items_codes('pack') == ['code3', 'code1', 'code2']
    assert await delete_collection('pack')
    assert not await get_collection_items_codes('pack')
    assert len((await get_collection_files_for_bot('col', 1))[0]) == 5


async def test_existing_database_receives_pagination_index(database):
    async with get_engine().begin() as conn:
        await conn.execute(text('DROP INDEX idx_ci_col_order'))
        await conn.execute(text('DELETE FROM schema_versions WHERE version=5'))
    await init_db()
    await init_db()
    async with get_engine().connect() as conn:
        indexes = (await conn.execute(text("PRAGMA index_list('collection_items')"))).all()
    assert 'idx_ci_col_order' in [row[1] for row in indexes]


def context_and_queue(monkeypatch, sent):
    context = SimpleNamespace(bot_data={'bot_record': {'id': 1}}, user_data={},
                              bot=SimpleNamespace(id=123, username='testbot', send_message=AsyncMock()))
    queue = SimpleNamespace(clear_chat_cancel=lambda chat: None,
                            submit_batch=AsyncMock(return_value=sent),
                            cancel_chat=lambda chat: None)
    monkeypatch.setattr(callbacks, 'get_queue_from_context', lambda ctx: queue)
    monkeypatch.setattr(callbacks, 'get_collection', AsyncMock(return_value={'name': '测试'}))
    monkeypatch.setattr(callbacks, 'get_collection_files_page_for_bot', AsyncMock(return_value={
        'files': [{'code': str(i)} for i in range(5)], 'total': 5, 'total_pages': 1, 'page': 1}))
    return context, queue


async def test_failed_page_can_be_retried_and_completed_page_is_not_resent(monkeypatch):
    context, queue = context_and_queue(monkeypatch, 0)
    await callbacks._send_paginated(context, 7, 'col', 'c1')
    assert not context.user_data['sent_pages_7_c1']
    message = context.bot.send_message.call_args.kwargs
    assert '未发完' in message['text']
    assert message['reply_markup'].inline_keyboard[0][0].text == '🔄 重试本页'
    queue.submit_batch.return_value = 5
    await callbacks._send_paginated(context, 7, 'col', 'c1')
    assert context.user_data['sent_pages_7_c1'] == {1}
    await callbacks._send_paginated(context, 7, 'col', 'c1')
    assert queue.submit_batch.await_count == 2


async def test_concurrent_page_clicks_submit_once(monkeypatch):
    context, queue = context_and_queue(monkeypatch, 5)
    started, release = asyncio.Event(), asyncio.Event()

    async def send(*args, **kwargs):
        started.set()
        await release.wait()
        return 5

    queue.submit_batch.side_effect = send
    task = asyncio.create_task(callbacks._send_paginated(context, 7, 'col', 'c1'))
    await started.wait()
    await callbacks._send_paginated(context, 7, 'col', 'c1')
    release.set()
    await task
    assert queue.submit_batch.await_count == 1
    assert not context.bot_data['_sending_tasks']


async def test_revisiting_sent_page_keeps_navigation(monkeypatch):
    context, queue = context_and_queue(monkeypatch, 5)
    callbacks.get_collection_files_page_for_bot.return_value['total_pages'] = 3
    context.user_data['sent_pages_7_c1'] = {1}
    await callbacks._send_paginated(context, 7, 'col', 'c1')
    queue.submit_batch.assert_not_awaited()
    markup = context.bot.send_message.call_args.kwargs['reply_markup']
    assert any(button.callback_data == 'sn|c1|2' for row in markup.inline_keyboard for button in row)


async def test_page_send_failure_offers_retry(monkeypatch):
    context, queue = context_and_queue(monkeypatch, 0)
    queue.submit_batch.side_effect = RuntimeError('queue full')
    await callbacks._send_page_files(context, 7, 'col', 1)
    assert '重试' in context.bot.send_message.call_args.kwargs['text']
    assert not context.bot_data['_sending_tasks']


async def test_browsing_can_return_to_same_page_immediately(monkeypatch):
    context, _ = context_and_queue(monkeypatch, 0)
    context.bot_data['cb_map'] = {'c1': 'col'}
    monkeypatch.setattr(callbacks, '_send_page', AsyncMock())
    query = SimpleNamespace(data='p|c1|1', from_user=SimpleNamespace(id=7),
                            message=SimpleNamespace(chat_id=7, chat=SimpleNamespace(type='private')),
                            answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
    update = SimpleNamespace(callback_query=query)
    await callbacks.button_callback(update, context)
    await callbacks.button_callback(update, context)
    assert callbacks._send_page.await_count == 2
    query.edit_message_reply_markup.assert_not_awaited()


async def test_stop_button_cancels_actual_bot_queue(monkeypatch):
    context, queue = context_and_queue(monkeypatch, 0)
    queue.cancel_chat = Mock(return_value=1)
    query = SimpleNamespace(data='stop_auto', from_user=SimpleNamespace(id=7),
                            message=SimpleNamespace(chat_id=7, chat=SimpleNamespace(type='private')),
                            answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
    await callbacks.button_callback(SimpleNamespace(callback_query=query), context)
    queue.cancel_chat.assert_called_once_with(7)
    assert context.user_data['stop_auto_send'] is True
