"""One scheduler per bot for all ExtBot requests, including direct API calls.

The rate permit is held only while reserving a slot, never across network I/O.
Limits are conservative pacing targets, not Telegram service guarantees.
"""

import asyncio
import math
import random
import time
import weakref
from collections import Counter

from telegram.error import RetryAfter
from telegram.ext import BaseRateLimiter

from config import (
    BOT_MIN_API_INTERVAL,
    API_MESSAGES_PER_SECOND,
    API_PRIVATE_INTERVAL,
    API_GROUP_INTERVAL,
    API_EDIT_INTERVAL,
    API_MAX_INFLIGHT,
    API_429_RETRIES,
    RETRY_AFTER_MAX_WAIT,
    API_429_JITTER,
)

_limiters = weakref.WeakValueDictionary()


def retry_seconds(value):
    value = value.total_seconds() if hasattr(value, "total_seconds") else value
    seconds = float(value or 0)
    return max(0.0, seconds) if math.isfinite(seconds) else 60.0


def chat_key(value):
    try:
        return int(value)
    except (ValueError, TypeError):
        return str(value).lower() if value is not None else None


class RateLimitExhausted(RetryAfter):
    """Cooldown is already recorded; wrappers/queues must not retry this again."""


class BotRateLimiter(BaseRateLimiter):
    def __init__(
        self,
        key="",
        *,
        api_interval=None,
        messages_per_second=None,
        private_interval=None,
        group_interval=None,
        edit_interval=None,
        max_inflight=None,
        retries=None,
        max_wait=None,
        jitter=None,
    ):
        self.key = key
        self.api_interval = (
            BOT_MIN_API_INTERVAL if api_interval is None else api_interval
        )
        self.message_interval = 1 / (
            API_MESSAGES_PER_SECOND
            if messages_per_second is None
            else messages_per_second
        )
        self.private_interval = (
            API_PRIVATE_INTERVAL if private_interval is None else private_interval
        )
        self.group_interval = (
            API_GROUP_INTERVAL if group_interval is None else group_interval
        )
        self.edit_interval = (
            API_EDIT_INTERVAL if edit_interval is None else edit_interval
        )
        self.max_inflight = API_MAX_INFLIGHT if max_inflight is None else max_inflight
        self.retries = API_429_RETRIES if retries is None else retries
        self.max_wait = RETRY_AFTER_MAX_WAIT if max_wait is None else max_wait
        self.jitter = API_429_JITTER if jitter is None else jitter
        self._condition = asyncio.Condition()
        self._next_api = self._next_message = self._cooldown_until = 0.0
        self._chats = {}
        self._edits = {}
        self._inflight = 0
        self.stats = Counter()
        self._operations = 0

    async def initialize(self):
        pass

    async def shutdown(self):
        # A temporary helper may share this limiter with a running Application.
        # It owns no transport/tasks; weak registry entries disappear with the bots.
        pass

    @staticmethod
    def _request(endpoint, data):
        endpoint = endpoint.lower()
        sending = endpoint.startswith("send") or endpoint in (
            "copymessage",
            "copymessages",
            "forwardmessage",
            "forwardmessages",
        )
        cost = 1
        if endpoint == "sendmediagroup":
            cost = max(1, len(data.get("media", [])))
        elif endpoint in ("copymessages", "forwardmessages"):
            cost = max(1, len(data.get("message_ids", [])))
        chat = chat_key(data.get("chat_id"))
        editing = endpoint.startswith("editmessage")
        edit_key = (
            (chat, data.get("message_id"), data.get("inline_message_id"))
            if editing
            else None
        )
        return sending, cost, chat, edit_key

    def _gc(self, now):
        self._operations += 1
        if (
            self._operations % 256 == 0
            or len(self._chats) > 20000
            or len(self._edits) > 20000
        ):
            self._chats = {
                key: until for key, until in self._chats.items() if until > now
            }
            self._edits = {
                key: until for key, until in self._edits.items() if until > now
            }

    def ready_in(self, chat_id):
        """Queue scheduling hint; the request hook still enforces the actual limits."""
        now = time.monotonic()
        return max(
            0,
            self._cooldown_until - now,
            self._next_api - now,
            self._next_message - now,
            self._chats.get(chat_key(chat_id), 0) - now,
        )

    async def _acquire(self, endpoint, data):
        sending, cost, chat, edit = self._request(endpoint, data)
        started = time.monotonic()
        async with self._condition:
            while True:
                now = time.monotonic()
                self._gc(now)
                deadline = max(self._cooldown_until, self._next_api)
                if sending:
                    deadline = max(
                        deadline, self._next_message, self._chats.get(chat, 0)
                    )
                if edit is not None:
                    deadline = max(deadline, self._edits.get(edit, 0))
                delay = deadline - now
                if delay <= 0 and self._inflight < self.max_inflight:
                    self._inflight += 1
                    self._next_api = now + self.api_interval
                    if sending:
                        self._next_message = now + cost * self.message_interval
                        if chat is not None:
                            group = not isinstance(chat, int) or chat < 0
                            self._chats[chat] = now + cost * (
                                self.group_interval if group else self.private_interval
                            )
                    if edit is not None:
                        self._edits[edit] = now + self.edit_interval
                    self.stats["requests"] += 1
                    self.stats["wait_ms"] += round((now - started) * 1000)
                    return
                try:
                    if delay > 0:
                        await asyncio.wait_for(self._condition.wait(), timeout=delay)
                    else:
                        await self._condition.wait()
                except asyncio.TimeoutError:
                    pass

    async def _release(self):
        async with self._condition:
            self._inflight -= 1
            self._condition.notify_all()

    async def process_request(
        self, callback, args, kwargs, endpoint, data, rate_limit_args
    ):
        for attempt in range(self.retries + 1):
            await self._acquire(endpoint, data)
            try:
                return await callback(*args, **kwargs)
            except RetryAfter as error:
                wait = retry_seconds(error.retry_after)
                async with self._condition:
                    self._cooldown_until = max(
                        self._cooldown_until,
                        time.monotonic() + wait + random.uniform(0, self.jitter),
                    )
                    self.stats["429"] += 1
                    self.stats["cooldown_ms"] += round(wait * 1000)
                    self._condition.notify_all()
                if attempt == self.retries or wait > self.max_wait:
                    self.stats["exhausted"] += 1
                    raise RateLimitExhausted(error.retry_after) from error
            finally:
                await self._release()

    def snapshot(self):
        return dict(
            self.stats,
            cooldown_seconds=round(max(0, self._cooldown_until - time.monotonic()), 2),
            inflight=self._inflight,
            chat_buckets=len(self._chats),
            edit_buckets=len(self._edits),
        )


def get_limiter(token):
    # Never store a token in metric keys or log labels.
    key = str(token).split(":", 1)[0]
    limiter = _limiters.get(key)
    if limiter is None:
        limiter = BotRateLimiter(key)
        _limiters[key] = limiter
    return limiter


def snapshots():
    return {key: limiter.snapshot() for key, limiter in list(_limiters.items())}
