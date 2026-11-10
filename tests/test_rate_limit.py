import asyncio
import json
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import RetryAfter, TimedOut
from telegram.ext import ExtBot
from telegram.request import BaseRequest

from services.rate_limit import BotRateLimiter, RateLimitExhausted


def limiter(**changes):
    options = dict(
        api_interval=0,
        messages_per_second=10000,
        private_interval=0,
        group_interval=0,
        edit_interval=0,
        max_inflight=8,
        retries=1,
        max_wait=1,
        jitter=0,
    )
    return BotRateLimiter(**(options | changes))


async def request(gate, callback, endpoint="sendMessage", data=None):
    return await gate.process_request(callback, (), {}, endpoint, data or {}, None)


async def test_private_chat_pacing_includes_album_weight():
    gate = limiter(private_interval=0.02)
    starts = []

    async def callback():
        starts.append(time.monotonic())
        return True

    await request(
        gate, callback, "sendMediaGroup", {"chat_id": 1, "media": [{}, {}, {}]}
    )
    await request(gate, callback, data={"chat_id": "1"})
    assert starts[1] - starts[0] >= 0.05


async def test_group_alias_and_numeric_id_share_budget():
    gate = limiter(private_interval=0.001, group_interval=0.04)
    callback = AsyncMock(return_value=True)
    await request(gate, callback, data={"chat_id": "-1"})
    start = time.monotonic()
    await request(gate, callback, data={"chat_id": -1})
    assert time.monotonic() - start >= 0.03


async def test_other_chats_and_callbacks_do_not_wait_for_one_chat():
    gate = limiter(private_interval=30)
    callback = AsyncMock(return_value=True)
    await request(gate, callback, data={"chat_id": 1})
    await asyncio.wait_for(request(gate, callback, data={"chat_id": 2}), 0.5)
    await asyncio.wait_for(
        request(gate, callback, "answerCallbackQuery", {"callback_query_id": "x"}), 0.5
    )
    assert callback.await_count == 3


async def test_network_call_does_not_hold_scheduler_lock():
    gate = limiter(max_inflight=2)
    started, release = asyncio.Event(), asyncio.Event()

    async def slow():
        started.set()
        await release.wait()
        return True

    pending = asyncio.create_task(request(gate, slow, data={"chat_id": 1}))
    try:
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(
            request(gate, AsyncMock(return_value=True), data={"chat_id": 2}), 0.5
        )
        assert not pending.done()
    finally:
        release.set()
        await pending
    assert gate.snapshot()["inflight"] == 0


async def test_inflight_limit():
    gate = limiter(max_inflight=2)
    active, maximum = 0, 0

    async def callback():
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        active -= 1
        return True

    await asyncio.gather(
        *(request(gate, callback, data={"chat_id": n}) for n in range(8))
    )
    assert maximum == 2


@pytest.mark.parametrize("duration", [0.06, timedelta(seconds=0.06)])
async def test_429_publishes_bot_cooldown(duration):
    gate = limiter()
    callback = AsyncMock(side_effect=[RetryAfter(duration), True])
    start = time.monotonic()
    assert await request(gate, callback, data={"chat_id": 1}) is True
    assert time.monotonic() - start >= 0.05
    assert gate.snapshot()["429"] == 1
    assert callback.await_count == 2


async def test_429_cooldown_covers_other_calls_but_not_other_bots():
    gate, other = limiter(), limiter()
    callback = AsyncMock(side_effect=[RetryAfter(0.12), True])
    pending = asyncio.create_task(request(gate, callback, data={"chat_id": 1}))
    try:
        async with asyncio.timeout(1):
            while gate.stats["429"] == 0:
                await asyncio.sleep(0.001)
        # 冷却截止时间在 stats["429"] 自增前的同一锁块内就已定死，
        # 因此从观察到 429 起算即可，与中间操作耗时无关（慢 CI 下
        # 下方 wait_for 本身可能吃掉几十毫秒）。
        assert not pending.done()  # 同步检查：冷却刚开始，重试不可能已完成
        cooldown_started_at = time.monotonic()

        # 其他 Bot 的限速器不受影响
        await asyncio.wait_for(
            request(other, AsyncMock(return_value=True), data={"chat_id": 1}), 1
        )

        # 同一 Bot 的其他调用必须等满冷却
        await request(gate, AsyncMock(return_value=True), "getMe")
        assert time.monotonic() - cooldown_started_at >= 0.12 - 0.01
    finally:
        await pending


async def test_exhaustion_keeps_full_long_cooldown_without_sleeping():
    gate = limiter(max_wait=0.1)
    with pytest.raises(RateLimitExhausted):
        await request(gate, AsyncMock(side_effect=RetryAfter(20)))
    assert gate.snapshot()["cooldown_seconds"] > 19
    assert gate.snapshot()["exhausted"] == 1


