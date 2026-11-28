"""Telegram Mini App 管理面板（v1 只读）：静态页 + initData 校验的 JSON API

安全模型：Telegram 对 initData 做 HMAC 签名（secret = HMAC("WebAppData", BOT_TOKEN)），
后端验签后要求 user.id ∈ ADMIN_IDS 才放行 /api/*；静态页本身不含数据。
所有响应带 frame-ancestors CSP，允许 Telegram 客户端 iframe 渲染。
"""
import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web

logger = logging.getLogger(__name__)

FRAME_ANCESTORS = "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
INIT_DATA_MAX_AGE = 86400  # initData 一天有效，过期重新打开面板即可刷新

PANEL_BOT_STATUSES = ('active', 'revoked', 'banned', 'all')
PANEL_BOT_SORTS = ('name', 'today', 'created', 'files')
PANEL_PAGE_SIZE = 20
PANEL_CODE_STATUSES = ('pending', 'used', 'cancelled', 'all')

_STATUS_LABELS = {
    'active': '🟢 运行中', 'paused': '⏸ 暂停', 'revoked': '⚠️ Token 失效',
    'banned': '⚠️ 封禁', 'admin_stopped': '⚠️ 管理员已停止',
    'frozen': '⚠️ 已冻结', 'deleted': '🗑 已删除',
}


def panel_url() -> str:
    from config import PANEL_PATH, WEBHOOK_HOST

    return f"https://{WEBHOOK_HOST}{PANEL_PATH.strip() or '/panel'}/"


def panel_enabled() -> bool:
    from config import PANEL_PATH

    return bool(PANEL_PATH.strip('/') and Path(_static_dir() / 'index.html').exists())


def _static_dir() -> Path:
    return Path(__file__).resolve().parent.parent / 'static' / 'panel'


def validate_init_data(init_data: str, bot_token: str) -> dict | None:
    """校验 Telegram Mini App initData 签名，返回发起用户；无效返回 None"""
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    except ValueError:
        return None
    received_hash = pairs.pop('hash', None)
    if not received_hash:
        return None
    check_string = '\n'.join(f'{k}={v}' for k, v in sorted(pairs.items()))
    secret = hmac.new(b'WebAppData', bot_token.encode(), hashlib.sha256).digest()
    computed = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(computed, received_hash):
        return None
    try:
        auth_date = int(pairs.get('auth_date', '0'))
    except ValueError:
        return None
    if not auth_date or int(time.time()) - auth_date > INIT_DATA_MAX_AGE:
        return None
    try:
        user = json.loads(pairs.get('user', '{}'))
    except ValueError:
        return None
    return user if user.get('id') else None


