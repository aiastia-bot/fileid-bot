"""管理面板：initData 验签 + 只读 API + 静态页"""

import hashlib
import hmac
import time
from urllib.parse import urlencode

import pytest
from aiohttp.test_utils import TestClient, TestServer

from db.core import get_session
from db.models import FileMapping, UserBot
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
    (tmp_path / 'app.js').write_text('//app')
    (tmp_path / 'vendor').mkdir()
    (tmp_path / 'vendor' / 'vue.global.prod.js').write_text('//vue')
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


async def _seed_files_and_collections():
    async with get_session() as session:
        session.add_all([
            FileMapping(bot_db_id=1, code='p1', file_type='photo', telegram_file_id='f1',
                        file_size=300, user_id=7, created_at='2026-03-01 00:00:00', is_valid=1),
            FileMapping(bot_db_id=1, code='v1', file_type='video', telegram_file_id='f2',
                        file_size=900, user_id=7, created_at='2026-03-02 00:00:00', is_valid=1),
            FileMapping(bot_db_id=1, code='d1', file_type='document', telegram_file_id='f3',
                        file_size=500, user_id=8, created_at='2026-03-03 00:00:00', is_valid=1),
            FileMapping(bot_db_id=1, code='x1', file_type='photo', telegram_file_id='f4',
                        file_size=999, user_id=7, created_at='2026-03-04 00:00:00', is_valid=0),
        ])
        from db.models import Collection
        session.add_all([
            Collection(code='col1', bot_db_id=1, name='合集', file_count=2,
                       status='completed', created_at='2026-03-05 00:00:00'),
        ])
        await session.commit()


async def test_api_bot_files_filters_and_sorts(client, seeded_bot):
    await _seed_files_and_collections()
    good = {'X-Telegram-Init-Data': make_init_data()}

    files = await client.get('/panel/api/bot/1/files', headers=good)
    data = await files.json()
    assert data['total'] == 3  # is_valid=0 的记录不计入
    assert [f['code'] for f in data['files']] == ['d1', 'v1', 'p1']  # 默认最新在前

    typed = await client.get('/panel/api/bot/1/files?file_type=video', headers=good)
    data = await typed.json()
    assert [f['code'] for f in data['files']] == ['v1']
    assert (await client.get('/panel/api/bot/1/files?file_type=sticker', headers=good)).status == 400

    biggest = await client.get('/panel/api/bot/1/files?sort=size', headers=good)
    assert [f['code'] for f in (await biggest.json())['files']] == ['v1', 'd1', 'p1']

    assert (await client.get('/panel/api/bot/999/files', headers=good)).status == 404
    assert (await client.get('/panel/api/bot/abc/files', headers=good)).status == 400

    cols = await client.get('/panel/api/bot/1/collections', headers=good)
    data = await cols.json()
    assert data['total'] == 1 and data['collections'][0]['code'] == 'col1'


async def test_api_bots_search(client, seeded_bot):
    good = {'X-Telegram-Init-Data': make_init_data()}
    hit = await client.get('/panel/api/bots?status=all&search=pb', headers=good)
    assert (await hit.json())['total'] == 1
    miss = await client.get('/panel/api/bots?status=all&search=zzz', headers=good)
    assert (await miss.json())['total'] == 0
    owner = await client.get(f'/panel/api/bots?status=all&search={ADMIN}', headers=good)
    assert (await owner.json())['total'] == 1


async def test_api_vips_and_orders(client, seeded_bot):
    from services import membership

    await membership.adjust_member(ADMIN, ADMIN, level=3, months=6)
    good = {'X-Telegram-Init-Data': make_init_data()}
    vips = await client.get('/panel/api/vips', headers=good)
    members = (await vips.json())['members']
    assert any(m['user_id'] == ADMIN and m['vip_level'] == 3 for m in members)

    orders = await client.get('/panel/api/orders', headers=good)
    assert orders.status == 200 and (await orders.json())['orders'] == []
    bad = await client.get('/panel/api/orders?user_id=abc', headers=good)
    assert bad.status == 400


async def test_static_assets_served(client):
    app_js = await client.get('/panel/app.js')
    assert app_js.status == 200 and 'app' in await app_js.text()
    vue = await client.get('/panel/vendor/vue.global.prod.js')
    assert vue.status == 200


async def test_write_endpoints_auth_validation_and_success(client, seeded_bot):
    body = {'level': 3, 'months': 6, 'count': 2}
    assert (await client.post('/panel/api/codes/generate', json=body)).status == 401

    good = {'X-Telegram-Init-Data': make_init_data()}
    assert (await client.post('/panel/api/codes/generate', headers=good)).status == 400
    assert (await client.post('/panel/api/codes/generate', headers=good,
                              json={'level': 3, 'months': 6, 'count': 99})).status == 400

    gen = await client.post('/panel/api/codes/generate', headers=good, json=body)
    data = await gen.json()
    assert len(data['codes']) == 2 and data['batch']

    cancel = await client.post('/panel/api/codes/cancel', headers=good,
                               json={'code_or_batch': data['codes'][0]})
    assert (await cancel.json())['cancelled'] == 1
    again = await client.post('/panel/api/codes/cancel', headers=good,
                              json={'code_or_batch': data['codes'][0]})
    assert (await again.json())['cancelled'] == 0

    listed = await client.get('/panel/api/codes?status=pending', headers=good)
    codes = (await listed.json())['codes']
    assert len(codes) == 1 and codes[0]['status'] == 'pending'


async def test_vip_set_endpoint(client, seeded_bot):
    good = {'X-Telegram-Init-Data': make_init_data()}
    set_vip = await client.post('/panel/api/vip/set', headers=good,
                                json={'user_id': ADMIN, 'level': 3, 'months': 6})
    data = await set_vip.json()
    assert data['info']['vip_level'] == 3
    assert data['after']['level'] == 3

    bad_level = await client.post('/panel/api/vip/set', headers=good,
                                  json={'user_id': ADMIN, 'level': 9, 'months': 6})
    assert bad_level.status == 400
    assert (await client.post('/panel/api/vip/set', headers=good,
                              json={'user_id': 'abc', 'level': 3, 'months': 6})).status == 400
    assert (await client.post('/panel/api/vip/set', headers=good,
                              json={})).status == 400
