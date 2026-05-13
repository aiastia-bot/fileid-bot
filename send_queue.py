"""统一发送队列 - 每 Bot 一个队列，Round-Robin 用户公平调度

架构:
  Handler 层 → 拆分文件为批次 → submit 到队列 → 等待完成
  队列消费者 → Round-Robin 取各用户的批次 → 调 send_batch → 固定间隔

好处:
  - 天然限速: 单消费者 + 固定间隔 = 恒定发送速率
  - 公平调度: 多用户交替发送，不会一个用户占满整个 Bot
  - 代码简洁: 发送逻辑集中在一处
"""
import asyncio
import logging
import time
from collections import OrderedDict
from typing import List, Dict, Optional

from config import SEND_BATCH_DELAY, SEND_MIN_INTERVAL

logger = logging.getLogger(__name__)


# ===== 全局注册表 =====
_queues: Dict[str, "SendQueue"] = {}


def get_queue(bot_name: str) -> "SendQueue":
    """获取或创建某 Bot 的发送队列"""
    if bot_name not in _queues:
        q = SendQueue(bot_name)
        _queues[bot_name] = q
        q.start()
        logger.info("SendQueue(@%s): 已创建并启动", bot_name)
    return _queues[bot_name]


def get_queue_from_context(context) -> "SendQueue":
    """从 context 获取当前 Bot 的发送队列"""
    bot_name = getattr(context.bot, 'username', 'unknown')
    q = get_queue(bot_name)
    q.set_bot(context.bot)
    return q


async def stop_all_queues():
    """停止所有队列消费者（进程退出时调用）"""
    for q in _queues.values():
        await q.stop()
    _queues.clear()


# ===== 任务 =====

class SendTask:
    """一个发送批次"""
    __slots__ = ('chat_id', 'files', 'caption', 'future', 'auto_id')

    def __init__(self, chat_id: int, files: List[Dict], caption: str = "",
                 auto_id: str = None):
        self.chat_id = chat_id
        self.files = files
        self.caption = caption
        self.future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.auto_id = auto_id  # 用于 auto_send 取消


# ===== 队列 =====

