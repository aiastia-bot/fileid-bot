"""回调按钮处理器模块"""
import asyncio
import logging

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes

from config import AUTO_SEND_INTERVAL, GROUP_SEND_SIZE, FILE_TYPE_MAP
from db import get_collection, get_collection_files
from utils import escape_markdown
from senders import send_file_group, _retry_send
from send_queue import get_queue_from_context

logger = logging.getLogger(__name__)

PER_PAGE = 5  # 每页文件数


async def _resolve_key(context, sk: str) -> str:
    """从短 key 映射回集合代码
    
    支持两种短key格式：
    - c{id}: 基于集合数据库ID，重启后可通过ID从数据库恢复
    - s{idx}: 基于内存索引（旧格式/降级），重启后失效
    """
    # 1. 先从内存 cb_map 查找
    cb_map = context.bot_data.get('cb_map', {})
    col_code = cb_map.get(sk, '')
    if col_code:
        logger.info("_resolve_key: sk=%s, found=True (cb_map), map_size=%d", sk, len(cb_map))
        return col_code

    # 2. cb_map 未命中：如果是 c{id} 格式，从数据库恢复
    if sk.startswith('c') and sk[1:].isdigit():
        col_id = int(sk[1:])
        from db import get_collection_by_id
        col_info = await get_collection_by_id(col_id)
        if col_info:
            col_code = col_info['code']
            # 回填 cb_map 缓存
            if 'cb_map' not in context.bot_data:
                context.bot_data['cb_map'] = {}
            context.bot_data['cb_map'][sk] = col_code
            logger.info("_resolve_key: sk=%s, found=True (DB恢复 col_id=%d), col_code=%s", sk, col_id, col_code)
            return col_code

    logger.warning("_resolve_key 失败: sk=%s 在 cb_map 中不存在 (map_size=%d)", sk, len(cb_map))
    return ''


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理内联按钮回调"""
    logger.info("========== 按钮回调开始 ==========")

    # === 安全获取 query 对象 ===
    if not update.callback_query:
        logger.error("button_callback: update.callback_query 为 None! update=%s", update)
        return

    query = update.callback_query
    data = query.data

    # === 修正 chat_id 获取 ===
    if query.message:
        chat_id = query.message.chat_id
        chat_type = query.message.chat.type if query.message.chat else "unknown"
    else:
        chat_id = query.from_user.id
        chat_type = "unknown(no_message)"

    user_id = query.from_user.id

    logger.info("回调数据: data=%r (len=%d)", data, len(data) if data else 0)
    logger.info("用户信息: user_id=%s, chat_id=%s, chat_type=%s", user_id, chat_id, chat_type)
    logger.info("bot_data cb_map 大小: %d", len(context.bot_data.get('cb_map', {})))

    # === answer 回调（消除按钮 loading 动画，失败不影响功能） ===
    try:
        await _retry_send(query.answer)
    except Exception as e:
        logger.debug("query.answer() 失败 (回调已过期，可忽略): %s", e)

    # === 防止重复点击：立即移除原消息按钮 ===
    if data != "noop":
        try:
            await _retry_send(query.edit_message_reply_markup, reply_markup=None)
        except Exception:
            pass  # 消息无法编辑（可能是太旧），忽略

    # === 数据为空检查 ===
    if not data:
        logger.error("button_callback: callback_data 为空! query=%s", query)
        try:
            await _retry_send(context.bot.send_message, chat_id=chat_id, text="❌ 回调数据为空，请重试。")
        except Exception:
            pass
        return

    try:
        # 短格式回调处理（sn| 必须在 s| 之前检查）
        if data.startswith("sn|") or data.startswith("s|") or data.startswith("a|") or data.startswith("p|") or data.startswith("ps|"):
            # 解析 action 和 rest
            if data.startswith("sn|"):
                action = 'sn'
                rest = data[3:]
            elif data.startswith("ps|"):
                action = 'ps'
                rest = data[3:]
            else:
                action = data[0]
                rest = data[2:]
            logger.info("短格式回调: action=%s, rest=%s", action, rest)

            if action == 'sn':
                # 下一页发送: sn|key|page（后台发送避免队列拥堵超时）
                parts = rest.split("|")
                logger.info("处理下一页发送: parts=%s", parts)
                if len(parts) < 2:
                    logger.error("下一页发送数据格式错误: rest=%s", rest)
                    await _retry_send(context.bot.send_message, chat_id=chat_id, text="⚠️ 数据格式错误。")
                    return
                sk = parts[0]
                try:
                    page = int(parts[1])
                except ValueError:
                    logger.error("下一页页码不是数字: parts[1]=%s", parts[1])
                    await _retry_send(context.bot.send_message, chat_id=chat_id, text="⚠️ 页码格式错误。")
                    return
                col_code = await _resolve_key(context, sk)
                if not col_code:
                    logger.warning("下一页发送失败: sk=%s 无法解析", sk)
                    await _retry_send(context.bot.send_message, chat_id=chat_id, text="⚠️ 按钮已过期，请重新发送集合代码。")
                    return
                logger.info("启动后台下一页发送: col_code=%s, page=%d", col_code, page)
                # 先更新状态消息，再后台发送
                await _safe_edit_query(query, context, chat_id, f"📤 正在发送第 {page} 页…")
                asyncio.create_task(_send_paginated(context, chat_id, col_code, sk, page=page))

            elif action == 's':
                # 分页发送: s|key (从第1页开始)
                sk = rest
                logger.info("处理分页发送: sk=%s", sk)
                col_code = await _resolve_key(context, sk)
                if not col_code:
                    logger.warning("分页发送失败: sk=%s 无法解析, cb_map=%s", sk, list(context.bot_data.get('cb_map', {}).keys()))
                    await _retry_send(context.bot.send_message, chat_id=chat_id, text="⚠️ 按钮已过期，请重新发送集合代码。")
                    return
                # 清除旧的发送记录，允许重新发送
                sent_key = f"sent_pages_{sk}"
                context.user_data.pop(sent_key, None)
                logger.info("启动后台分页发送: col_code=%s, chat_id=%s", col_code, chat_id)
                await _safe_edit_query(query, context, chat_id, "📤 正在发送第 1 页…")
                asyncio.create_task(_send_paginated(context, chat_id, col_code, sk, page=1))

            elif action == 'a':
                # 自动发送: a|key（异步后台发送，避免 webhook 超时）
                sk = rest
                logger.info("处理自动发送: sk=%s", sk)
                col_code = await _resolve_key(context, sk)
                if not col_code:
                    logger.warning("自动发送失败: sk=%s 无法解析", sk)
                    await _retry_send(context.bot.send_message, chat_id=chat_id, text="⚠️ 按钮已过期，请重新发送集合代码。")
                    return
                # 防重复提交检查
                sending_tasks = _get_sending_tasks(context)
                task_key = f"{chat_id}_{col_code}"
                if task_key in sending_tasks:
                    logger.warning("自动发送重复提交: task_key=%s", task_key)
                    await _safe_edit_query(query, context, chat_id, "⚠️ 该集合正在发送中，请勿重复操作。")
                    return
                logger.info("启动后台自动发送: col_code=%s, chat_id=%s, user_id=%s", col_code, chat_id, user_id)
                asyncio.create_task(_auto_send_bg(context, chat_id, col_code, user_id, query, task_key))

            elif action == 'p':
                # 分页浏览: p|key|page
                parts = rest.split("|")
                logger.info("处理分页浏览: parts=%s", parts)
                if len(parts) < 2:
                    logger.error("分页数据格式错误: rest=%s, parts=%s", rest, parts)
                    await _retry_send(context.bot.send_message, chat_id=chat_id, text="⚠️ 数据格式错误。")
                    return
                sk = parts[0]
                try:
                    page = int(parts[1])
                except ValueError:
                    logger.error("分页页码不是数字: parts[1]=%s", parts[1])
                    await _retry_send(context.bot.send_message, chat_id=chat_id, text="⚠️ 页码格式错误。")
                    return
                col_code = await _resolve_key(context, sk)
                if not col_code:
                    logger.warning("分页失败: sk=%s 无法解析", sk)
                    await _retry_send(context.bot.send_message, chat_id=chat_id, text="⚠️ 按钮已过期，请重新发送集合代码。")
                    return
                logger.info("开始分页浏览: col_code=%s, page=%d", col_code, page)
                await _send_page(context, chat_id, col_code, page, query)
                logger.info("分页浏览完成: col_code=%s, page=%d", col_code, page)

            elif action == 'ps':
                # 发送本页文件: ps|key|page（后台发送避免队列拥堵超时）
                parts = rest.split("|")
                if len(parts) < 2:
                    await _retry_send(context.bot.send_message, chat_id=chat_id, text="⚠️ 数据格式错误。")
                    return
                sk = parts[0]
                try:
                    page = int(parts[1])
                except ValueError:
                    await _retry_send(context.bot.send_message, chat_id=chat_id, text="⚠️ 页码格式错误。")
                    return
                col_code = await _resolve_key(context, sk)
                if not col_code:
                    await _retry_send(context.bot.send_message, chat_id=chat_id, text="⚠️ 按钮已过期，请重新发送集合代码。")
                    return
                logger.info("启动后台发送本页文件: col_code=%s, page=%d", col_code, page)
                await _safe_edit_query(query, context, chat_id, f"📤 正在发送第 {page} 页文件…")
                asyncio.create_task(_send_page_files(context, chat_id, col_code, page))

        elif data == "stop_auto":
            logger.info("处理停止自动发送: user_id=%s", user_id)
            context.user_data['stop_auto_send'] = True
            try:
                await _retry_send(query.edit_message_reply_markup, reply_markup=None)
            except Exception as e:
                logger.warning("停止按钮: 编辑消息失败 (可忽略): %s", e)
            await _retry_send(context.bot.send_message, chat_id=chat_id, text="⏹ 已停止自动发送。")

        elif data == "noop":
            logger.debug("noop 回调")

        else:
            logger.warning("未知的回调数据: %r", data)
            await _retry_send(context.bot.send_message, chat_id=chat_id, text=f"❓ 未知操作: {data}")

    except Exception as e:
        logger.error("按钮回调处理失败: data=%r, error=%s", data, e, exc_info=True)
        try:
            await _retry_send(context.bot.send_message, chat_id=chat_id, text=f"❌ 操作失败: {e}")
        except Exception as e2:
            logger.error("发送错误消息也失败: %s\n原始错误: %s", e2, e)

    logger.info("========== 按钮回调结束 ==========")


async def _safe_edit_query(query, context, chat_id, text, **kwargs):
    """安全编辑回调消息：先尝试 query.edit_message_text，失败则发新消息"""
    if query:
        try:
            await _retry_send(query.edit_message_text, text, **kwargs)
            return
        except Exception:
            pass
    await _retry_send(context.bot.send_message, chat_id=chat_id, text=text, **kwargs)


def _get_sending_tasks(context) -> dict:
    """获取正在发送的任务记录（防重复提交）"""
    if '_sending_tasks' not in context.bot_data:
        context.bot_data['_sending_tasks'] = {}
    return context.bot_data['_sending_tasks']


async def _send_paginated(context, chat_id, col_code, sk, page=1, query=None):
    """分页发送集合文件：每次发送 PER_PAGE 个，带页码按钮，已发送页显示✅"""
    logger.info("_send_paginated: col_code=%s, sk=%s, page=%d", col_code, sk, page)

    files = await get_collection_files(col_code)
    col_info = await get_collection(col_code)

    if not files or not col_info:
        msg = "⚠️ 集合为空或不存在。"
        await _safe_edit_query(query, context, chat_id, msg)
        return

    total = len(files)
    total_pages = (total + PER_PAGE - 1) // PER_PAGE
    page = max(1, min(page, total_pages))
    start = (page - 1) * PER_PAGE
    page_files = files[start:start + PER_PAGE]

    # 记录已发送的页面
    sent_key = f"sent_pages_{sk}"
    sent_pages = context.user_data.get(sent_key, set())

    # 如果该页已发送，不重复发送，只更新状态
    if page in sent_pages:
        safe_name = escape_markdown(col_info['name'])
        text = f"📦 *{safe_name}*\n"
        text += f"⚠️ 第 {page}/{total_pages} 页已发送过，请勿重复操作。\n"
        text += f"📊 进度: {len(sent_pages)}/{total_pages} 页"
        if len(sent_pages) >= total_pages:
            text += "\n\n🎉 所有文件已发送完毕！"
            await _safe_edit_query(query, context, chat_id, text, parse_mode="Markdown")
        else:
            # 只显示未发送页的按钮
            await _safe_edit_query(query, context, chat_id, text, parse_mode="Markdown")
        return

    sent_pages.add(page)
    context.user_data[sent_key] = sent_pages

    # 通过 SendQueue 发送本页文件（享受 Bot 级别限流保护 + Redis 持久化）
    logger.info("_send_paginated: 发送第 %d 页, %d 个文件", page, len(page_files))
    try:
        queue = get_queue_from_context(context)
        sent = await queue.submit_batch(chat_id, page_files)
        logger.info("_send_paginated: 第 %d 页发送完成, sent=%d", page, sent)
    except Exception as e:
        logger.error("_send_paginated: 第 %d 页发送失败: %s", page, e, exc_info=True)
        sent = 0

    # 构建状态消息
    safe_name = escape_markdown(col_info['name'])
    text = f"📦 *{safe_name}*\n"
    text += f"✅ 第 {page}/{total_pages} 页已发送 ({sent}/{len(page_files)})\n"
    text += f"📊 进度: {len(sent_pages)}/{total_pages} 页"

    # 全部发送完毕：不显示任何按钮，防止重复操作
    if len(sent_pages) >= total_pages:
        text += "\n\n🎉 所有文件已发送完毕！"
        await _safe_edit_query(query, context, chat_id, text, parse_mode="Markdown")
        return

    # 未发送完：显示导航按钮
    buttons = []
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"sn|{sk}|{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"sn|{sk}|{page + 1}"))
    buttons.append(nav)

    # 页码按钮行：最多显示当前页前后5页，每行5个按钮
    page_range_start = max(1, page - 5)
    page_range_end = min(total_pages, page + 5)
    if page_range_end - page_range_start < 10:
        if page_range_start == 1:
            page_range_end = min(total_pages, page_range_start + 10)
        elif page_range_end == total_pages:
            page_range_start = max(1, page_range_end - 10)

    page_buttons = []
    if page_range_start > 1:
        page_buttons.append(InlineKeyboardButton("<<", callback_data=f"sn|{sk}|{page_range_start - 1}"))

    for p in range(page_range_start, page_range_end + 1):
        if p in sent_pages and p != page:
            label = f"✅{p}"
        elif p == page:
            label = f"【{p}】"
        else:
            label = f"{p}"
        page_buttons.append(InlineKeyboardButton(label, callback_data=f"sn|{sk}|{p}"))
        if len(page_buttons) == 5:
            buttons.append(page_buttons)
            page_buttons = []

    if page_range_end < total_pages:
        page_buttons.append(InlineKeyboardButton(">>", callback_data=f"sn|{sk}|{page_range_end + 1}"))

    if page_buttons:
        buttons.append(page_buttons)

    reply_markup = InlineKeyboardMarkup(buttons)
    await _safe_edit_query(query, context, chat_id, text, parse_mode="Markdown", reply_markup=reply_markup)


async def _auto_send(context, chat_id, col_code, user_id, query=None):
    """自动发送集合文件（每组间隔）"""
    logger.info("_auto_send 开始: col_code=%s, chat_id=%s, user_id=%s", col_code, chat_id, user_id)
    files = await get_collection_files(col_code)
    logger.info("_auto_send: 查询到 %d 个文件", len(files) if files else 0)
    if not files:
        msg = "⚠️ 集合为空。"
        await _safe_edit_query(query, context, chat_id, msg)
        return

    total = len(files)
    context.user_data['stop_auto_send'] = False

    keyboard = [[InlineKeyboardButton("⏹ 停止发送", callback_data="stop_auto")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    status_msg = await _retry_send(context.bot.send_message, 
        chat_id=chat_id, text=f"▶️ 自动发送中... (0/{total})", reply_markup=reply_markup
    )

    pv = [f for f in files if f['file_type'] in ('photo', 'video')]
    docs = [f for f in files if f['file_type'] == 'document']
    audios = [f for f in files if f['file_type'] in ('audio', 'voice')]

    all_groups = []
    for lst in [pv, docs, audios]:
        for i in range(0, len(lst), GROUP_SEND_SIZE):
            all_groups.append(lst[i:i + GROUP_SEND_SIZE])

    sent_count = 0
    queue = get_queue_from_context(context)
    for idx, group in enumerate(all_groups):
        if context.user_data.get('stop_auto_send'):
            # 取消队列中该用户剩余的 auto_send 任务
            await _retry_send(context.bot.send_message, chat_id=chat_id, text=f"⏹ 已停止。成功发送 {sent_count}/{total} 个文件。")
            return

        try:
            sent_count += await queue.submit_batch(chat_id, group)
        except Exception as e:
            logger.error("自动发送组失败: %s", e)

        try:
            await _retry_send(context.bot.edit_message_text,
                chat_id=chat_id, message_id=status_msg.message_id,
                text=f"▶️ 自动发送中... ({sent_count}/{total})",
                reply_markup=reply_markup
            )
        except Exception:
            pass

        if idx < len(all_groups) - 1:
            await asyncio.sleep(AUTO_SEND_INTERVAL)

    try:
        await _retry_send(context.bot.edit_message_text,
            chat_id=chat_id, message_id=status_msg.message_id,
            text=f"✅ 自动发送完成！成功 {sent_count}/{total}",
            reply_markup=None
        )
    except Exception:
        await _retry_send(context.bot.send_message, chat_id=chat_id, text=f"✅ 自动发送完成！成功 {sent_count}/{total}")


async def _send_page(context, chat_id, col_code, page, query=None):
    """分页浏览集合（只看列表，不发送文件）"""
    logger.info("_send_page: col_code=%s, page=%d, chat_id=%s", col_code, page, chat_id)
    files = await get_collection_files(col_code)
    col_info = await get_collection(col_code)
    logger.info("_send_page: files=%d, col_info=%s", len(files) if files else 0, bool(col_info))
    if not files or not col_info:
        msg = "⚠️ 集合为空或不存在。"
        await _safe_edit_query(query, context, chat_id, msg)
        return

    total = len(files)
    total_pages = (total + PER_PAGE - 1) // PER_PAGE
    page = max(1, min(page, total_pages))
    start = (page - 1) * PER_PAGE
    page_files = files[start:start + PER_PAGE]

    safe_name = escape_markdown(col_info['name'])
    text = f"📦 *{safe_name}* (第{page}/{total_pages}页，共{total}个文件)\n\n"
    for i, f in enumerate(page_files, start + 1):
        type_name = FILE_TYPE_MAP.get(f['file_type'], f['file_type'])
        size_mb = f['file_size'] / (1024 * 1024) if f['file_size'] else 0
        size_text = f"{size_mb:.1f}MB" if size_mb >= 1 else f"{f['file_size'] / 1024:.0f}KB" if f['file_size'] else "未知"
        text += f"{i}. {type_name} ({size_text})\n"

    # 获取短 key
    sk = None
    cb_map = context.bot_data.get('cb_map', {})
    for k, v in cb_map.items():
        if v == col_code:
            sk = k
            break
    if not sk:
        from handlers.messages import _short_key
        sk = await _short_key(context, col_code)

    buttons = []
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("⬅️ 上一页", callback_data=f"p|{sk}|{page - 1}"))
    nav.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav.append(InlineKeyboardButton("➡️ 下一页", callback_data=f"p|{sk}|{page + 1}"))
    buttons.append(nav)
    buttons.append([InlineKeyboardButton("⬇️ 发送本页文件", callback_data=f"ps|{sk}|{page}")])
    buttons.append([
        InlineKeyboardButton("⬇️ 分页发送", callback_data=f"s|{sk}"),
        InlineKeyboardButton("▶️ 自动发送", callback_data=f"a|{sk}"),
    ])

    reply_markup = InlineKeyboardMarkup(buttons)
    await _safe_edit_query(query, context, chat_id, text, parse_mode="Markdown", reply_markup=reply_markup)


async def _send_page_files(context, chat_id, col_code, page, query=None):
    """发送指定页的文件"""
    logger.info("_send_page_files: col_code=%s, page=%d, chat_id=%s", col_code, page, chat_id)
    files = await get_collection_files(col_code)
    logger.info("_send_page_files: files=%d", len(files) if files else 0)
    if not files:
        msg = "⚠️ 集合为空。"
        await _safe_edit_query(query, context, chat_id, msg)
        return

    start = (page - 1) * PER_PAGE
    page_files = files[start:start + PER_PAGE]
    if not page_files:
        msg = "⚠️ 该页没有文件。"
        await _safe_edit_query(query, context, chat_id, msg)
        return

    logger.info("_send_page_files: 准备发送 %d 个文件", len(page_files))
    queue = get_queue_from_context(context)
    sent = await queue.submit_batch(chat_id, page_files)
    result_text = f"✅ 已发送第{page}页文件 ({sent}/{len(page_files)})"
    logger.info("_send_page_files 完成: %s", result_text)
    await _safe_edit_query(query, context, chat_id, result_text)


# ===== 后台发送任务（避免 webhook 超时） =====

async def _auto_send_bg(context, chat_id, col_code, user_id, query, task_key: str):
    """后台自动发送：在 asyncio.Task 中运行，不阻塞 webhook 响应
    
    - 注册 task_key 防止重复提交
    - 发送完成后自动清理 task_key
    - 异常不会导致未处理的 Task 错误
    """
    sending_tasks = _get_sending_tasks(context)
    sending_tasks[task_key] = True
    try:
        await _auto_send(context, chat_id, col_code, user_id, query)
    except Exception as e:
        logger.error("后台自动发送异常: col_code=%s, chat_id=%s, error=%s", col_code, chat_id, e, exc_info=True)
        try:
            await _retry_send(context.bot.send_message, chat_id=chat_id,
                              text=f"❌ 自动发送失败: {e}")
        except Exception:
            pass
    finally:
        sending_tasks.pop(task_key, None)
        logger.info("后台自动发送任务结束: task_key=%s", task_key)


