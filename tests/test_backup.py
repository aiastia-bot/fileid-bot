"""级联备份的 DB 层与工具函数测试

覆盖：
- build_edges 拓扑：主 Bot 单推、备份层扇出、单父性质、参数边界
- 配置/成员 CRUD 与 reconcile 补位（B 失效 → A→C, C→D，通知入队）
- has_file / missing_files_for（推送去重与回填数据源）
- get_files_by_codes 跨 Bot 同码取回（返回本 Bot 的 file_id，带 requested_code）
- parse_file_code 通用前缀兜底

运行：
    python -m pytest tests/test_backup.py -v
"""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_DB_FD, _DB_PATH = tempfile.mkstemp(prefix='fileid_test_bk_', suffix='.db')
os.close(_DB_FD)
os.unlink(_DB_PATH)
os.environ.setdefault('DB_TYPE', 'sqlite')
os.environ.setdefault('DB_PATH', _DB_PATH)
os.environ.setdefault('REDIS_URL', '')

from db.core import init_db, get_session  # noqa: E402
from db.models import UserBot, FileMapping, UserNotice  # noqa: E402
from db import backup as bdb  # noqa: E402
from db.files import save_file, get_files_by_codes  # noqa: E402
from utils import parse_file_code  # noqa: E402


async def _add_bot(owner_id: int, bot_id: int, username: str, status: str = 'active') -> int:
    async with get_session() as session:
        bot = UserBot(owner_id=owner_id, bot_token=f'tok_{owner_id}_{bot_id}',
                      bot_id=bot_id, bot_username=username,
                      bot_firstname='T', status=status,
                      created_at='2026-01-01 00:00:00', updated_at='2026-01-01 00:00:00')
        session.add(bot)
        await session.commit()
        return bot.id


async def _set_status(bot_db_id: int, status: str) -> None:
    async with get_session() as session:
        bot = await session.get(UserBot, bot_db_id)
        bot.status = status
        await session.commit()


class TestBuildEdges(unittest.IsolatedAsyncioTestCase):
    def test_two_members(self):
        assert bdb.build_edges([1, 2], 2) == [(1, 2)]

    def test_four_members_primary_single_push(self):
        # A 只推给 B，B 分发给 C/D（用户的核心诉求：A 不承担扇出）
        assert sorted(bdb.build_edges([1, 2, 3, 4], 2)) == [(1, 2), (2, 3), (2, 4)]

    def test_five_members_second_level(self):
        assert sorted(bdb.build_edges([1, 2, 3, 4, 5], 2)) == \
            [(1, 2), (2, 3), (2, 4), (3, 5)]

    def test_single_parent_property(self):
        members = list(range(1, 9))
        edges = bdb.build_edges(members, 2)
        parents = [c for _, c in edges]
        assert len(parents) == len(set(parents)), '每个节点至多一个父节点'
        assert set(parents) | {members[0]} == set(members), '全员可达'
        assert len(edges) == len(members) - 1

    def test_invalid_inputs(self):
        assert bdb.build_edges([], 2) == []
        assert bdb.build_edges([1], 2) == []
        assert bdb.build_edges([1, 2], 0) == []


