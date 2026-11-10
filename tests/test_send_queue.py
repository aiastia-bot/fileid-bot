"""SendQueue 消费者逻辑的本地模拟测试

不依赖真实 Telegram / Redis / DB，全部用 mock：
- send_batch 被 monkeypatch 成可控的 fake（可注入异常）
- Redis 持久化被禁用（走内存）
- 验证：公平性、/stop 取消、RetryAfter 限流、拉黑 Bot、并发安全

运行：
    python -m pytest tests/test_send_queue.py -v
或直接：
    python tests/test_send_queue.py
"""
import asyncio
import logging
import os
import sys
import time
import types
import unittest

# 让脚本能直接 import 项目根目录的模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 在 import 任何项目模块前，把限速/延迟降到接近 0，让测试跑得快
os.environ.setdefault('SEND_MIN_INTERVAL', '0')
os.environ.setdefault('SEND_BATCH_DELAY', '0')
os.environ.setdefault('SEND_INDIVIDUAL_DELAY', '0')
os.environ.setdefault('BOT_MIN_API_INTERVAL', '0')

from send_queue import SendQueue, SendTask  # noqa: E402
# 测试里直接用 senders 模块对象替换 send_batch
import senders  # noqa: E402

logging.basicConfig(level=logging.WARNING)


# ===== fake send_batch 工厂 =====

class FakeSendController:
    """控制 fake send_batch 的行为：记录调用、按 chat_id 抛指定异常"""

    def __init__(self):
        # chat_id -> 触发该 chat 时抛的异常（None 表示正常）
        self.exceptions: dict = {}
        # 记录所有成功的发送：(chat_id, file_count) 按时间顺序
        self.sent_log: list = []
        # 每次 send_batch 的延迟
        self.delay = 0.0
        # 锁，防止并发记录日志混乱
        self._lock = asyncio.Lock()

    def make_send_batch(self):
        async def fake_send_batch(bot, chat_id, files, caption="",
                                  protect_content=False, auto_delete=0):
            if self.delay:
                await asyncio.sleep(self.delay)
            exc = self.exceptions.get(chat_id)
            if exc is not None:
                # 异常触发一次后自动清除（模拟瞬时错误），除非显式重新设置
                del self.exceptions[chat_id]
                raise exc
            async with self._lock:
                self.sent_log.append((chat_id, len(files)))
            return len(files)
        return fake_send_batch


def make_queue(controller, bot_name="testbot"):
    """构造一个禁用 Redis、注入 fake send_batch 的 SendQueue

    关键：消费循环里是 `from senders import send_batch`，所以要替换的是
    senders 模块的属性，而不是 send_queue 模块的。
    """
    q = SendQueue(bot_name, durable=False)
    import send_queue
    send_queue.SEND_BATCH_DELAY = 0
    send_queue.SEND_MIN_INTERVAL = 0
    q._bot = object()  # 占位，不被 fake 使用
    # 让 _get_redis 返回 None（纯内存）
    q._redis = False
    # 记录原始 send_batch，测试结束后恢复
    original = senders.send_batch
    senders.send_batch = controller.make_send_batch()
    q.start()
    return q, original


async def stop(q, original=None):
    await q.stop()
    if original is not None:
        senders.send_batch = original


def wait_pending(q, timeout=10.0):
    """等队列消费到空（或超时）。

    timeout 默认 10s：RetryAfter 触发后会暂停整个队列 retry_after+3s，
    需留足时间让重试完成。
    """
    async def _wait():
        deadline = time.monotonic() + timeout
        while q.pending > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
    return _wait()


async def wait_sent(controller, expected, timeout=10.0):
    """等发送总数达到 expected（按 sent_log 累计）"""
    deadline = time.monotonic() + timeout
    while sum(n for _, n in controller.sent_log) < expected and time.monotonic() < deadline:
        await asyncio.sleep(0.01)


def make_files(n, ftype='photo'):
    return [{'file_type': ftype, 'telegram_file_id': f'id_{i}'} for i in range(n)]


# ===== 测试用例 =====

