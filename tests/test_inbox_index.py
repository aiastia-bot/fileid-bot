"""Inbox 队列查询索引回归测试

2026-11-19 事故：update_inbox 积累 9.6 万历史行后，_next() 的窗口函数查询
被计划器引到低选择性的 node_id 单列索引上（单机部署 node_id 全表同值），
对全表扫描 + TEMP B-TREE 排序；32 worker 每秒轮询 × ~36ms/次 ≈ 3-4 核，
把 4 核 VPS 打满。复合索引 (node_id, state, available_at) 让扫描范围
收敛到 eligible 行。
"""
import time

import pytest
from sqlalchemy import inspect, text

from db.core import get_engine
from services.inbox import Inbox


@pytest.fixture
def inbox():
    return Inbox(lambda key: object())   # 非 None 即视为已加载 Bot


async def test_composite_index_created(database):
    """migration v4 幂等补建复合索引（已有库走 create_all 不会补建新模型索引）"""
    async with get_engine().connect() as conn:
        names = await conn.run_sync(
            lambda sc: {i["name"] for i in inspect(sc).get_indexes("update_inbox")}
        )
    assert "ix_update_inbox_node_state_avail" in names


async def test_next_query_plan_uses_composite_index(database, inbox):
    """EXPLAIN 必须走复合索引；命中旧 node_id 单列索引或全表扫描即事故复现"""
    now = int(time.time())
    async with get_engine().connect() as conn:
        for i in range(40):
            await conn.execute(
                text(
                    "INSERT INTO update_inbox (bot_key, update_id, node_id, body, state,"
                    " attempts, available_at, error, created_at) VALUES (:b, :u,"
                    " 'standalone', '{}', :s, 0, :a, '', :c)"
                ),
                [
                    dict(b="bot1", u=i, s="completed", a=now, c=now - 5000 + i),
                    dict(b="bot2", u=1000 + i, s="pending", a=0, c=now - 10 + i),
                ],
            )
        await conn.commit()

        compiled = inbox._next_stmt.compile()
        binds = {k: getattr(v, "value", None) for k, v in compiled.params.items()}
        binds.update({"node_id": "standalone", "now": now})
        plan = (
            await conn.execute(
                text("EXPLAIN QUERY PLAN " + str(compiled)), binds
            )
        ).fetchall()
    detail = " | ".join(str(row[-1]) for row in plan)
    assert "ix_update_inbox_node_state_avail" in detail
    assert "ix_update_inbox_node_id" not in detail   # 事故索引不得再被选中
    assert "SCAN update_inbox" not in detail         # 全表扫描同样不得出现


async def test_next_stmt_is_reusable(database, inbox):
    """语句对象跨会话复用：同一 _next_stmt 连续执行仍返回正确候选"""
    from db.core import get_session
    from db.models import UpdateInbox
    from sqlalchemy import select

    now = int(time.time())
    async with get_session() as session:
        session.add(UpdateInbox(
            bot_key="b1", update_id=1, node_id="standalone", body="{}",
            state="pending", attempts=0, available_at=0, error="", created_at=now - 10,
        ))
        session.add(UpdateInbox(
            bot_key="b2", update_id=2, node_id="standalone", body="{}",
            state="pending", attempts=0, available_at=0, error="", created_at=now - 5,
        ))
        await session.commit()

    got = []
    for _ in range(4):
        job = await inbox._next()
        if job:
            got.append(job[0])
    assert got == ["b1", "b2"]   # 两个 bot 各领取一次，之后队列为空
    async with get_session() as session:
        states = dict(
            (row.bot_key, row.state)
            for row in (
                await session.execute(select(UpdateInbox))
            ).scalars()
        )
    assert set(states.values()) == {"processing"}
