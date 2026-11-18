"""Inbox 取任务公平性回归测试

2026-11-18 事故：_next 只看全局最老 100 条，加载失败 Bot 的积压
填满队头后静默跳过，全站零吞吐饿死 4 天且零日志。修复后：
候选 = 每个 bot 的最老一条（窗口函数），并按调用轮转起点 + 死 Bot 告警。
"""
import logging
import time

import pytest
from sqlalchemy import select

from db.core import get_session
from db.models import UpdateInbox
from services.inbox import Inbox


async def insert_update(bot_key, update_id, created_at):
    """直接插行以精确控制 created_at（accept 用当前时间，无法构造队头阻塞）"""
    async with get_session() as session:
        session.add(UpdateInbox(
            bot_key=bot_key, update_id=update_id, node_id="standalone",
            body=f'{{"update_id": {update_id}}}', state="pending",
            attempts=0, available_at=0, error="",
            created_at=created_at,
        ))
        await session.commit()


@pytest.fixture
def alive_app():
    return {"ok": True}


async def claimed_bot_keys(inbox, n):
    got = []
    for _ in range(n):
        job = await inbox._next()
        if job:
            got.append(job[0])
    return got


async def test_dead_bot_head_does_not_starve_others(database, alive_app):
    """事故场景：死 Bot 的积压占据队头，健康 Bot 的新消息必须仍被处理"""
    now = int(time.time())
    dead, healthy = now - 86400, now - 1
    for i in range(6):  # 死 Bot 6 条陈旧积压
        await insert_update("dead", 100 + i, dead + i)
    await insert_update("master", 500, healthy)

    inbox = Inbox(lambda key: alive_app if key == "master" else None)
    got = await claimed_bot_keys(inbox, 8)

    assert got[0] == "master"           # 新消息立即被处理，不等死 Bot
    assert set(got) == {"master"}       # 死 Bot 的行永远不被领取
    async with get_session() as session:
        states = dict(
            (row.update_id, row.state)
            for row in (
                await session.execute(
                    select(UpdateInbox).where(UpdateInbox.bot_key == "dead")
                )
            ).scalars()
        )
    assert set(states.values()) == {"pending"}  # 死 Bot 积压原样保留


async def test_flood_bot_does_not_delay_other_bot(database, alive_app):
    """单一 bot 的洪峰不得把其他 bot 的新消息压后整个积压长度"""
    now = int(time.time())
    for i in range(5):  # 洪峰 bot 5 条更老的积压
        await insert_update("flood", 10 + i, now - 100 + i)
    await insert_update("master", 900, now - 1)

    inbox = Inbox(lambda key: alive_app)
    got = await claimed_bot_keys(inbox, 3)

    # 轮转起点下，master 的消息必须在前几次领取内轮到（而非排完 5 条洪峰后）
    assert "master" in got[:2]
    assert got.count("master") >= 1


async def test_dead_bot_warning_emitted_once_per_hour(database, caplog):
    now = int(time.time())
    await insert_update("dead", 1, now - 10)
    inbox = Inbox(lambda key: None)
    with caplog.at_level(logging.ERROR, logger="services.inbox"):
        await claimed_bot_keys(inbox, 3)
    warnings = [r for r in caplog.records if "应用未加载" in r.message]
    assert len(warnings) == 1          # 3 次轮询只告警一次，防刷屏
    assert inbox._dead_bot_warned["dead"] <= time.time()


async def test_dead_bot_warning_suppressed_during_startup(database, caplog):
    """启动加载期 resolve_app 落空是暂态，不应触发死 Bot 告警"""
    now = int(time.time())
    await insert_update("dead", 1, now - 10)
    inbox = Inbox(lambda key: None)
    inbox.suppress_dead_bot_warnings = True
    with caplog.at_level(logging.ERROR, logger="services.inbox"):
        await claimed_bot_keys(inbox, 2)
    assert not [r for r in caplog.records if "应用未加载" in r.message]
    assert not inbox._dead_bot_warned

    # 加载完成（开关复位）后恢复告警
    inbox.suppress_dead_bot_warnings = False
    with caplog.at_level(logging.ERROR, logger="services.inbox"):
        await claimed_bot_keys(inbox, 1)
    assert any("应用未加载" in r.message for r in caplog.records)
