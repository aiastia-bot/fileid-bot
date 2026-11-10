"""级联备份：配置/成员/路由边 的数据访问与拓扑计算

拓扑规则：
- 成员按 position 排序，position=0 是主 Bot（对外营业、负载最高的那个）
- 主 Bot 只推给 1 个子节点（用户要求 A 不承担扇出），其余节点各推 BACKUP_FANOUT 个
- 每个节点至多一个父节点 → 森林结构，天然无环
- Bot 死亡（status != active）后由 reconcile 剔除并补位，边表随之重建
"""
import logging
from collections import deque
from datetime import datetime
from typing import Optional

from sqlalchemy import delete, exists, select, update

from db.core import get_session, _model_to_dict
from db.models import BackupConfig, BackupMember, BackupEdge, FileMapping, UserBot

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def build_edges(member_bot_db_ids: list, fanout: int = 2) -> list:
    """按成员顺序构建 (from_bot_db_id, to_bot_db_id) 边列表

    主 Bot（位置 0）固定只接 1 个子节点，其余节点各接 fanout 个子节点。
    [A,B,C,D] fanout=2 → A→B, B→C, B→D
    [A,B,C,D,E] fanout=2 → A→B, B→C, B→D, C→E
    """
    if not member_bot_db_ids or len(member_bot_db_ids) < 2 or fanout < 1:
        return []
    edges = []
    queue = deque([member_bot_db_ids[0]])
    remaining = list(member_bot_db_ids[1:])
    while queue and remaining:
        parent = queue.popleft()
        width = 1 if parent == member_bot_db_ids[0] else fanout
        children = remaining[:width]
        remaining = remaining[len(children):]
        for child in children:
            edges.append((parent, child))
            queue.append(child)
    return edges


# ===== 配置与成员 =====

async def get_backup_config(owner_id: int) -> dict:
    """返回 {enabled, members:[{bot_db_id, position, username, status, telegram_id}]}，无配置时 members 为空"""
    async with get_session() as session:
        cfg = (await session.execute(
            select(BackupConfig).where(BackupConfig.owner_id == owner_id)
        )).scalar_one_or_none()
        enabled = bool(cfg and cfg.enabled)
        rows = (await session.execute(
            select(BackupMember, UserBot)
            .join(UserBot, UserBot.id == BackupMember.bot_db_id)
            .where(BackupMember.owner_id == owner_id)
            .order_by(BackupMember.position)
        )).all()
    members = []
    for member, bot in rows:
        d = _model_to_dict(member)
        d.update({'username': bot.bot_username, 'status': bot.status,
                  'telegram_id': bot.bot_id, 'bot_firstname': bot.bot_firstname})
        members.append(d)
    return {'enabled': enabled, 'members': members}


async def set_backup_members(owner_id: int, bot_db_ids: list) -> None:
    """登记/更新成员顺序并启用配置（position 顺序即列表顺序）"""
    now = _now()
    async with get_session() as session:
        cfg = (await session.execute(
            select(BackupConfig).where(BackupConfig.owner_id == owner_id)
        )).scalar_one_or_none()
        if cfg:
            cfg.enabled = 1
            cfg.updated_at = now
        else:
            session.add(BackupConfig(owner_id=owner_id, enabled=1,
                                     created_at=now, updated_at=now))
        await session.execute(
            delete(BackupMember).where(BackupMember.owner_id == owner_id))
        for position, bot_db_id in enumerate(bot_db_ids):
            session.add(BackupMember(owner_id=owner_id, bot_db_id=bot_db_id,
                                     position=position, created_at=now))
        await session.commit()


async def add_backup_member(owner_id: int, bot_db_id: int) -> None:
    members = (await get_backup_config(owner_id))['members']
    ids = [m['bot_db_id'] for m in members]
    if bot_db_id in ids:
        return
    ids.append(bot_db_id)
    await set_backup_members(owner_id, ids)


async def remove_backup_member(owner_id: int, bot_db_id: int) -> None:
    members = (await get_backup_config(owner_id))['members']
    ids = [m['bot_db_id'] for m in members]
    if bot_db_id in ids:
        ids.remove(bot_db_id)
    if len(ids) < 2:
        await disable_backup(owner_id)
    else:
        await set_backup_members(owner_id, ids)


