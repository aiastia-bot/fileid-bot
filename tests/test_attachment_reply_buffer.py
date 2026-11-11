"""附件回复缓冲回归测试：占位消息 + 进度编辑 + 静默合并

单附件保持直接回复；批量上传发一次占位、靠编辑报进度、静默/15秒后
原位编辑成汇总（批量全程只发 1 条新消息，编辑走独立限速预算）。
"""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers import messages


class FakeBot:
    def __init__(self):
        self.sent = []      # (chat_id, text, kwargs)
        self.edits = []     # (text, chat_id, message_id, kwargs)
        self.deleted = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=100 + len(self.sent))

    async def edit_message_text(self, text, chat_id=None, message_id=None, **kwargs):
        self.edits.append((text, chat_id, message_id, kwargs))
        return True

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True


def make_context():
    return SimpleNamespace(bot_data={}, bot=FakeBot())


async def wait_until(pred, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(0.02)  # 先让出循环，再检查：缓冲任务是异步建立的
        if pred():
            return True
    return pred()


@pytest.fixture
def fast_send(monkeypatch):
    async def _pass(fn, *a, **kw):
        return await fn(*a, **kw)
    monkeypatch.setattr(messages, "_retry_send", _pass)


@pytest.fixture
def no_hint(monkeypatch):
    monkeypatch.setattr(messages, "_file_quota_hint_text", AsyncMock(return_value="HINT"))


def add(context, chat_id, user_id, n, code="ABC123", mid=11, over=False):
    return asyncio.ensure_future(
        messages._buffer_attachment_reply(
            context, chat_id, user_id, ("照片", f"{code}{n}", mid, over))
    )


async def test_single_attachment_keeps_direct_reply(monkeypatch, fast_send):
    monkeypatch.setattr(messages, "ATTACH_REPLY_DEBOUNCE", 0.05)
    context = make_context()

    add(context, 1, 2, 1)
    assert await wait_until(
        lambda: not context.bot_data.get("_attachment_reply_buffer")
    )

    # 单附件：一条 Markdown 直接回复（reply_to 原消息），无占位无编辑
    assert len(context.bot.sent) == 1
    chat_id, text, kwargs = context.bot.sent[0]
    assert "`ABC1231`" in text and "已保存" in text
    assert kwargs.get("reply_to_message_id") == 11
    assert kwargs.get("parse_mode") == "Markdown"
    assert context.bot.edits == []


async def test_batch_uses_placeholder_then_final_edit(monkeypatch, fast_send, no_hint):
    monkeypatch.setattr(messages, "ATTACH_REPLY_DEBOUNCE", 0.1)
    context = make_context()

    add(context, 1, 2, 1)   # 第 1 个：只建缓冲
    add(context, 1, 2, 2)   # 第 2 个：触发占位
    add(context, 1, 2, 3)   # 第 3 个：触发进度编辑

    # 占位已发出（仅一次新消息），且有「已收到 3」的进度编辑
    assert await wait_until(lambda: len(context.bot.sent) == 1)
    assert "已收到" in context.bot.sent[0][1]
    assert await wait_until(
        lambda: any("已收到 3" in e[0] for e in context.bot.edits)
    )

    # 静默后占位原位编辑成汇总（不再发新消息）
    assert await wait_until(
        lambda: any("✅ 已保存 3 个文件" in e[0] for e in context.bot.edits)
    )
    assert len(context.bot.sent) == 1
    final = next(e for e in context.bot.edits if "已保存" in e[0])
    assert final[2] == 101  # 占位消息 id，原位编辑


async def test_max_wait_flushes_during_stream(monkeypatch, fast_send, no_hint):
    """连续上传不断重置静默计时器时，强制上限必须兜底发出汇总"""
    monkeypatch.setattr(messages, "ATTACH_REPLY_DEBOUNCE", 30)   # 静默永不触发
    monkeypatch.setattr(messages, "ATTACH_REPLY_MAX_WAIT", 0.2)
    context = make_context()

    add(context, 1, 2, 1)
    for n in range(2, 7):
        await asyncio.sleep(0.05)
        add(context, 1, 2, n)  # 每 50ms 一个附件，静默计时器不断顺延

    def has_summary():
        texts = [t for _, t, _ in context.bot.sent] + [e[0] for e in context.bot.edits]
        return any("✅ 已保存" in t for t in texts)

    assert await wait_until(has_summary)
    # 过程反馈存在（占位消息发出）
    assert any("已收到" in t for _, t, _ in context.bot.sent)


async def test_summary_html_blockquote_and_quota_hint(monkeypatch, no_hint):
    items = [("照片", f"C{n}", n, False) for n in range(20)]
    items.append(("文档", "CX", 99, True))  # 一个超额项

    text = await messages._build_attachment_summary_text(items, len(items), ok_count=20)
    assert "<blockquote expandable>" in text      # >15 个折叠
    assert "<code>C0</code>" in text
    assert "已超出文件额度" in text
    assert "HINT" in text

    small = await messages._build_attachment_summary_text(items[:3], 3, ok_count=3)
    assert "blockquote" not in small and "HINT" not in small


async def test_shutdown_flush_sends_pending_summary(fast_send, no_hint):
    context = make_context()
    app = SimpleNamespace(bot_data=context.bot_data, bot=context.bot)
    await add(app, 1, 2, 1)
    await add(app, 1, 2, 2)

    await messages.flush_all_attachment_reply_buffers(app)

    assert not app.bot_data["_attachment_reply_buffer"]
    texts = [t for _, t, _ in app.bot.sent] + [e[0] for e in app.bot.edits]
    assert any("✅ 已保存 2 个文件" in t for t in texts)
