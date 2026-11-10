"""级联备份运行时：推送、补位、存量回填

- 推送：Bot 保存新文件后，把文件发给自己的下游节点（走本进程的 SendQueue）
- 补位：周期 reconcile 按成员表重算路由边，Bot 失效自动重建并通知主人
- 回填：/backup sync 把主 Bot 已有而下游缺的文件批量补推（只推直连子节点，
  子节点收到后照常级联，无需遍历整棵树）

复用 send_queue（限速/重试/429 冷却/持久化）与 notices（主人通知）。
"""
import asyncio
import logging
import time

from config import BACKUP_ENABLED, BACKUP_FANOUT
from db import backup as bdb
from db import get_user_bot_by_id
from services.notices import enqueue as notice_enqueue

logger = logging.getLogger(__name__)

SYNC_PAGE_SIZE = 200        # 每页查询缺文件的条数
SYNC_QUEUE_WAIT = 10        # 队列满时的重试等待秒数
SYNC_QUEUE_RETRIES = 60     # 队列满的最大重试次数（约 10 分钟）


# ===== 推送 =====

async def propagate_new_files(context, file_infos: list) -> int:
    """把刚保存的文件推给本 Bot 的全部下游节点，返回提交的文件份数

    file_infos: FileMapping 形状的 dict（code/telegram_file_id/file_type/file_size/file_unique_id）
    必须在本 Bot 所在进程调用（SendQueue 是进程本地的，绑定 context.bot）。
    """
    if not BACKUP_ENABLED or not file_infos:
        return 0

    bot_record = context.bot_data.get('bot_record') or {}
    bot_db_id = bot_record.get('id')
    if not bot_db_id:
        return 0  # 主 Bot / 无记录的 Bot 不参与备份

    pushable = {f.get('file_unique_id'): f for f in file_infos
                if f.get('file_unique_id')}
    if not pushable:
        return 0

    children = await bdb.get_backup_children(bot_db_id)
    # chat_id 用对方 Bot 的数字 id（真机验证可用；send_jobs.chat_id 是整型列，
    # 不能存 @username）
    children = [c for c in children
                if c['status'] == 'active' and c['telegram_id']]
    if not children:
        return 0

    from send_queue import get_queue_from_context
    queue = get_queue_from_context(context)

    pushed = 0
    for child in children:
        batch = [f for uid, f in pushable.items()
                 if not await bdb.has_file(child['to_bot_db_id'], uid)]
        if not batch:
            continue
        chat_id = int(child['telegram_id'])
        try:
            task = queue.submit_batch_async(chat_id, batch)
            await task.accepted
            pushed += len(batch)
            logger.info("备份推送: %d 个文件 → @%s (%s)", len(batch),
                        child['username'], chat_id)
        except Exception as exc:
            # 队列满等场景：放弃本次推送，存量回填可兜底
            logger.warning("备份推送失败 → @%s: %s", child['username'], exc)
    return pushed


async def _propagate_safe(context, file_infos):
    try:
        await propagate_new_files(context, file_infos)
    except Exception:
        logger.exception("备份推送协程异常")


def kick_propagate(context, file_infos) -> None:
    """保存点调用的 fire-and-forget 入口（不阻塞用户回复）"""
    if not BACKUP_ENABLED or not file_infos:
        return
    asyncio.create_task(_propagate_safe(context, file_infos))


# ===== 补位 =====

def _render_rebuild(result: dict) -> str:
    lines = []
    if result.get('dropped'):
        lines.append("⛔ 已失效并移出备份: " + " ".join(f"@{u}" for u in result['dropped']))
    if result.get('removed'):
        lines.append("➖ 移除链路: " + " ".join(f"{p}→{c}" for p, c in result['removed']))
    if result.get('added'):
        lines.append("➕ 新建链路: " + " ".join(f"{p}→{c}" for p, c in result['added']))
    lines.append("当前链路顺序: " + " ".join(f"@{u}" for u in result.get('members', [])))
    return "\n".join(lines)


async def _notify_rebuild(owner_id: int, result: dict) -> None:
    # key 含边集指纹：同一拓扑只会通知一次，拓扑再变会再次通知
    edge_changes = tuple(sorted(
        (str(p), str(c)) for p, c in
        list(result.get('added', [])) + list(result.get('removed', []))))
    key = f"backup_rebuild:{owner_id}:{abs(hash(edge_changes)) % 10 ** 12}"
    await notice_enqueue(key, owner_id, "🔁 备份拓扑已自动更新\n" + _render_rebuild(result))