async def disable_backup(owner_id: int) -> None:
    """关闭备份：清边、清成员、配置置 disabled"""
    owner_bot_ids = [m['bot_db_id'] for m in (await get_backup_config(owner_id))['members']]
    async with get_session() as session:
        await session.execute(
            update(BackupConfig).where(BackupConfig.owner_id == owner_id)
            .values(enabled=0, updated_at=_now()))
        await session.execute(
            delete(BackupMember).where(BackupMember.owner_id == owner_id))
        if owner_bot_ids:
            await session.execute(
                delete(BackupEdge).where(BackupEdge.from_bot_db_id.in_(owner_bot_ids)))
        await session.commit()


async def get_all_backup_configs() -> list:
    """所有启用中的配置（owner_id 列表），供周期 reconcile 使用"""
    async with get_session() as session:
        rows = (await session.execute(
            select(BackupConfig.owner_id).where(BackupConfig.enabled == 1)
        )).scalars().all()
    return list(rows)


# ===== 路由边（运行时读） =====

async def get_backup_children(bot_db_id: int) -> list:
    """我（bot_db_id）的下游节点，带目标 Bot 的 username/telegram_id/状态"""
    async with get_session() as session:
        rows = (await session.execute(
            select(BackupEdge, UserBot)
            .join(UserBot, UserBot.id == BackupEdge.to_bot_db_id)
            .where(BackupEdge.from_bot_db_id == bot_db_id)
        )).all()
    children = []
    for edge, bot in rows:
        children.append({'to_bot_db_id': edge.to_bot_db_id,
                         'username': bot.bot_username,
                         'telegram_id': bot.bot_id,
                         'status': bot.status})
    return children


async def get_backup_parent_telegram_ids(bot_db_id: int) -> set:
    """允许向我推送备份的父节点 telegram bot id 集合（接收方白名单）"""
    async with get_session() as session:
        rows = (await session.execute(
            select(UserBot.bot_id)
            .join(BackupEdge, BackupEdge.from_bot_db_id == UserBot.id)
            .where(BackupEdge.to_bot_db_id == bot_db_id)
        )).scalars().all()
    return {r for r in rows if r}


# ===== 文件覆盖 =====

async def has_file(bot_db_id: int, file_unique_id: str) -> bool:
    """目标 Bot 是否已持有该文件（推送去重 + 防环兜底）"""
    if not bot_db_id or not file_unique_id:
        return False
    async with get_session() as session:
        found = await session.execute(
            select(FileMapping.id).where(
                FileMapping.bot_db_id == bot_db_id,
                FileMapping.file_unique_id == file_unique_id,
                FileMapping.is_valid == 1,
            ).limit(1))
        return found.scalar_one_or_none() is not None


async def missing_files_for(parent_bot_db_id: int, child_bot_db_id: int,
                            limit: int = 500, offset_id: int = 0) -> list:
    """父节点有而子节点缺的文件行（存量回填用），按 id 升序分页"""
    from sqlalchemy.orm import aliased
    child = aliased(FileMapping)
    child_has = exists(select(child.id).where(
        child.bot_db_id == child_bot_db_id,
        child.file_unique_id == FileMapping.file_unique_id,
    ))
    async with get_session() as session:
        rows = (await session.execute(
            select(FileMapping).where(
                FileMapping.bot_db_id == parent_bot_db_id,
                FileMapping.is_valid == 1,
                FileMapping.file_unique_id.isnot(None),
                FileMapping.id > offset_id,
                ~child_has,
            ).order_by(FileMapping.id).limit(limit)
        )).scalars().all()
    return [_model_to_dict(r) for r in rows]


async def coverage_stats(owner_id: int) -> list:
    """每条边的覆盖情况：父节点文件数 / 子节点缺失数"""
    from sqlalchemy.orm import aliased
    from sqlalchemy import func
    members = {m['bot_db_id']: m for m in (await get_backup_config(owner_id))['members']}
    stats = []
    for parent, child_id in await get_owner_edges(owner_id):
        child = aliased(FileMapping)
        child_has = exists(select(child.id).where(
            child.bot_db_id == child_id,
            child.file_unique_id == FileMapping.file_unique_id,
        ))
        base = select(func.count()).select_from(FileMapping).where(
            FileMapping.bot_db_id == parent,
            FileMapping.is_valid == 1,
            FileMapping.file_unique_id.isnot(None),
        )
        async with get_session() as session:
            total = (await session.execute(base)).scalar() or 0
            missing = (await session.execute(base.where(~child_has))).scalar() or 0
        stats.append({
            'from': members.get(parent, {}).get('username', str(parent)),
            'to': members.get(child_id, {}).get('username', str(child_id)),
            'parent_total': total,
            'child_missing': missing,
        })
    return stats