class TestFairness(unittest.IsolatedAsyncioTestCase):
    """R1: 多用户 Round-Robin 公平性"""

    async def test_multiple_users_round_robin(self):
        """3 个用户各提交 3 个任务，应交错发送而非一个用户发完再下一个"""
        controller = FakeSendController()
        q, orig = make_queue(controller)
        try:
            # 给一点延迟让交错可见
            controller.delay = 0.02
            # 用户 A/B/C 各提交 3 个单文件任务
            for chat_id in [100, 200, 300]:
                for _ in range(3):
                    q.submit_batch_async(chat_id, make_files(1))

            # 等满 9 个文件发送完成（而非等 pending==0，避免最后一个正在发送时误判）
            await wait_sent(controller, 9, timeout=5.0)

            # 提取发送顺序里的 chat_id
            order = [cid for cid, _ in controller.sent_log]
            self.assertEqual(len(order), 9, f"应发送 9 个，实际 {len(order)}")

            # 公平性验证：前 3 个发送应覆盖 3 个不同用户（每人一个）
            first_three = set(order[:3])
            self.assertEqual(first_three, {100, 200, 300},
                             f"前 3 个发送应覆盖所有用户，实际顺序 {order}")

            # 不应出现某个用户在任意 4 连发窗口里出现 3 次以上（即独占调度）
            # Round-Robin 下每用户最多 ceil(4/3)=2 次，允许 2，禁止 3
            from collections import Counter
            for i in range(len(order) - 3):
                window = order[i:i+4]
                counts = Counter(window)
                top = counts.most_common(1)[0][1]
                self.assertLessEqual(top, 2,
                                     f"位置 {i} 出现用户独占调度: {window}")
        finally:
            await stop(q, orig)

    async def test_submit_batch_returns_count(self):
        """submit_batch（同步等待版）应返回成功发送数"""
        controller = FakeSendController()
        q, orig = make_queue(controller)
        try:
            sent = await q.submit_batch(999, make_files(3))
            self.assertEqual(sent, 3)
        finally:
            await stop(q, orig)


class TestStopCancel(unittest.IsolatedAsyncioTestCase):
    """R3: /stop 取消（cancel_chat）"""

    async def test_cancel_pending_tasks(self):
        """取消排队中的任务：剩余任务不应被发送"""
        controller = FakeSendController()
        controller.delay = 0.1  # 拖慢消费，确保取消时还有排队
        q, orig = make_queue(controller)
        try:
            # 用户 100 先提交 1 个（会先开始发送，占用 0.1s）
            q.submit_batch_async(100, make_files(1))
            # 用户 200 提交 5 个
            for _ in range(5):
                q.submit_batch_async(200, make_files(1))

            await asyncio.sleep(0.05)  # 让用户 100 开始发送
            # 取消用户 200
            stopped = q.cancel_chat(200)
            self.assertGreater(stopped, 0, "应取消至少一个任务")

            await wait_pending(q, timeout=2.0)

            sent_for_200 = [n for cid, n in controller.sent_log if cid == 200]
            # 用户 200 的任务可能已经发了 0~1 个（取决于时序），但绝不应发完全部 5 个
            self.assertLess(len(sent_for_200), 5,
                            f"用户 200 应被取消，实际发送 {len(sent_for_200)} 个")
        finally:
            await stop(q, orig)

    async def test_cancel_marks_chat(self):
        """cancel_chat 后，is_chat_cancelled 应为 True 直到清理"""
        controller = FakeSendController()
        controller.delay = 0.2
        q, orig = make_queue(controller)
        try:
            q.submit_batch_async(500, make_files(3))
            await asyncio.sleep(0.01)
            q.cancel_chat(500)
            self.assertTrue(q.is_chat_cancelled(500))
        finally:
            await stop(q, orig)


class TestRetryAfter(unittest.IsolatedAsyncioTestCase):
    """R2: RetryAfter 限流"""

    async def test_retryafter_pauses_and_retries(self):
        """触发 RetryAfter 后：队列暂停，任务放回，恢复后重新发送

        消费循环对 RetryAfter 的处理是 retry_after + 3s 暂停，所以本测试
        用 wait_sent 等最终发送成功，而非依赖固定超时。
        """
        from telegram.error import RetryAfter
        controller = FakeSendController()
        q, orig = make_queue(controller)
        try:
            # 用户 100 首次发送抛 RetryAfter，controller 会自动清除异常，重试时正常
            controller.exceptions[100] = RetryAfter(0.1)

            q.submit_batch_async(100, make_files(2))
            t0 = time.monotonic()
            # 等待 2 个文件最终发送成功（限流暂停 ~3.1s 后重试）
            await wait_sent(controller, 2, timeout=8.0)
            elapsed = time.monotonic() - t0

            # 应该至少等待了 ~3s（RetryAfter 0.1 + 缓冲 3）
            self.assertGreater(elapsed, 2.5, "应在 RetryAfter 后等待约 3s 再重试")
            sent_for_100 = [n for cid, n in controller.sent_log if cid == 100]
            self.assertEqual(sum(sent_for_100), 2, "重试后应发送全部 2 个文件")
        finally:
            await stop(q, orig)

    async def test_retryafter_sets_rate_limit_until(self):
        """触发 RetryAfter 后 _rate_limit_until 应被设置"""
        from telegram.error import RetryAfter
        controller = FakeSendController()
        q, orig = make_queue(controller)
        try:
            controller.exceptions[100] = RetryAfter(0.1)
            q._rate_limit_until = 0.0

            q.submit_batch_async(100, make_files(1))
            await asyncio.sleep(0.05)  # 让限流触发

            self.assertGreater(q._rate_limit_until, 0.0,
                              "_rate_limit_until 应被设置")
            # 清掉异常避免无限重试
            controller.exceptions.clear()
        finally:
            await stop(q, orig)