class TestBackupTopology(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        async with get_session() as session:
            from sqlalchemy import delete
            for model in (FileMapping, UserBot, bdb.BackupConfig,
                          bdb.BackupMember, bdb.BackupEdge, UserNotice):
                await session.execute(delete(model))
            await session.commit()
        # 主人 100：A B C D 四个 Bot
        self.a = await _add_bot(100, 111, 'backupa')
        self.b = await _add_bot(100, 222, 'backupb')
        self.c = await _add_bot(100, 333, 'backupc')
        self.d = await _add_bot(100, 444, 'backupd')

    async def test_setup_and_reconcile(self):
        await bdb.set_backup_members(100, [self.a, self.b, self.c, self.d])
        result = await bdb.reconcile_owner(100, 2)
        assert result.get('changed')
        assert sorted(await bdb.get_owner_edges(100)) == \
            sorted([(self.a, self.b), (self.b, self.c), (self.b, self.d)])
        # 幂等：再跑一次无变化
        result2 = await bdb.reconcile_owner(100, 2)
        assert not result2.get('changed')

    async def test_failover_promotes_next_member(self):
        # B 失效 → A→C, C→D（自动补位）；通知走服务层 reconcile_all
        await bdb.set_backup_members(100, [self.a, self.b, self.c, self.d])
        from services.backup import reconcile_all
        await reconcile_all()
        assert sorted(await bdb.get_owner_edges(100)) == \
            sorted([(self.a, self.b), (self.b, self.c), (self.b, self.d)])
        await _set_status(self.b, 'revoked')
        changed = await reconcile_all()
        assert changed == 1
        assert sorted(await bdb.get_owner_edges(100)) == \
            sorted([(self.a, self.c), (self.c, self.d)])
        # 通知已入队（notices outbox）
        async with get_session() as session:
            notices = (await session.execute(
                __import__('sqlalchemy').select(UserNotice)
                .where(UserNotice.user_id == 100))).scalars().all()
        assert notices and 'backupb' in notices[0].body

    async def test_root_failover(self):
        # 根失效 → 备份顶上当根
        await bdb.set_backup_members(100, [self.a, self.b, self.c])
        await bdb.reconcile_owner(100, 2)
        await _set_status(self.a, 'revoked')
        result = await bdb.reconcile_owner(100, 2)
        assert result['changed']
        assert sorted(await bdb.get_owner_edges(100)) == sorted([(self.b, self.c)])

    async def test_disable_clears_edges(self):
        await bdb.set_backup_members(100, [self.a, self.b])
        await bdb.reconcile_owner(100, 2)
        assert await bdb.get_owner_edges(100)
        await bdb.disable_backup(100)
        assert not await bdb.get_owner_edges(100)
        config = await bdb.get_backup_config(100)
        assert not config['enabled'] and not config['members']

    async def test_children_and_parent_whitelist(self):
        await bdb.set_backup_members(100, [self.a, self.b, self.c])
        await bdb.reconcile_owner(100, 2)
        children = await bdb.get_backup_children(self.a)
        assert children and children[0]['to_bot_db_id'] == self.b
        parents_b = await bdb.get_backup_parent_telegram_ids(self.b)
        assert parents_b == {111}
        parents_c = await bdb.get_backup_parent_telegram_ids(self.c)
        assert parents_c == {222}

    async def test_ownership_isolation(self):
        # 另一主人的 Bot 不能混入
        other = await _add_bot(200, 555, 'otherbot')
        await bdb.set_backup_members(200, [other])
        assert await bdb.get_owner_edges(200) == []
        assert not await bdb.get_backup_children(other)


class TestBackupFileFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await init_db()
        async with get_session() as session:
            from sqlalchemy import delete
            for model in (FileMapping, UserBot, bdb.BackupConfig,
                          bdb.BackupMember, bdb.BackupEdge, UserNotice):
                await session.execute(delete(model))
            await session.commit()
        self.a = await _add_bot(100, 111, 'backupa')
        self.b = await _add_bot(100, 222, 'backupb')

    async def test_has_file_and_missing(self):
        code_a = await save_file(1, 'document', 'FILEID_A', 100, 'uniq-1', 'backupa', 'backupa',
                                 bot_db_id=self.a)
        assert await bdb.has_file(self.a, 'uniq-1')
        assert not await bdb.has_file(self.b, 'uniq-1')
        missing = await bdb.missing_files_for(self.a, self.b)
        assert len(missing) == 1 and missing[0]['file_unique_id'] == 'uniq-1'
        # 保存备份副本后不再缺
        await save_file(0, 'document', 'FILEID_B', 100, 'uniq-1', 'backupb', 'backupb',
                        bot_db_id=self.b)
        assert not await bdb.missing_files_for(self.a, self.b)

    async def test_cross_bot_code_resolution(self):
        code_a = await save_file(1, 'document', 'FILEID_A', 100, 'uniq-1', 'backupa', 'backupa',
                                 bot_db_id=self.a)
        # B 侧持有同一文件的备份副本（自己的 file_id）
        code_b = await save_file(0, 'document', 'FILEID_B', 100, 'uniq-1', 'backupb', 'backupb',
                                 bot_db_id=self.b)
        assert code_a != code_b
        # 用 A 的码问 B：应返回 B 的行（B 自己的 file_id），并带 requested_code
        rows = await get_files_by_codes([code_a], self.b)
        assert len(rows) == 1
        assert rows[0]['bot_db_id'] == self.b
        assert rows[0]['telegram_file_id'] == 'FILEID_B'
        assert rows[0]['requested_code'] == code_a
        # 反向：B 的码问 A，同样解析
        rows_a = await get_files_by_codes([code_b], self.a)
        assert rows_a and rows_a[0]['telegram_file_id'] == 'FILEID_A'
        # 没有备份副本的码：维持旧行为（过滤掉别的 Bot 的行）
        code_x = await save_file(1, 'photo', 'FILEID_X', 10, 'uniq-x', 'backupa', 'backupa',
                                 bot_db_id=self.a)
        assert not await get_files_by_codes([code_x], self.b)

    async def test_save_file_dedup_on_backup_push(self):
        # 同一文件重复推送 → 幂等，返回同一代码
        c1 = await save_file(0, 'document', 'FILEID_B1', 100, 'uniq-1', 'backupb', 'backupb',
                             bot_db_id=self.b)
        c2 = await save_file(0, 'document', 'FILEID_B2', 100, 'uniq-1', 'backupb', 'backupb',
                             bot_db_id=self.b)
        assert c1 == c2


class TestParseFileCode(unittest.IsolatedAsyncioTestCase):
    async def test_generic_prefix_fallback(self):
        mine = f'mybot_d:{"a" * 32}'
        other = f'otherbot_p:{"b" * 32}'
        assert parse_file_code(mine, 'mybot') == [mine]
        # 其他 Bot 前缀的码也能被解析（跨 Bot 同码取回的前提）
        assert parse_file_code(other, 'mybot') == [other]
        assert parse_file_code(f'{mine} {other}', 'mybot') == [mine, other]
        # 集合码不被文件码误匹配
        col = f'mybot_col:{"c" * 32}'
        assert parse_file_code(col, 'mybot') == []
        assert parse_file_code('random text', 'mybot') == []


class TestVipGate(unittest.IsolatedAsyncioTestCase):
    """级联备份的 VIP 门槛：VIP2/3 可用，免费/VIP1/基础版不可用"""

    async def asyncSetUp(self):
        await init_db()

    async def _level_of(self, user_id, level):
        from db.vip import update_user_vip, user_can_use_backup, get_or_create_user
        await get_or_create_user(user_id)
        await update_user_vip(user_id, level, 1)
        return await user_can_use_backup(user_id)

    async def test_vip_gate(self):
        from db.vip import get_or_create_user, user_can_use_backup
        await get_or_create_user(900)
        assert not await user_can_use_backup(900), '免费用户不可用'
        assert not await self._level_of(901, 1), 'VIP1 不可用'
        assert await self._level_of(902, 2), 'VIP2 可用'
        assert await self._level_of(903, 3), 'VIP3 可用'


class TestPurgeDeadBot(unittest.IsolatedAsyncioTestCase):
    """死亡 Bot 记录清理：文件行、集合引用、计数器、备份组联动"""

    async def asyncSetUp(self):
        await init_db()
        async with get_session() as session:
            from sqlalchemy import delete
            for model in (FileMapping, UserBot, bdb.BackupConfig,
                          bdb.BackupMember, bdb.BackupEdge, UserNotice):
                await session.execute(delete(model))
            await session.commit()
        from db.models import Collection, CollectionItem
        self.Collection, self.CollectionItem = Collection, CollectionItem
        self.a = await _add_bot(100, 111, 'backupa')
        self.b = await _add_bot(100, 222, 'backupb')

    async def _make_collection(self, code, file_codes):
        from db.collections import add_file_to_collection
        async with get_session() as session:
            session.add(self.Collection(code=code, bot_username='backupa',
                                        name='test', user_id=1, file_count=len(file_codes),
                                        status='completed',
                                        created_at='2026-01-01 00:00:00'))
            await session.commit()
        for order, fc in enumerate(file_codes):
            await add_file_to_collection(code, fc, order)

    async def test_preview_and_purge(self):
        from db import bot_purge_preview, purge_bot_files
        from db.files import get_bot_file_count
        from db.collections import get_collection_files

        codes = [await save_file(1, 'document', f'FID_{i}', 100, f'uniq-{i}',
                                 'backupa', 'backupa', bot_db_id=self.a)
                 for i in range(3)]
        await self._make_collection('col_purge_test', codes)

        preview = await bot_purge_preview(self.a)
        assert preview['files'] == 3
        assert preview['collection_items'] == 3
        assert preview['collections'] == 1

        result = await purge_bot_files(self.a)
        assert result['deleted_files'] == 3
        assert result['removed_items'] == 3
        assert result['fixed_collections'] == 1

        # 行已删、计数器归零、集合引用清空且 file_count 校准
        assert not await get_files_by_codes(codes, self.a)
        assert await get_bot_file_count(self.a) == 0
        assert not await get_collection_files('col_purge_test')
        async with get_session() as session:
            from sqlalchemy import select
            col = (await session.execute(
                select(self.Collection).where(self.Collection.code == 'col_purge_test')
            )).scalar_one()
        assert col.file_count == 0

        # B 的记录不受影响
        code_b = await save_file(1, 'document', 'FID_B', 100, 'uniq-b',
                                 'backupb', 'backupb', bot_db_id=self.b)
        assert await get_files_by_codes([code_b], self.b)

    async def test_purge_removes_backup_membership(self):
        await bdb.set_backup_members(100, [self.a, self.b])
        await bdb.reconcile_owner(100, 2)
        assert await bdb.get_owner_edges(100)

        owner_id = await bdb.remove_bot_from_backup(self.a)
        assert owner_id == 100
        # 成员与边都清了
        config = await bdb.get_backup_config(100)
        assert all(m['bot_db_id'] != self.a for m in config['members'])
        assert (self.a, self.b) not in await bdb.get_owner_edges(100)


class TestCrossBotCollection(unittest.IsolatedAsyncioTestCase):
    """集合跨 Bot 取回：按接收 Bot 解析出自己的 file_id，缺失文件计入 missing"""

    async def asyncSetUp(self):
        await init_db()
        async with get_session() as session:
            from sqlalchemy import delete
            for model in (FileMapping, UserBot, bdb.BackupConfig,
                          bdb.BackupMember, bdb.BackupEdge, UserNotice):
                await session.execute(delete(model))
            await session.commit()
        from db.models import Collection, CollectionItem
        self.Collection, self.CollectionItem = Collection, CollectionItem
        self.a = await _add_bot(100, 111, 'backupa')
        self.b = await _add_bot(100, 222, 'backupb')

    async def test_collection_resolves_per_bot(self):
        from db.collections import (create_collection, add_file_to_collection,
                                    get_collection_files_for_bot)
        codes = [await save_file(1, 'document', f'FID_A{i}', 100, f'uniq-{i}',
                                 'backupa', 'backupa', bot_db_id=self.a)
                 for i in range(3)]
        # 第 4 个文件只在 A 上，不备份到 B
        loner = await save_file(1, 'photo', 'FID_LONER', 10, 'uniq-loner',
                                'backupa', 'backupa', bot_db_id=self.a)
        await create_collection('col_xbot_test', 'backupa', '跨Bot测试', 1, bot_db_id=self.a)
        for order, code in enumerate(codes + [loner]):
            await add_file_to_collection('col_xbot_test', code, order)

        # A 上：4 个全是自己的
        files_a, missing_a = await get_collection_files_for_bot('col_xbot_test', self.a)
        assert len(files_a) == 4 and missing_a == 0
        assert all(f['telegram_file_id'].startswith('FID_') for f in files_a)

        # B 上：3 个备份副本解析成 B 的 file_id，loner 计入 missing
        for i in range(3):
            await save_file(0, 'document', f'FID_B{i}', 100, f'uniq-{i}',
                            'backupb', 'backupb', bot_db_id=self.b)
        files_b, missing_b = await get_collection_files_for_bot('col_xbot_test', self.b)
        assert len(files_b) == 3 and missing_b == 1
        assert all(f['telegram_file_id'].startswith('FID_B') for f in files_b)
        assert all(f['bot_db_id'] == self.b for f in files_b)
        # 保持集合顺序
        assert [f['file_unique_id'] for f in files_b] == ['uniq-0', 'uniq-1', 'uniq-2']


class TestAutoPurgeGrace(unittest.IsolatedAsyncioTestCase):
    """失效 Bot 自动清理的宽限期判定"""

    async def asyncSetUp(self):
        await init_db()

    async def test_dead_bots_past_grace(self):
        from datetime import datetime, timedelta
        from db import get_dead_bots_past_grace
        old = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        fresh = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        revoked_old = await _add_bot(300, 301, 'deadold')
        revoked_fresh = await _add_bot(300, 302, 'deadfresh')
        stopped = await _add_bot(300, 303, 'adminstopped', status='admin_stopped')
        active = await _add_bot(300, 304, 'alive')

        from db import update_user_bot_status
        await update_user_bot_status(revoked_old, 'revoked')
        await update_user_bot_status(revoked_fresh, 'revoked')
        async with get_session() as session:
            from sqlalchemy import update as sa_update
            from db.models import UserBot
            await session.execute(sa_update(UserBot).where(
                UserBot.id == revoked_old).values(updated_at=old))
            await session.execute(sa_update(UserBot).where(
                UserBot.id == revoked_fresh).values(updated_at=fresh))
            await session.commit()

        dead = await get_dead_bots_past_grace(1)
        ids = {b['id'] for b in dead}
        assert revoked_old in ids, '超过宽限期的 revoked 应入选'
        assert revoked_fresh not in ids, '宽限期内不清理'
        assert stopped not in ids and active not in ids, '临时状态/活跃 Bot 不清理'
        assert await get_dead_bots_past_grace(0) == [], '0 = 禁用'

        # 部署基线：早于基线死亡的存量 Bot 不入选（保护已有数据）
        baseline = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        assert await get_dead_bots_past_grace(1, not_before=baseline) == [], \
            '基线之前死亡的存量 Bot 不清理'
        # 基线设在死亡之后 → 入选
        assert {b['id'] for b in await get_dead_bots_past_grace(1, not_before=old)} == {revoked_old}
        assert {b['id'] for b in await get_dead_bots_past_grace(1, not_before='2000-01-01 00:00:00')} == {revoked_old}


if __name__ == '__main__':
    unittest.main()
