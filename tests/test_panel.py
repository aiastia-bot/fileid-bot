"""管理面板：initData 验签 + 只读 API + 静态页"""

import hashlib
import hmac
import time
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

from db.core import get_session
from db.models import UserBot
from services import panel

TOKEN = '12345:paneltest'
ADMIN = 99


def make_init_data(user_id=ADMIN, token=TOKEN, offset=0):
    params = {
        'auth_date': str(int(time.time()) - offset),
        'query_id': 'AAF',
        'user': f'{{"id": {user_id}, "first_name": "A"}}',
    }
    check = '\n'.join(f'{k}={v}' for k, v in sorted(params.items()))
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    params['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(params)


def test_validate_init_data():
    user = panel.validate_init_data(make_init_data(), TOKEN)
    assert user['id'] == ADMIN
    assert panel.validate_init_data(make_init_data(), 'wrong:token') is None
    # 篡改字段后签名不匹配
    assert panel.validate_init_data(make_init_data().replace('first_name', 'first_x'), TOKEN) is None
    old = make_init_data(offset=panel.INIT_DATA_MAX_AGE + 100)
    assert panel.validate_init_data(old, TOKEN) is None
    assert panel.validate_init_data('', TOKEN) is None
    assert panel.validate_init_data('hash=deadbeef', TOKEN) is None


@pytest.fixture
async def client(monkeypatch, tmp_path):
    from aiohttp import web

    monkeypatch.setattr(panel, '_static_dir', lambda: tmp_path)
    (tmp_path / 'index.html').write_text('<html>panel</html>')
    monkeypatch.setattr('services.runtime.manager', None, raising=False)
    app = web.Application()
    panel.add_panel_routes(app, TOKEN, [ADMIN])
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


@pytest.fixture
async def seeded_bot(database):
    async with get_session() as session:
        session.add(UserBot(id=1, bot_username='pb', owner_id=ADMIN, bot_token='t',
                            created_at='2026-01-01 00:00:00'))
        await session.commit()


async def test_static_page_served(client):
    for path in ('/panel', '/panel/'):
        response = await client.get(path)
        assert response.status == 200
        assert 'panel' in await response.text()
        assert response.headers['Content-Security-Policy'].startswith('frame-ancestors')


async def test_api_rejects_missing_or_non_admin(client, seeded_bot):
    assert (await client.get('/panel/api/stats')).status == 401
    non_admin = {'X-Telegram-Init-Data': make_init_data(user_id=7)}
    assert (await client.get('/panel/api/stats', headers=non_admin)).status == 401
    assert (await client.get('/panel/api/stats', headers={'X-Telegram-Init-Data': 'garbage'})).status == 401


async def test_stats_running_bots_from_manager(client, seeded_bot, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr('services.runtime.manager',
                        SimpleNamespace(active_count=5), raising=False)
    stats = await client.get('/panel/api/stats',
                             headers={'X-Telegram-Init-Data': make_init_data()})
    assert (await stats.json())['running_bots'] == 5


async def test_api_serves_admin_data(client, seeded_bot):
    good = {'X-Telegram-Init-Data': make_init_data()}
    stats = await client.get('/panel/api/stats', headers=good)
    assert stats.status == 200
    data = await stats.json()
    assert data['bot_count'] == 1 and data['blacklist_count'] == 0

    bots = await client.get('/panel/api/bots?status=all&page=1', headers=good)
    data = await bots.json()
    assert data['total'] == 1 and data['bots'][0]['owner_id'] == ADMIN
    assert data['bots'][0]['status_label']
    assert (await client.get('/panel/api/bots?status=weird', headers=good)).status == 400
    assert (await client.get('/panel/api/bots?page=x', headers=good)).status == 400

    user = await client.get(f'/panel/api/user/{ADMIN}', headers=good)
    data = await user.json()
    assert data['vip']['user_id'] == ADMIN
    assert data['counts']['active'] == 1
    assert (await client.get('/panel/api/user/abc', headers=good)).status == 400
    assert (await client.get('/panel/api/user/0', headers=good)).status == 400

    codes = await client.get('/panel/api/codes?status=all', headers=good)
    assert codes.status == 200 and (await codes.json())['codes'] == []
    assert (await client.get('/panel/api/codes?status=nope', headers=good)).status == 400


async def test_disabled_panel_registers_nothing(monkeypatch):
    from aiohttp import web

    monkeypatch.setattr('config.PANEL_PATH', '')
    app = web.Application()
    panel.add_panel_routes(app, TOKEN, [ADMIN])
    registered = [r.canonical for r in app.router.routes()]
    assert not any('panel' in route for route in registered)


def test_panel_url(monkeypatch):
    monkeypatch.setattr('config.PANEL_PATH', '/panel')
    monkeypatch.setattr('config.WEBHOOK_HOST', 'bots.example.com')
    assert panel.panel_url() == 'https://bots.example.com/panel/'