class TestBlocked(unittest.IsolatedAsyncioTestCase):
    """R4: 拉黑 Bot"""

    async def test_blocked_cancels_remaining(self):
        """用户拉黑 Bot：该用户剩余任务全部取消，future 收到异常"""
        # 必须用 senders 模块里的 SendBlockedError，消费循环里 isinstance 才能匹配
        SendBlockedError = senders.SendBlockedError

        controller = FakeSendController()
        q, orig = make_queue(controller)
        try:
            controller.delay = 0.05
            # 用户 100 的所有发送都抛 SendBlockedError
            controller.exceptions[100] = SendBlockedError("blocked")

            # 提交 3 个任务，收集 future 结果
            tasks = [q.submit_batch_async(100, make_files(1)) for _ in range(3)]
            await wait_pending(q, timeout=5.0)

            # 所有 future 都应结束（被 cancel 或 set_exception）
            done_count = sum(1 for t in tasks if t.future.done())
            self.assertEqual(done_count, 3, "所有任务 future 都应结束")
        finally:
            await stop(q, orig)

    async def test_blocked_does_not_affect_other_users(self):
        """用户 A 拉黑，用户 B 应正常发送"""
        SendBlockedError = senders.SendBlockedError

        controller = FakeSendController()
        q, orig = make_queue(controller)
        try:
            controller.delay = 0.02
            controller.exceptions[100] = SendBlockedError("blocked")

            q.submit_batch_async(100, make_files(1))
            q.submit_batch_async(200, make_files(2))
            # 只等用户 200 的 2 个文件发完即可（用户 100 会反复抛异常被清理）
            await wait_sent(controller, 2, timeout=5.0)

            sent_200 = [n for cid, n in controller.sent_log if cid == 200]
            self.assertEqual(sum(sent_200), 2, "用户 200 应正常发送")
        finally:
            await stop(q, orig)


class TestQueueInfo(unittest.IsolatedAsyncioTestCase):
    """queue_info / pending 等辅助方法"""

    async def test_queue_info_position(self):
        controller = FakeSendController()
        controller.delay = 0.2
        q, orig = make_queue(controller)
        try:
            # 先占住消费
            q.submit_batch_async(100, make_files(1))
            await asyncio.sleep(0.02)

            # 用户 200 排 3 个
            for _ in range(3):
                q.submit_batch_async(200, make_files(1))
            # 用户 300 排 2 个
            for _ in range(2):
                q.submit_batch_async(300, make_files(1))

            info_200 = q.queue_info(200)
            self.assertEqual(info_200['user_pending'], 3)
            info_300 = q.queue_info(300)
            self.assertEqual(info_300['user_pending'], 2)
            # 300 排在 200 后面，position 应更大
            self.assertGreaterEqual(info_300['position'], 1)
        finally:
            await stop(q, orig)


class TestSplitBatches(unittest.TestCase):
    """split_files_to_batches 分组逻辑（#3 优化验证）"""

    def test_mixed_types_grouped(self):
        from send_queue import split_files_to_batches
        files = [
            {'file_type': 'photo'}, {'file_type': 'photo'},
            {'file_type': 'video'},
            {'file_type': 'document'},
            {'file_type': 'audio'}, {'file_type': 'audio'},
            {'file_type': 'voice'},
        ]
        batches = split_files_to_batches(files, batch_size=10)
        # 应分 3 组：photo+video, document+voice, audio
        self.assertEqual(len(batches), 3)
        # 第一组 photo+video
        self.assertEqual(len(batches[0]), 3)
        self.assertEqual(len(batches[1]), 2)  # document+voice
        self.assertEqual(len(batches[2]), 2)  # audio

    def test_respects_batch_size(self):
        from send_queue import split_files_to_batches
        files = [{'file_type': 'photo'} for _ in range(7)]
        batches = split_files_to_batches(files, batch_size=3)
        self.assertEqual(len(batches), 3)  # 3+3+1
        self.assertEqual(len(batches[0]), 3)
        self.assertEqual(len(batches[2]), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