def add_panel_routes(app: web.Application, bot_token: str, admin_ids) -> None:
    """把面板静态页和只读 API 挂到 aiohttp 应用；未启用时不注册"""
    if not bot_token or not admin_ids:
        logger.info("管理面板未启用（缺少 BOT_TOKEN 或 ADMIN_IDS）")
        return
    base = _panel_base()
    if not base:
        logger.info("管理面板未启用（PANEL_PATH 为空）")
        return
    index_file = _static_dir() / 'index.html'

    def _json(data, status=200):
        return web.json_response(data, status=status,
                                 headers={'Content-Security-Policy': FRAME_ANCESTORS})

    def _authorized(request) -> dict | None:
        raw = request.headers.get('X-Telegram-Init-Data', '')
        if not raw:
            return None
        user = validate_init_data(raw, bot_token)
        if not user or user.get('id') not in admin_ids:
            return None
        return user

    async def index_handler(request):
        if not index_file.exists():
            return _json({'error': 'panel not built'}, 404)
        return web.Response(body=index_file.read_bytes(), content_type='text/html',
                            charset='utf-8',
                            headers={'Content-Security-Policy': FRAME_ANCESTORS})

    async def stats_handler(request):
        if not _authorized(request):
            return _json({'error': 'unauthorized'}, 401)
        from db import get_platform_stats, get_blacklist_count
        from services.runtime import manager

        stats = await get_platform_stats()
        stats['blacklist_count'] = await get_blacklist_count()
        stats['running_bots'] = manager.active_count if manager else 0
        return _json(stats)

    async def bots_handler(request):
        if not _authorized(request):
            return _json({'error': 'unauthorized'}, 401)
        from db import get_platform_bot_page

        status = request.query.get('status', 'all')
        sort = request.query.get('sort', 'name')
        if status not in PANEL_BOT_STATUSES or sort not in PANEL_BOT_SORTS:
            return _json({'error': 'invalid filter'}, 400)
        try:
            page = int(request.query.get('page', '1'))
        except ValueError:
            return _json({'error': 'invalid page'}, 400)
        bots, total, page = await get_platform_bot_page(
            status, page=page, page_size=PANEL_PAGE_SIZE, sort=sort)
        return _json({
            'bots': [{
                'id': b['id'], 'username': b['bot_username'],
                'firstname': b['bot_firstname'], 'bot_id': b['bot_id'],
                'owner_id': b['owner_id'], 'status': b['status'],
                'status_label': _STATUS_LABELS.get(b['status'], b['status']),
                'created_at': b['created_at'][:19], 'node_id': b['node_id'],
                'file_count': b['file_count'],
            } for b in bots],
            'total': total, 'page': page,
            'last_page': max(1, (total + PANEL_PAGE_SIZE - 1) // PANEL_PAGE_SIZE),
        })

    async def user_handler(request):
        if not _authorized(request):
            return _json({'error': 'unauthorized'}, 401)
        from db import get_user_bots_by_owner
        from db.vip import get_user_vip_info

        try:
            uid = int(request.match_info['uid'])
        except ValueError:
            return _json({'error': 'invalid user id'}, 400)
        if uid <= 0:
            return _json({'error': 'invalid user id'}, 400)
        info = await get_user_vip_info(uid)
        bots = await get_user_bots_by_owner(uid, include_deleted=True)
        counts = {}
        for bot in bots:
            counts[bot['status']] = counts.get(bot['status'], 0) + 1
        return _json({
            'vip': info,
            'bots': [{
                'id': b['id'], 'username': b['bot_username'], 'status': b['status'],
                'status_label': _STATUS_LABELS.get(b['status'], b['status']),
                'created_at': b['created_at'][:19],
            } for b in bots],
            'counts': counts,
        })

    async def codes_handler(request):
        if not _authorized(request):
            return _json({'error': 'unauthorized'}, 401)
        from services.membership import list_redeem_codes

        status = request.query.get('status', 'pending')
        if status not in PANEL_CODE_STATUSES:
            return _json({'error': 'invalid status'}, 400)
        rows = await list_redeem_codes(status, limit=50)
        return _json({'codes': rows})

    app.router.add_get(base, index_handler)
    app.router.add_get(f'{base}/', index_handler)
    app.router.add_get(f'{base}/api/stats', stats_handler)
    app.router.add_get(f'{base}/api/bots', bots_handler)
    app.router.add_get(f'{base}/api/user/{{uid}}', user_handler)
    app.router.add_get(f'{base}/api/codes', codes_handler)
    logger.info("管理面板已挂载 %s（只读，%d 位管理员）", base, len(admin_ids))


def _panel_base() -> str:
    from config import PANEL_PATH

    path = PANEL_PATH.strip()
    if not path.strip('/'):
        return ''
    return path if path.startswith('/') else '/' + path


def entry_button():
    """管理员回复键盘里的面板入口按钮；未启用时返回 None"""
    if not panel_enabled():
        return None
    from telegram import InlineKeyboardButton, WebAppInfo

    return InlineKeyboardButton('🖥 管理面板', web_app=WebAppInfo(url=panel_url()))


async def sync_admin_menu_buttons(bot, admin_ids) -> None:
    """给管理员私聊设置面板菜单按钮。

    同时满足 Telegram 的域名关联要求：inline web_app 按钮的 URL 域名
    必须先通过 MenuButton（或 BotFather）与 Bot 绑定才能正常打开。
    只设管理员私聊，不改全局默认菜单。
    """
    if not panel_enabled() or not admin_ids:
        return
    from telegram import MenuButtonWebApp, WebAppInfo

    url = panel_url()
    for admin_id in admin_ids:
        try:
            await bot.set_chat_menu_button(
                chat_id=admin_id,
                menu_button=MenuButtonWebApp(text='🖥 管理面板', web_app=WebAppInfo(url=url)),
            )
        except Exception as e:
            logger.warning("设置管理员 %s 面板菜单按钮失败: %s", admin_id, e)
    logger.info("已为 %d 位管理员设置面板菜单按钮: %s", len(admin_ids), url)
