"""Telegram Mini App 管理面板：静态页 + initData 校验的 JSON API（含定向赠码等管理操作）

安全模型：Telegram 对 initData 做 HMAC 签名（secret = HMAC("WebAppData", BOT_TOKEN)），
后端验签后要求 user.id ∈ ADMIN_IDS 才放行 /api/*；静态页本身不含数据。
所有响应带 frame-ancestors CSP，允许 Telegram 客户端 iframe 渲染。
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl

from aiohttp import web
from sqlalchemy import select
from telegram.error import Forbidden

from db.core import get_session

logger = logging.getLogger(__name__)

FRAME_ANCESTORS = "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
INIT_DATA_MAX_AGE = 86400  # initData 一天有效，过期重新打开面板即可刷新

PANEL_BOT_STATUSES = ('active', 'revoked', 'banned', 'all')
PANEL_BOT_SORTS = ('name', 'today', 'created', 'files')
PANEL_PAGE_SIZE = 20
GIFT_SEND_DELAY = 0.3  # 批量私信间隔（秒），防触发限流
PANEL_CODE_STATUSES = ('pending', 'used', 'cancelled', 'all')
PANEL_FILE_TYPES = ('photo', 'video', 'document', 'audio', 'voice', 'animation')
PANEL_FILE_SORTS = ('new', 'old', 'size')
# 前端资源版本号：发版时递增，配合 Cache-Control: no-cache 防 webview 缓存旧文件
PANEL_ASSET_VERSION = '4'

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
                            headers={'Content-Security-Policy': FRAME_ANCESTORS,
                                     'Cache-Control': 'no-cache'})

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
        search = request.query.get('search', '').strip()[:64]
        if status not in PANEL_BOT_STATUSES or sort not in PANEL_BOT_SORTS:
            return _json({'error': 'invalid filter'}, 400)
        try:
            page = int(request.query.get('page', '1'))
        except ValueError:
            return _json({'error': 'invalid page'}, 400)
        bots, total, page = await get_platform_bot_page(
            status, page=page, page_size=PANEL_PAGE_SIZE, sort=sort, search=search)
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

    async def bot_files_handler(request):
        if not _authorized(request):
            return _json({'error': 'unauthorized'}, 401)
        from db import get_bot_files_page, get_user_bot_by_id

        try:
            bot_db_id = int(request.match_info['bid'])
        except ValueError:
            return _json({'error': 'invalid bot id'}, 400)
        bot = await get_user_bot_by_id(bot_db_id)
        if not bot:
            return _json({'error': 'bot not found'}, 404)
        file_type = request.query.get('file_type') or None
        if file_type and file_type not in PANEL_FILE_TYPES:
            return _json({'error': 'invalid file type'}, 400)
        sort = request.query.get('sort', 'new')
        if sort not in PANEL_FILE_SORTS:
            return _json({'error': 'invalid sort'}, 400)
        try:
            page = int(request.query.get('page', '1'))
        except ValueError:
            return _json({'error': 'invalid page'}, 400)
        files, total, page = await get_bot_files_page(
            bot_db_id, file_type=file_type, sort=sort, page=page,
            page_size=PANEL_PAGE_SIZE)
        return _json({
            'bot': {'id': bot['id'], 'username': bot['bot_username'],
                    'status_label': _STATUS_LABELS.get(bot['status'], bot['status'])},
            'files': files, 'total': total, 'page': page,
            'last_page': max(1, (total + PANEL_PAGE_SIZE - 1) // PANEL_PAGE_SIZE),
        })

    async def bot_collections_handler(request):
        if not _authorized(request):
            return _json({'error': 'unauthorized'}, 401)
        from db import get_user_bot_by_id
        from db.collections import get_bot_collections_page

        try:
            bot_db_id = int(request.match_info['bid'])
        except ValueError:
            return _json({'error': 'invalid bot id'}, 400)
        bot = await get_user_bot_by_id(bot_db_id)
        if not bot:
            return _json({'error': 'bot not found'}, 404)
        try:
            page = int(request.query.get('page', '1'))
        except ValueError:
            return _json({'error': 'invalid page'}, 400)
        rows, total, page = await get_bot_collections_page(
            bot_db_id, page=page, page_size=PANEL_PAGE_SIZE)
        return _json({
            'collections': rows, 'total': total, 'page': page,
            'last_page': max(1, (total + PANEL_PAGE_SIZE - 1) // PANEL_PAGE_SIZE),
        })

    async def vips_handler(request):
        if not _authorized(request):
            return _json({'error': 'unauthorized'}, 401)
        from db.vip import get_all_vip_users

        members = await get_all_vip_users()
        return _json({'members': [{
            'user_id': m['user_id'], 'vip_level': m['vip_level'],
            'vip_expire_at': m['vip_expire_at'], 'created_at': m['created_at'],
        } for m in members]})

    async def orders_handler(request):
        if not _authorized(request):
            return _json({'error': 'unauthorized'}, 401)
        from services.payments import list_orders

        uid = request.query.get('user_id')
        if uid:
            try:
                uid = int(uid)
            except ValueError:
                return _json({'error': 'invalid user id'}, 400)
        else:
            uid = None
        orders = await list_orders(uid, limit=50)
        return _json({'orders': [{
            'payload': o.get('payload'), 'user_id': o.get('user_id'),
            'product': o.get('product'), 'amount': o.get('amount'),
            'status': o.get('status'), 'created_at': (o.get('created_at') or '')[:19],
        } for o in orders]})

    async def gift_targets_handler(request):
        """定向赠码名单：paid=历史付费用户 / vip=当前有效付费会员，标记已有同等级待领码的用户"""
        if not _authorized(request):
            return _json({'error': 'unauthorized'}, 401)
        from config import BASIC_LEVEL
        from db.models import VipRedeemCode
        from services.payments import paid_user_totals

        group = request.query.get('group', 'paid')
        if group not in ('paid', 'vip'):
            return _json({'error': 'invalid group'}, 400)
        try:
            level = int(request.query.get('level', str(BASIC_LEVEL)))
        except ValueError:
            return _json({'error': 'invalid level'}, 400)
        if level not in (1, 2, 3, BASIC_LEVEL):
            return _json({'error': 'invalid level'}, 400)

        async with get_session() as session:
            rows = (await session.execute(
                select(VipRedeemCode.recipient_uid).where(
                    VipRedeemCode.status == 'pending',
                    VipRedeemCode.vip_level == level,
                    VipRedeemCode.recipient_uid.isnot(None)))).all()
        pending = {r[0] for r in rows}

        users = []
        if group == 'paid':
            totals = await paid_user_totals()
            for uid in sorted(totals):
                users.append({'user_id': uid, 'paid_total': totals[uid],
                              'pending_same_level': uid in pending})
        else:
            from db.vip import get_all_vip_users

            now_text_value = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            for member in await get_all_vip_users():
                expire = member['vip_expire_at']
                if expire and expire <= now_text_value:
                    continue  # 只列当前有效会员
                users.append({'user_id': member['user_id'],
                              'vip_level': member['vip_level'],
                              'vip_expire_at': expire,
                              'pending_same_level': member['user_id'] in pending})
        return _json({'users': users})

    async def gift_send_handler(request):
        """批量赠码：每人一个一次性码，主 Bot 私信送达；同等级待领码用户服务端跳过"""
        from services.runtime import master_app
        from senders import _retry_send

        actor, body = await _write_guard(request)
        level = _int_field(body, 'level')
        months = _int_field(body, 'months')
        expires_days = _int_field(body, 'expires_days', required=False)
        reason = str(body.get('reason') or '')[:500]
        uids = body.get('users')
        if not isinstance(uids, list) or not uids or len(uids) > 500:
            return _json({'error': 'invalid users'}, 400)
        try:
            uids = sorted({int(u) for u in uids})
        except (ValueError, TypeError):
            return _json({'error': 'invalid users'}, 400)
        if not uids or any(u <= 0 for u in uids):
            return _json({'error': 'invalid users'}, 400)
        if not master_app:
            return _json({'error': '主 Bot 未就绪'}, 400)

        from db.models import VipRedeemCode

        async with get_session() as session:
            rows = (await session.execute(
                select(VipRedeemCode.recipient_uid).where(
                    VipRedeemCode.status == 'pending',
                    VipRedeemCode.vip_level == level,
                    VipRedeemCode.recipient_uid.in_(uids)))).all()
        skipped = sorted({r[0] for r in rows})
        targets = [u for u in uids if u not in skipped]
        if not targets:
            return _json({'error': '所选用户都已有待领的同等级码'}, 400)

        from services.membership import generate_redeem_codes, get_plan, REDEEM_MAX_BATCH

        try:
            plan = await get_plan(level)
            codes, batches = [], []
            # REDEEM_MAX_BATCH 为单批上限：人数多时自动分批，便于按批作废
            for offset in range(0, len(targets), REDEEM_MAX_BATCH):
                chunk_targets = targets[offset : offset + REDEEM_MAX_BATCH]
                chunk, batch = await generate_redeem_codes(
                    actor, level, months, len(chunk_targets), expires_days,
                    recipients=chunk_targets)
                codes.extend(chunk)
                batches.append(batch)
        except ValueError as e:
            return _json({'error': str(e)}, 400)

        months_text = '永久' if months == 0 else f'{months} 个月'
        bot = master_app.bot
        sent, failed = [], []
        for uid, code in zip(targets, codes):
            text = (f"🎁 感谢您的支持！\n"
                    f"送您 {plan['name']} {months_text} 兑换码：\n"
                    f"<code>{code}</code>\n"
                    f"在主 Bot 发送 /redeem <code>{code}</code> 即可兑换")
            try:
                await _retry_send(bot.send_message, chat_id=uid, text=text,
                                  parse_mode='HTML')
                sent.append({'user_id': uid, 'code': code})
            except Forbidden:
                failed.append({'user_id': uid, 'error': '用户已停用 Bot，无法私信（码保留可补发）'})
            except Exception as e:
                failed.append({'user_id': uid, 'error': str(e)[:120]})
            await asyncio.sleep(GIFT_SEND_DELAY)
        return _json({'batches': batches, 'reason': reason, 'sent': sent,
                      'failed': failed, 'skipped': skipped})

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

    async def _write_guard(request):
        """写操作的统一入口：校验身份并解析 JSON body，返回 (actor_id, body) 或抛 web 异常"""
        actor = _authorized(request)
        if not actor:
            raise web.HTTPUnauthorized(text='unauthorized')
        try:
            body = await request.json()
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text='invalid json')
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text='invalid json')
        return actor['id'], body

    def _int_field(body, key, *, lo=None, hi=None, required=True, default=None):
        value = body.get(key, default)
        if value is None or value == '':
            if required:
                raise web.HTTPBadRequest(text=f'missing {key}')
            return None
        try:
            value = int(value)
        except (ValueError, TypeError):
            raise web.HTTPBadRequest(text=f'invalid {key}')
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            raise web.HTTPBadRequest(text=f'{key} out of range')
        return value

    async def codes_generate_handler(request):
        from services.membership import generate_redeem_codes

        try:
            actor, body = await _write_guard(request)
            level = _int_field(body, 'level')
            months = _int_field(body, 'months')
            count = _int_field(body, 'count', lo=1, hi=50)
            expires_days = _int_field(body, 'expires_days', required=False)
            codes, batch = await generate_redeem_codes(
                actor, level, months, count, expires_days)
        except ValueError as e:
            return _json({'error': str(e)}, 400)
        return _json({'codes': codes, 'batch': batch})

    async def codes_cancel_handler(request):
        from services.membership import cancel_redeem_codes

        try:
            actor, body = await _write_guard(request)
            code_or_batch = str(body.get('code_or_batch') or '').strip().lower()
            reason = str(body.get('reason') or '')[:500]
            if not code_or_batch:
                return _json({'error': 'missing code_or_batch'}, 400)
            cancelled = await cancel_redeem_codes(actor, code_or_batch, reason)
        except ValueError as e:
            return _json({'error': str(e)}, 400)
        return _json({'cancelled': cancelled})

    async def vip_set_handler(request):
        from services.entitlements import reconcile_user
        from services.membership import adjust_member

        try:
            actor, body = await _write_guard(request)
            uid = _int_field(body, 'user_id', lo=1)
            level = _int_field(body, 'level')
            months = _int_field(body, 'months')
            after = await adjust_member(actor, uid, level=level, months=months,
                                        reason='管理面板设置')
        except ValueError as e:
            return _json({'error': str(e)}, 400)
        from db.vip import get_user_vip_info

        try:
            await reconcile_user(uid)
        except Exception:
            logger.exception("面板设置会员后同步额度失败: user=%s", uid)
        return _json({'after': after, 'info': await get_user_vip_info(uid)})

    app.router.add_get(base, index_handler)
    app.router.add_get(f'{base}/', index_handler)
    app.router.add_get(f'{base}/api/stats', stats_handler)
    app.router.add_get(f'{base}/api/bots', bots_handler)
    app.router.add_get(f'{base}/api/bot/{{bid}}/files', bot_files_handler)
    app.router.add_get(f'{base}/api/bot/{{bid}}/collections', bot_collections_handler)
    app.router.add_get(f'{base}/api/user/{{uid}}', user_handler)
    app.router.add_get(f'{base}/api/codes', codes_handler)
    app.router.add_get(f'{base}/api/vips', vips_handler)
    app.router.add_get(f'{base}/api/orders', orders_handler)
    app.router.add_post(f'{base}/api/codes/generate', codes_generate_handler)
    app.router.add_post(f'{base}/api/codes/cancel', codes_cancel_handler)
    app.router.add_post(f'{base}/api/vip/set', vip_set_handler)
    app.router.add_get(f'{base}/api/gift/targets', gift_targets_handler)
    app.router.add_post(f'{base}/api/gift/send', gift_send_handler)
    # 静态资源（app.js / styles.css / vendor）：禁缓存，放最后避免盖过 API 路由
    async def panel_static_handler(request):
        resource = request.match_info['resource'].replace('\\', '/')
        file_path = (_static_dir() / resource).resolve()
        if not str(file_path).startswith(str(_static_dir().resolve())) or not file_path.is_file():
            return _json({'error': 'not found'}, 404)
        return web.FileResponse(file_path, headers={'Cache-Control': 'no-cache'})
    app.router.add_get(f'{base}/{{resource:.+}}', panel_static_handler)
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