async def reconcile_all() -> int:
    """周期补位：遍历全部启用配置，返回发生重建的用户数"""
    if not BACKUP_ENABLED:
        return 0
    changed = 0
    for owner_id in await bdb.get_all_backup_configs():
        try:
            result = await bdb.reconcile_owner(owner_id, BACKUP_FANOUT)
            if result.get('changed'):
                changed += 1
                await _notify_rebuild(owner_id, result)
                logger.info("备份拓扑重建 owner=%s: +%d -%d",
                            owner_id, len(result.get('added', [])), len(result.get('removed', [])))
        except Exception:
            logger.exception("备份 reconcile 失败 owner=%s", owner_id)
    return changed


# ===== 存量回填 =====

async def _run_sync_local(bot_db_id: int, app=None) -> str:
    """在本进程执行回填（Bot 由本进程托管时）"""
    from services.leases import acquire, release
    from send_queue import get_queue, QueueFullError

    lease = f"backup_sync:{bot_db_id}"
    if not await acquire(lease, ttl=3600):
        return "已有同步任务在进行，请稍后再试"

    try:
        if app is None:
            from services import runtime
            if runtime.manager:
                app = (runtime.manager.get_all_apps() or {}).get(int(bot_db_id))
    except Exception:
        app = None
    if app is None:
        await release(lease)
        return "该 Bot 未在本进程运行，无法回填"

    bot = app.bot
    queue = get_queue(str(bot.id))
    queue.bot_name = bot.username
    queue.set_bot(bot)

    bot_record = await get_user_bot_by_id(bot_db_id)
    owner_id = bot_record.get('owner_id') if bot_record else 0

    try:
        children = await bdb.get_backup_children(bot_db_id)
        children = [c for c in children
                    if c['status'] == 'active' and c['telegram_id']]
        if not children:
            return "当前没有下游备份节点"
        summary = []
        for child in children:
            offset_id, pushed = 0, 0
            while True:
                files = await bdb.missing_files_for(bot_db_id, child['to_bot_db_id'],
                                                    limit=SYNC_PAGE_SIZE, offset_id=offset_id)
                if not files:
                    break
                from send_queue import split_files_to_batches
                for batch in split_files_to_batches(files):
                    for attempt in range(SYNC_QUEUE_RETRIES):
                        try:
                            task = queue.submit_batch_async(int(child['telegram_id']), batch)
                            await task.accepted
                            break
                        except QueueFullError:
                            if attempt == SYNC_QUEUE_RETRIES - 1:
                                raise
                            await asyncio.sleep(SYNC_QUEUE_WAIT)
                pushed += len(files)
                offset_id = files[-1]['id']
                if len(files) < SYNC_PAGE_SIZE:
                    break
            summary.append(f"@{child['username']}: 补推 {pushed} 个")
            if owner_id:
                await notice_enqueue(
                    f"backup_sync:{bot_db_id}:{child['to_bot_db_id']}:{int(time.time()) // 3600}",
                    owner_id, f"📦 备份回填完成（{bot.username} → {child['username']}）\n{summary[-1]}")
        return "回填完成：\n" + "\n".join(summary)
    finally:
        await release(lease)


async def request_backup_sync(owner_id: int) -> str:
    """发起存量回填：Bot 在本进程就直接跑，否则转发给托管它的 Worker"""
    config = await bdb.get_backup_config(owner_id)
    members = config.get('members') or []
    if len(members) < 2:
        return "请先用 /backup on 配置至少 2 个 Bot"

    root_id = members[0]['bot_db_id']
    bot_record = await get_user_bot_by_id(root_id)
    if not bot_record:
        return "主 Bot 不存在"

    from services import runtime
    from config import ROLE
    local_apps = {}
    if runtime.manager:
        local_apps = runtime.manager.get_all_apps() or {}

    if int(root_id) in {int(k) for k in local_apps}:
        return await _run_sync_local(root_id)

    # Bot 在 Worker 上：走内部 API
    if runtime.scheduler and ROLE == 'master':
        import httpx
        from config import WORKER_SECRET
        from db import get_all_worker_nodes
        nodes = {n['node_id']: n for n in await get_all_worker_nodes()}
        node = nodes.get(bot_record.get('node_id'))
        if node and node['status'] == 'online':
            try:
                async with httpx.AsyncClient() as client:
                    resp = await client.post(
                        f"{node['node_url']}/internal/backup_sync",
                        json={'bot_db_id': root_id},
                        headers={'X-Worker-Secret': WORKER_SECRET},
                        timeout=30.0)
                if resp.status_code == 200:
                    return "回填任务已下发到 Worker，完成后会收到通知"
                return f"Worker 拒绝了回填请求: {resp.text[:120]}"
            except Exception as exc:
                return f"联系 Worker 失败: {exc}"
        return "主 Bot 所在 Worker 不在线"
    return "该 Bot 不在本进程运行，且当前部署模式不支持远程回填"