async def test_network_errors_are_not_retried_by_limiter():
    callback = AsyncMock(side_effect=TimedOut())
    with pytest.raises(TimedOut):
        await request(limiter(), callback)
    callback.assert_awaited_once()


async def test_cancellation_releases_scheduler():
    gate = limiter(private_interval=30)
    await request(gate, AsyncMock(return_value=True), data={"chat_id": 1})
    task = asyncio.create_task(request(gate, AsyncMock(), data={"chat_id": 1}))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(
        request(gate, AsyncMock(return_value=True), data={"chat_id": 2}), 0.5
    )
    assert gate.snapshot()["inflight"] == 0


async def test_progress_edits_have_separate_per_message_budget():
    gate = limiter(edit_interval=30)
    callback = AsyncMock(return_value=True)
    await request(gate, callback, "editMessageText", {"chat_id": 1, "message_id": 1})
    await asyncio.wait_for(
        request(gate, callback, "editMessageText", {"chat_id": 1, "message_id": 2}), 0.5
    )
    assert callback.await_count == 2


class FakeTelegram(BaseRequest):
    def __init__(self):
        self.calls = []
        self.fail_send = False

    @property
    def read_timeout(self):
        return 1

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, **kwargs):
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append(endpoint)
        if endpoint == "sendMessage" and self.fail_send:
            return 429, json.dumps(
                {
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 0.01},
                }
            ).encode()
        if endpoint == "getMe":
            result = {
                "id": 123456,
                "is_bot": True,
                "first_name": "Test",
                "username": "test_bot",
            }
        elif endpoint in ("sendMessage", "editMessageText"):
            result = {
                "message_id": 1,
                "date": 1,
                "chat": {"id": 1, "type": "private"},
                "text": "ok",
            }
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


async def test_direct_and_wrapped_extbot_calls_share_hook():
    from senders import _retry_send

    transport, gate = FakeTelegram(), limiter()
    bot = ExtBot(
        "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789",
        request=transport,
        rate_limiter=gate,
    )
    async with bot:
        await bot.send_message(1, "direct")
        await _retry_send(bot.send_message, chat_id=2, text="wrapped")
        await bot.answer_callback_query("callback")
        await bot.edit_message_text("edit", chat_id=1, message_id=1)
        await bot.delete_messages(chat_id=1, message_ids=[1, 2])
    assert transport.calls.count("sendMessage") == 2
    assert gate.stats["requests"] == len(transport.calls)
    assert {"getMe", "answerCallbackQuery", "editMessageText", "deleteMessages"} <= set(
        transport.calls
    )


async def test_wrappers_do_not_multiply_429_retries():
    from senders import _retry_send

    transport, gate = FakeTelegram(), limiter(retries=1)
    bot = ExtBot(
        "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ123456789",
        request=transport,
        rate_limiter=gate,
    )
    async with bot:
        transport.fail_send = True
        with pytest.raises(RateLimitExhausted):
            await _retry_send(bot.send_message, chat_id=1, text="limited")
    assert transport.calls.count("sendMessage") == 2


async def test_queue_does_not_retry_managed_exhaustion(monkeypatch):
    from send_queue import SendQueue

    callback = AsyncMock(side_effect=RateLimitExhausted(0.1))
    monkeypatch.setattr("senders.send_batch", callback)
    q = SendQueue("test", durable=False)
    q.set_bot(SimpleNamespace(id=1, username="test"))
    q.start()
    try:
        task = q.submit_batch_async(1, [{"file_type": "photo"}])
        with pytest.raises(RateLimitExhausted):
            await asyncio.wait_for(task.future, 1)
        callback.assert_awaited_once()
    finally:
        await q.stop()


async def test_queue_skips_chat_waiting_for_its_own_budget(monkeypatch):
    from send_queue import SendQueue

    deadline = time.monotonic() + 0.12
    fake_gate = SimpleNamespace(
        ready_in=lambda chat: max(0, deadline - time.monotonic()) if chat == 1 else 0
    )
    callback = AsyncMock(return_value=1)
    monkeypatch.setattr("senders.send_batch", callback)
    q = SendQueue("test", durable=False)
    q.set_bot(SimpleNamespace(id=1, username="test", rate_limiter=fake_gate))
    q.start()
    try:
        first = q.submit_batch_async(1, [{"file_type": "photo"}])
        second = q.submit_batch_async(2, [{"file_type": "photo"}])
        await asyncio.wait_for(asyncio.gather(first.future, second.future), 1)
        assert [call.args[1] for call in callback.call_args_list] == [2, 1]
    finally:
        await q.stop()


async def test_auto_delete_uses_batch_api(monkeypatch):
    from senders import _schedule_auto_delete

    bot = SimpleNamespace(delete_messages=AsyncMock())
    await _schedule_auto_delete(bot, 1, list(range(150)), 0)
    assert bot.delete_messages.await_count == 2
    assert [
        len(call.kwargs["message_ids"]) for call in bot.delete_messages.call_args_list
    ] == [100, 50]