class SendQueue:
    """每 Bot 的发送队列，Round-Robin 用户公平调度"""

    def __init__(self, bot_name: str):
        self.bot_name = bot_name
        self._bot = None
        self._queues: OrderedDict[int, List[SendTask]] = OrderedDict()
        self._running = False
        self._consumer_task: Optional[asyncio.Task] = None
        self._event = asyncio.Event()
        self._last_send = 0.0
        self._total_sent = 0

    def set_bot(self, bot):
        """设置 bot 实例（用于发送）"""
        self._bot = bot

    def start(self):
        """启动消费者"""
        self._running = True
        self._consumer_task = asyncio.create_task(self._consume_loop())

    async def stop(self):
        """停止消费者"""
        self._running = False
        self._event.set()
        # 取消所有等待中的任务
        for tasks in self._queues.values():
            for t in tasks:
                if not t.future.done():
                    t.future.cancel()
        self._queues.clear()
        if self._consumer_task:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass

    @property
    def pending(self) -> int:
        """队列中待处理任务总数"""
        return sum(len(v) for v in self._queues.values())

    def queue_info(self, chat_id: int) -> Dict:
        """获取指定用户的队列状态"""
        user_pending = len(self._queues.get(chat_id, []))
        total_pending = self.pending
        position = 0
        for cid, tasks in self._queues.items():
            if cid == chat_id:
                break
            position += len(tasks)
        return {
            'user_pending': user_pending,
            'total_pending': total_pending,
            'position': position + 1 if user_pending > 0 else 0,
        }

    # ===== 提交方式 =====

    async def submit_batch(self, chat_id: int, files: List[Dict],
                           caption: str = "", auto_id: str = None) -> int:
        """提交一个批次并等待完成。返回成功发送数量。"""
        task = SendTask(chat_id, files, caption, auto_id)
        if chat_id not in self._queues:
            self._queues[chat_id] = []
        self._queues[chat_id].append(task)
        self._event.set()
        return await task.future

    def submit_batch_async(self, chat_id: int, files: List[Dict],
                           caption: str = "", auto_id: str = None) -> SendTask:
        """提交一个批次但不等待。返回 SendTask（可通过 await task.future 获取结果）。"""
        task = SendTask(chat_id, files, caption, auto_id)
        if chat_id not in self._queues:
            self._queues[chat_id] = []
        self._queues[chat_id].append(task)
        self._event.set()
        return task

    def cancel_auto(self, chat_id: int, auto_id: str) -> int:
        """取消指定用户的 auto_send 任务"""
        tasks = self._queues.get(chat_id, [])
        removed = 0
        for t in tasks[:]:
            if t.auto_id == auto_id:
                if not t.future.done():
                    t.future.cancel()
                tasks.remove(t)
                removed += 1
        if not tasks and chat_id in self._queues:
            del self._queues[chat_id]
        if removed:
            logger.info("SendQueue(@%s): 取消 %d 个 auto_send 任务 (chat_id=%s, auto_id=%s)",
                        self.bot_name, removed, chat_id, auto_id)
        return removed

    # ===== 消费者 =====

    async def _consume_loop(self):
        """消费者主循环：Round-Robin 从各用户取任务"""
        from senders import send_batch

        while self._running:
            # 等待任务
            if not self._queues:
                self._event.clear()
                await self._event.wait()
                if not self._running:
                    break

            # Round-Robin: 从每个用户取一个任务
            processed_any = False
            chat_ids = list(self._queues.keys())

            for chat_id in chat_ids:
                if not self._running:
                    break

                tasks = self._queues.get(chat_id)
                if not tasks:
                    continue

                task = tasks.pop(0)
                if not tasks:
                    del self._queues[chat_id]

                processed_any = True

                # 最小发送间隔
                now = time.monotonic()
                wait = SEND_MIN_INTERVAL - (now - self._last_send)
                if wait > 0:
                    await asyncio.sleep(wait)

                # 发送
                try:
                    sent = await send_batch(self._bot, task.chat_id,
                                            task.files, task.caption)
                    self._last_send = time.monotonic()
                    self._total_sent += sent
                    if not task.future.done():
                        task.future.set_result(sent)
                except Exception as e:
                    from telegram.error import RetryAfter
                    from senders import SendBlockedError

                    if isinstance(e, RetryAfter):
                        # 限流：等待 TG 要求的时间后重新入队，不丢弃任务
                        wait = (e.retry_after if hasattr(e, 'retry_after') and e.retry_after else 30) + 3
                        logger.warning("SendQueue(@%s): 限流等待 %.0fs 后重试 (chat_id=%s, 剩余队列=%d)",
                                       self.bot_name, wait, task.chat_id, self.pending)
                        await asyncio.sleep(wait)
                        # 重新入队到该用户队列头部
                        if chat_id not in self._queues:
                            self._queues[chat_id] = []
                        self._queues[chat_id].insert(0, task)
                        continue  # 跳过批次间隔，直接进入下一轮
                    elif isinstance(e, SendBlockedError):
                        # 用户已拉黑 Bot：取消该用户所有剩余任务
                        cancelled = 0
                        remaining = self._queues.pop(chat_id, [])
                        for t in remaining:
                            if not t.future.done():
                                t.future.cancel()
                                cancelled += 1
                        logger.warning("SendQueue(@%s): chat_id=%s 已拉黑 Bot，取消剩余 %d 个任务",
                                       self.bot_name, chat_id, cancelled)
                        if not task.future.done():
                            task.future.set_exception(e)
                    else:
                        logger.error("SendQueue(@%s): 发送失败 chat_id=%s: %s",
                                     self.bot_name, task.chat_id, e)
                        if not task.future.done():
                            task.future.set_exception(e)

                # 批次间固定间隔
                await asyncio.sleep(SEND_BATCH_DELAY)

            if not processed_any:
                await asyncio.sleep(0.05)


# ===== 辅助函数：按类型拆分文件为批次 =====

def split_files_to_batches(files: List[Dict], batch_size: int = None) -> List[List[Dict]]:
    """按类型分组后拆成批次，同类型文件在一起（可合并为相册）
    
    顺序: 图片/视频 → 文档 → 音频
    返回: [[batch1_files], [batch2_files], ...]
    """
    from config import GROUP_SEND_SIZE
    size = batch_size or GROUP_SEND_SIZE

    photo_video = [f for f in files if f['file_type'] in ('photo', 'video')]
    documents = [f for f in files if f['file_type'] in ('document', 'voice')]
    audios = [f for f in files if f['file_type'] == 'audio']

    batches = []
    for group in [photo_video, documents, audios]:
        for i in range(0, len(group), size):
            batches.append(group[i:i + size])

    return batches