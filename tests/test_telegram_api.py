import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import config
from bot_manager import BotManager
from services.telegram_api import application_builder, create_bot


@pytest.mark.parametrize(
    'endpoint,ids,expected',
    [
        ('', set(), 'https://api.telegram.org'),
        ('http://telegram-bot-api:8081', {'987654'}, 'https://api.telegram.org'),
        ('http://telegram-bot-api:8081', {'123456'}, 'http://telegram-bot-api:8081'),
        ('https://telegram-api.example', {'all'}, 'https://telegram-api.example'),
    ],
)
async def test_bot_api_routing_matches_for_live_and_temporary_clients(monkeypatch, endpoint, ids, expected):
    monkeypatch.setattr(config, 'TELEGRAM_BOT_API_URL', endpoint)
    monkeypatch.setattr(config, 'TELEGRAM_BOT_API_BOT_IDS', ids)
    token = '123456:test-token'
    app = BotManager()._create_user_bot_app(token)
    temp = create_bot(token)
    try:
        for bot in (app.bot, temp):
            assert bot.base_url == f'{expected}/bot{token}'
            assert bot.base_file_url == f'{expected}/file/bot{token}'
    finally:
        await app.bot.shutdown()
        await temp.shutdown()


async def test_selected_bot_uses_custom_api_for_webhook_validation_and_files(monkeypatch):
    token = '123456:test-token'
    calls = []

    async def telegram_api(request):
        calls.append(request.path)
        result = {
            'getMe': {'id': 123456, 'is_bot': True, 'first_name': 'Test', 'username': 'routing_test_bot'},
            'setWebhook': True,
            'deleteWebhook': True,
            'getFile': {'file_id': 'test-file', 'file_unique_id': 'test-unique', 'file_path': 'input/test.txt'},
        }
        method = request.match_info['method']
        return web.json_response({'ok': True, 'result': result[method]})

    async def download(request):
        calls.append(request.path)
        return web.Response(body=b'test content')

    server_app = web.Application()
    server_app.router.add_post(f'/bot{token}/{{method}}', telegram_api)
    server_app.router.add_get(f'/file/bot{token}/input/test.txt', download)
    async with TestServer(server_app) as server:
        monkeypatch.setattr(config, 'TELEGRAM_BOT_API_URL', str(server.make_url('/')).rstrip('/'))
        monkeypatch.setattr(config, 'TELEGRAM_BOT_API_BOT_IDS', {'123456'})
        async with application_builder(token).build() as app:
            assert await app.bot.set_webhook('https://example.com/webhook/test')
            file = await app.bot.get_file('test-file')
            assert await file.download_as_bytearray() == b'test content'
        async with create_bot(token) as temp:
            assert temp.id == 123456
            assert await temp.delete_webhook()

    assert calls == [
        f'/bot{token}/getMe', f'/bot{token}/setWebhook', f'/bot{token}/getFile',
        f'/file/bot{token}/input/test.txt', f'/bot{token}/getMe', f'/bot{token}/deleteWebhook',
    ]


@pytest.mark.parametrize(
    'endpoint,ids',
    [
        ('', {'123456'}),
        ('http://local:8081', set()),
        ('http://local:8081', {'all', '123456'}),
        ('http://local:8081/bot', {'123456'}),
        ('http://user:password@local:8081', {'123456'}),
        ('http://local:badport', {'123456'}),
    ],
)
def test_invalid_bot_api_settings_fail_at_startup(monkeypatch, endpoint, ids):
    monkeypatch.setattr(config, 'TELEGRAM_BOT_API_URL', endpoint)
    monkeypatch.setattr(config, 'TELEGRAM_BOT_API_BOT_IDS', ids)
    with pytest.raises(ValueError, match='TELEGRAM_BOT_API_'):
        config.validate_config()
