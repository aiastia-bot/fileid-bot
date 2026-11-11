"""用户级限流（redis_manager）回归测试

背景：2026-11-11 线上全站停摆 8 小时——被限速的检查本身也会往窗口写入记录，
rate_limit_wait 的重查不断自我续期，窗口永远无法腾空，handler 无限排队直至
59 秒 webhook 超时，把全部 inbox worker 拖死。本文件锁死该行为。
"""
import asyncio
import time
from types import SimpleNamespace

from redis_manager import RedisManager


async def test_blocked_check_does_not_extend_window(monkeypatch):
    """被拒绝的检查不得写入窗口：否则重查自我续期，限速永远无法解除"""
    m = RedisManager()
    fake_now = [1000.0]
    monkeypatch.setattr(
        "redis_manager.time", SimpleNamespace(time=lambda: fake_now[0])
    )

    assert (await m.rate_limit_check("k", limit=2, window=60))[0]
    assert (await m.rate_limit_check("k", limit=2, window=60))[0]

    # 第 3 个请求被拒，按最早一条记录给出等待时长
    allowed, current, retry_after = await m.rate_limit_check("k", limit=2, window=60)
    assert not allowed and current == 2
    assert 55 <= retry_after <= 60

    # 模拟等待循环在窗口尾部的多次重查（修复前：每次重查追加记录自我续期）
    fake_now[0] = 1055.0
    for _ in range(10):
        allowed, _, retry_after = await m.rate_limit_check("k", limit=2, window=60)
        assert not allowed
        assert retry_after <= 5 + 1e-6

    # 窗口自然过期后必须放行（修复前：1055 的重查记录撑住了窗口，永远拒绝）
    fake_now[0] = 1060.1
    assert (await m.rate_limit_check("k", limit=2, window=60))[0]


async def test_rate_limit_wait_unblocks_when_window_expires():
    """排队等待必须在窗口过期后拿到名额，而不是无限自旋（线上事故回归）

    复现事故机制：sleep 上限 1 秒 → 重查频率 1 次/秒；修复前每次重查
    都往窗口追加记录，追加速率追平过期速率，current 永远 >= limit。
    """
    m = RedisManager()
    key = f"k{time.monotonic()}"
    limit, window = 2, 6
    assert await m.rate_limit_wait(key, limit, window, max_wait=30)
    assert await m.rate_limit_wait(key, limit, window, max_wait=30)

    # 第三个请求排队：窗口过期后必须放行；修复前会无限自旋
    third = asyncio.ensure_future(
        m.rate_limit_wait(key, limit, window, max_wait=30)
    )
    done, _ = await asyncio.wait({third}, timeout=9)
    assert third in done
    assert third.result() is True