async def get_owner_edges(owner_id: int) -> list:
    """主人的全部边 [(from_bot_db_id, to_bot_db_id)]，按成员顺序"""
    members = [m['bot_db_id'] for m in (await get_backup_config(owner_id))['members']]
    if not members:
        return []
    async with get_session() as session:
        rows = (await session.execute(
            select(BackupEdge).where(BackupEdge.from_bot_db_id.in_(members))
        )).scalars().all()
    return [(e.from_bot_db_id, e.to_bot_db_id) for e in rows]


# ===== 拓扑重建（补位） =====

async def reconcile_owner(owner_id: int, fanout: int = 2) -> dict:
    """按当前成员重算边并落库（只写差异）。返回变更说明，供上层发通知。"""
    config = await get_backup_config(owner_id)
    if not config['enabled']:
        return {}
    members = config['members']
    active = [m for m in members if m['status'] == 'active']
    dropped = [m for m in members if m['status'] != 'active']
    active_ids = [m['bot_db_id'] for m in active]

    owner_bot_ids = [m['bot_db_id'] for m in members]
    async with get_session() as session:
        current_rows = (await session.execute(
            select(BackupEdge).where(BackupEdge.from_bot_db_id.in_(owner_bot_ids or [0]))
        )).scalars().all()
    current = {(e.from_bot_db_id, e.to_bot_db_id) for e in current_rows}
    desired = set(build_edges(active_ids, fanout))

    to_add = desired - current
    to_remove = current - desired
    if not to_add and not to_remove:
        return {}

    now = _now()
    async with get_session() as session:
        if to_remove:
            await session.execute(delete(BackupEdge).where(
                _edge_in(BackupEdge, to_remove)))
        for parent, child in to_add:
            session.add(BackupEdge(from_bot_db_id=parent, to_bot_db_id=child,
                                   created_at=now))
        await session.commit()

    names = {m['bot_db_id']: (m.get('username') or str(m['bot_db_id'])) for m in members}
    return {
        'changed': True,
        'added': [(names.get(p, p), names.get(c, c)) for p, c in to_add],
        'removed': [(names.get(p, p), names.get(c, c)) for p, c in to_remove],
        'dropped': [names.get(m['bot_db_id'], m['bot_db_id']) for m in dropped],
        'members': [names.get(i, i) for i in active_ids],
    }


def _edge_in(edge_model, pairs):
    """(from, to) 元组集合的匹配条件（SQLite 无原生 tuple-IN）"""
    from sqlalchemy import or_, and_
    return or_(*[and_(edge_model.from_bot_db_id == p, edge_model.to_bot_db_id == c)
                 for p, c in pairs])


async def remove_bot_from_backup(bot_db_id: int) -> Optional[int]:
    """把 Bot 从备份组彻底移除（成员行 + 相关边），返回其主人 ID（用于补位重建）"""
    async with get_session() as session:
        members = (await session.execute(
            select(BackupMember).where(BackupMember.bot_db_id == bot_db_id)
        )).scalars().all()
        owner_ids = {m.owner_id for m in members}
        await session.execute(
            delete(BackupMember).where(BackupMember.bot_db_id == bot_db_id))
        await session.execute(
            delete(BackupEdge).where(
                (BackupEdge.from_bot_db_id == bot_db_id) |
                (BackupEdge.to_bot_db_id == bot_db_id)))
        await session.commit()
    # 每个涉及的组立即补位
    from config import BACKUP_FANOUT
    for owner_id in owner_ids:
        try:
            await reconcile_owner(owner_id, BACKUP_FANOUT)
        except Exception:
            logger.exception("移除 Bot %d 后补位失败 owner=%s", bot_db_id, owner_id)
    return next(iter(owner_ids)) if owner_ids else None
