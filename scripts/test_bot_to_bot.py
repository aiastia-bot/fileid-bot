#!/usr/bin/env python3
"""两个 Bot 互发文件的链路测试（Bot-to-Bot 备份方案验证）

验证目标（就是备份流程要依赖的那几条事实）：
  1. A 能否直接把文件私聊发给 B（不需要群/频道）
  2. B 能否从收到的消息里拿到「属于 B 自己的 file_id」
  3. B 用自己的 file_id 能否独立分发给别人
  4. A 的 file_id 在 B 侧是否确实不可用（决定「不能只传 ID 字符串」）

前置条件（缺一不可）：
  * A、B 两个 Bot 都在 @BotFather 里打开 Bot-to-Bot Communication Mode
    （@BotFather → /mybots → 选 Bot → Bot Settings → 找到 Bot-to-Bot Communication 开关，
      菜单名以你客户端显示的为准）
  * 两个 Bot 都没有配置 webhook（脚本会先 deleteWebhook 并丢弃 pending updates）
  * 没有任何别的地方在用这两个 token 轮询 getUpdates（否则会 409 conflict）

用法：
  export TEST_BOT_TOKEN_A='123:AAA'
  export TEST_BOT_TOKEN_B='456:BBB'
  python scripts/test_bot_to_bot.py
  python scripts/test_bot_to_bot.py --media document,photo,video
  python scripts/test_bot_to_bot.py --json report.json

离线自测（不需要真 token，验证脚本自身逻辑）：
  python scripts/test_bot_to_bot.py --selftest

退出码：0 全部通过；1 有 FAIL；2 参数/环境问题。
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import httpx

DEFAULT_API_BASE = "https://api.telegram.org"
CAPTION_PREFIX = "#b2b-probe"

# message 里可能出现媒体的字段（photo 特殊：是数组，取最大那张）
MEDIA_FIELDS = ("document", "video", "audio", "animation", "voice", "video_note", "sticker")
MIME_BY_KIND = {
    "document": "application/octet-stream",
    "photo": "image/png",
    "video": "video/mp4",
}

# ===== 报告 =====
MARK = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️ ", "SKIP": "⏭️ ", "INFO": "   "}


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append(Check(name, status, detail))
        line = f"{MARK.get(status, '   ')} {name}"
        if detail:
            line += f" — {detail}"
        print(line, flush=True)

    def check(self, name: str, ok: bool, detail: str = "", warn_only: bool = False) -> bool:
        self.add(name, "PASS" if ok else ("WARN" if warn_only else "FAIL"), detail)
        return ok

    def skip(self, name: str, detail: str = "") -> None:
        self.add(name, "SKIP", detail)

    def info(self, detail: str) -> None:
        print(f"{MARK['INFO']} {detail}", flush=True)

    @property
    def failed(self) -> List[Check]:
        return [c for c in self.checks if c.status == "FAIL"]

    def as_dict(self) -> dict:
        return {
            "ok": not self.failed,
            "checks": [{"name": c.name, "status": c.status, "detail": c.detail} for c in self.checks],
        }


# ===== 极简 Bot API 客户端 =====
class ApiError(Exception):
    def __init__(self, method: str, code: int, description: str):
        self.method = method
        self.code = code
        self.description = description
        super().__init__(f"{method} 失败 [{code}] {description}")

    @property
    def blocked_by_privacy(self) -> bool:
        """Bot-to-Bot 模式没开时的典型报错"""
        d = self.description.lower()
        return self.code == 403 or "can't initiate conversation" in d or "bot can't" in d


def _form(data: Dict[str, object]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, value in data.items():
        if value is None:
            continue
        if value is True:
            out[key] = "true"
        elif value is False:
            out[key] = "false"
        else:
            out[key] = str(value)
    return out


class BotApi:
    """只实现测试需要的几个方法，避免依赖 python-telegram-bot"""

    def __init__(self, token: str, api_base: str = DEFAULT_API_BASE, *, sleep: float = 1.0,
                 timeout: float = 30.0):
        self.token = token
        self.base = api_base.rstrip("/")
        self.sleep = sleep
        self.timeout = timeout
        self.calls = 0
        self._client = httpx.AsyncClient(timeout=timeout)

    async def close(self) -> None:
        await self._client.aclose()

    async def call(self, method: str, params: Optional[Dict[str, object]] = None,
                   files: Optional[Dict[str, Tuple[str, bytes, str]]] = None,
                   timeout: Optional[float] = None):
        url = f"{self.base}/bot{self.token}/{method}"
        if self.sleep:
            await asyncio.sleep(self.sleep)
        self.calls += 1
        try:
            response = await self._client.post(
                url,
                data=_form(params or {}),
                files=files,
                timeout=timeout or self.timeout,
            )
        except httpx.HTTPError as exc:
            raise ApiError(method, 0, f"网络错误: {exc}") from exc

        try:
            payload = response.json()
        except ValueError:
            raise ApiError(method, response.status_code, response.text[:300])

        if not payload.get("ok"):
            raise ApiError(
                method,
                int(payload.get("error_code") or response.status_code),
                str(payload.get("description") or ""),
            )
        return payload.get("result")

    async def get_me(self) -> dict:
        return await self.call("getMe")

    async def send_media(self, kind: str, chat_id: str, *, path: Optional[Path] = None,
                         file_id: Optional[str] = None, caption: Optional[str] = None,
                         extra: Optional[Dict[str, object]] = None) -> dict:
        """kind = document / photo / video"""
        params: Dict[str, object] = {"chat_id": chat_id}
        if caption is not None:
            params["caption"] = caption
        if extra:
            params.update(extra)

        files = None
        if path is not None:
            files = {kind: (path.name, path.read_bytes(), MIME_BY_KIND.get(kind, "application/octet-stream"))}
        else:
            params[kind] = file_id
        return await self.call(f"send{kind.capitalize()}", params, files)

    async def delete_message(self, chat_id: str, message_id: int) -> None:
        with contextlib.suppress(ApiError):
            await self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id})


# ===== 取消息里的媒体 =====
def extract_media(message: dict) -> Optional[Tuple[str, dict]]:
    # photo 是数组（多档尺寸），取最后一档即最大图；其余类型是单个对象
    photo = message.get("photo")
    if isinstance(photo, list) and photo:
        return "photo", photo[-1]
    if isinstance(photo, dict) and photo.get("file_id"):
        return "photo", photo
    for key in MEDIA_FIELDS:
        obj = message.get(key)
        if isinstance(obj, dict) and obj.get("file_id"):
            return key, obj
    return None


# ===== 长轮询 =====
class Poller:
    """给单个 Bot 跑 getUpdates，缓存消息供 wait_for 检索"""

    def __init__(self, api: BotApi, name: str, poll_timeout: int = 20):
        self.api = api
        self.name = name
        self.poll_timeout = poll_timeout
        self._messages: List[dict] = []
        self._claimed: set[int] = set()
        self._cond = asyncio.Condition()
        self._task: Optional[asyncio.Task] = None
        self.offset = 0
        self.errors: List[str] = []

    async def start(self) -> None:
        # 必须在 getUpdates 前清 webhook，否则 409
        await self.api.call("deleteWebhook", {"drop_pending_updates": True})
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            try:
                updates = await self.api.call(
                    "getUpdates",
                    {
                        "offset": self.offset,
                        "timeout": self.poll_timeout,
                        "allowed_updates": json.dumps(["message"]),
                    },
                    timeout=self.poll_timeout + 20,
                )
            except ApiError as exc:
                self.errors.append(f"{exc.code}: {exc.description}")
                await asyncio.sleep(2)
                continue
            except asyncio.CancelledError:
                raise
            for update in updates or []:
                self.offset = max(self.offset, int(update.get("update_id", 0)) + 1)
                message = update.get("message")
                if not message:
                    continue
                async with self._cond:
                    self._messages.append(message)
                    self._cond.notify_all()

    async def wait_for(self, predicate: Callable[[dict], bool], timeout: float) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        async with self._cond:
            while True:
                for index, message in enumerate(self._messages):
                    if index in self._claimed:
                        continue
                    if predicate(message):
                        self._claimed.add(index)
                        return message
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._cond.wait(), remaining)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task

    def dump(self) -> str:
        lines = []
        for message in self._messages:
            media = extract_media(message)
            sender = (message.get("from") or {}).get("username") or (message.get("from") or {}).get("id")
            lines.append(f"  · from={sender} kind={media[0] if media else '-'} "
                         f"caption={(message.get('caption') or '')[:40]}")
        return "\n".join(lines) or "  · (无)"


# ===== 测试素材 =====
def make_png(path: Path, size: int = 64) -> None:
    """手搓一张纯色 PNG，避免依赖 Pillow"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes((30, 120, 200)) * size for _ in range(size))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def make_mp4(png: Path, out: Path) -> Optional[str]:
    """用 ffmpeg 把 PNG 转 1 秒 mp4；失败返回原因"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return "未安装 ffmpeg"
    cmd = [
        ffmpeg, "-y", "-loop", "1", "-i", str(png), "-t", "1", "-r", "10",
        "-vf", "scale=128:128", "-pix_fmt", "yuv420p", "-loglevel", "error", str(out),
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0 or not out.exists():
        return f"ffmpeg 失败: {result.stderr.decode('utf-8', 'replace')[:120]}"
    return None


def build_fixtures(workdir: Path, media: List[str], report: Report) -> Dict[str, Path]:
    fixtures: Dict[str, Path] = {}
    png = workdir / "probe.png"
    make_png(png)

    if "photo" in media:
        fixtures["photo"] = png
    if "document" in media:
        txt = workdir / "probe.txt"
        txt.write_text("fileid-bot bot-to-bot probe\n" + "x" * 512, encoding="utf-8")
        fixtures["document"] = txt
    if "video" in media:
        mp4 = workdir / "probe.mp4"
        problem = make_mp4(png, mp4)
        if problem:
            report.skip("video 素材", problem)
        else:
            fixtures["video"] = mp4
    return fixtures


# ===== 测试主体 =====
@dataclass
class Config:
    a_token: str
    b_token: str
    api_base: str = DEFAULT_API_BASE
    media: List[str] = field(default_factory=lambda: ["document"])
    reverse: bool = True
    timeout: float = 45.0
    sleep: float = 1.0
    poll_timeout: int = 20


@dataclass
class Peer:
    label: str
    api: BotApi
    me: dict
    poller: Poller

    @property
    def chat_ref(self) -> str:
        username = self.me.get("username")
        return f"@{username}" if username else str(self.me["id"])

    async def cleanup(self, message: dict) -> None:
        peer_chat = (message.get("chat") or {}).get("id")
        message_id = message.get("message_id")
        if peer_chat and message_id:
            await self.api.delete_message(str(peer_chat), int(message_id))


def _caption_has(marker: str) -> Callable[[dict], bool]:
    def predicate(message: dict) -> bool:
        if marker not in (message.get("caption") or ""):
            return False
        return extract_media(message) is not None
    return predicate


async def run_text_probe(sender: Peer, receiver: Peer, cfg: Config, report: Report) -> bool:
    """纯文本探针：隔离「Bot-to-Bot 模式没开」和「文件方面的问题」"""
    label = f"{sender.label}→{receiver.label} 文本探针"
    marker = f"{CAPTION_PREFIX} text {int(time.time() * 1000)}"
    try:
        await sender.api.call("sendMessage", {
            "chat_id": receiver.chat_ref, "text": marker,
            "disable_notification": True,
        })
    except ApiError as exc:
        hint = ""
        if exc.blocked_by_privacy:
            hint = ("｜这就是 Bot-to-Bot 模式没开的典型报错：去 @BotFather 给两个 Bot 都打开 "
                    "Bot-to-Bot Communication Mode")
        report.check(label, False, f"{exc.code} {exc.description}{hint}")
        return False

    def predicate(message: dict) -> bool:
        return (message.get("text") or "").strip() == marker

    try:
        await receiver.poller.wait_for(predicate, cfg.timeout)
    except asyncio.TimeoutError:
        report.check(label, False,
                     f"发送成功但 {cfg.timeout:.0f}s 内没收到 update｜接收方可能还有 webhook/别的轮询进程在抢"
                     f"｜也可能接收方没开模式（部分情况下丢弃而非报错）")
        return False

    report.check(label, True, "文本链路通")
    return True


async def run_direction(sender: Peer, receiver: Peer, kind: str, path: Path,
                        cfg: Config, report: Report) -> None:
    label = f"{sender.label}→{receiver.label} {kind}"
    stamp = int(time.time() * 1000)
    upload_marker = f"{CAPTION_PREFIX} {sender.label}2{receiver.label} {kind} {stamp}"
    redeliver_marker = f"{CAPTION_PREFIX} redeliver {kind} {stamp}"
    cross_marker = f"{CAPTION_PREFIX} crossid {kind} {stamp}"

    # ① 上传并直发（真上传，不是 file_id）
    try:
        sent = await sender.api.send_media(kind, receiver.chat_ref, path=path, caption=upload_marker)
    except ApiError as exc:
        hint = ""
        if exc.blocked_by_privacy:
            hint = ("｜提示：检查 @BotFather 里两个 Bot 的 Bot-to-Bot Communication Mode 是否都已开启，"
                    "以及对方 @username 拼写是否正确")
        report.check(f"{label} 发送", False, f"{exc.code} {exc.description}{hint}")
        return

    if isinstance(sent, dict) and sent.get("message_id") is None:
        report.check(f"{label} 发送回执", False, f"返回里没有 message_id: {str(sent)[:160]}")
        return
    report.check(f"{label} 发送", True,
                 f"message_id={sent.get('message_id')} chat={(sent.get('chat') or {}).get('id')}")

    media = extract_media(sent)
    if not media:
        report.check(f"{label} 发送回执含媒体", False, "发送接口没返回媒体对象")
        return
    sender_file_id = media[1]["file_id"]
    sender_unique = media[1].get("file_unique_id", "")

    # ② 接收方拿到消息
    try:
        received = await receiver.poller.wait_for(_caption_has(upload_marker), cfg.timeout)
    except asyncio.TimeoutError:
        detail = (f"等待 {cfg.timeout:.0f}s 未收到 update｜接收方 getUpdates 已收到 {len(receiver.poller._messages)} 条消息"
                  f"｜可能原因：仅发送方开了 Bot-to-Bot 模式；或接收方有别的进程/服务在抢 getUpdates")
        report.check(f"{label} 送达", False, detail + "\n" + receiver.poller.dump())
        return

    from_id = (received.get("from") or {}).get("id")
    report.check(f"{label} 送达", from_id == sender.me.get("id"),
                 f"update 来自 from.id={from_id}")

    recv_media = extract_media(received)
    if not recv_media:
        report.check(f"{label} 接收含媒体", False, "收到的消息里没有媒体字段")
        return
    recv_file_id = recv_media[1]["file_id"]
    recv_unique = recv_media[1].get("file_unique_id", "")

    report.check(f"{label} 接收得 file_id", True,
                 f"{recv_file_id[:28]}… unique={recv_unique[:16]}…")
    report.check(f"{label} file_id 按 Bot 隔离", sender_file_id != recv_file_id,
                 "A/B 的 file_id 不同（符合预期）" if sender_file_id != recv_file_id
                 else "两个 Bot 拿到同一个 file_id，与官方文档描述不同，需复核",
                 warn_only=True)

    # ③ 接收方用自己的 file_id 独立分发
    try:
        resent = await receiver.api.send_media(kind, sender.chat_ref, file_id=recv_file_id,
                                               caption=redeliver_marker)
        resent_media = extract_media(resent) or (None, {})
        report.check(f"{label} 接收方独立分发", True,
                     f"用 B 自己的 file_id 再发成功，message_id={resent.get('message_id')}")
        if resent_media[1]:
            report.check(f"{label} 二次分发 file_id 稳定", True,
                         f"再发后仍是 {str(resent_media[1].get('file_id'))[:28]}…")
    except ApiError as exc:
        report.check(f"{label} 接收方独立分发", False, f"{exc.code} {exc.description}")

    # ④ 反向验证：发送方的 file_id 在接收方侧不可用
    try:
        await receiver.api.send_media(kind, sender.chat_ref, file_id=sender_file_id,
                                      caption=cross_marker)
        report.check(f"{label} 跨 Bot file_id 被拒绝", False,
                     "意外成功：对方 file_id 竟然可用，设计假设需要复核", warn_only=True)
    except ApiError as exc:
        text = exc.description.lower()
        looks_like_id_error = any(k in text for k in ("wrong file", "file_id", "file identifier", "reupload", "invalid"))
        report.check(f"{label} 跨 Bot file_id 被拒绝", looks_like_id_error,
                     f"{exc.code} {exc.description}",
                     warn_only=not looks_like_id_error)


def check_polling_health(peers: List["Peer"], report: Report) -> None:
    for peer in peers:
        detail = f"收到 {len(peer.poller._messages)} 条消息"
        conflict = any(err.startswith("409") for err in peer.poller.errors)
        if peer.poller.errors:
            detail += f"｜轮询错误 {len(peer.poller.errors)} 次，首个: {peer.poller.errors[0]}"
        if conflict:
            detail += "｜409 冲突：同一个 token 还有别的进程在 getUpdates，先停掉它再重跑"
        report.check(f"{peer.label} 轮询健康", not peer.poller.errors, detail,
                     warn_only=not conflict)


async def run_suite(cfg: Config) -> Report:
    report = Report()
    api_a = BotApi(cfg.a_token, cfg.api_base, sleep=cfg.sleep)
    api_b = BotApi(cfg.b_token, cfg.api_base, sleep=cfg.sleep)
    peers: List[Peer] = []

    try:
        # 0) 身份
        try:
            me_a = await api_a.get_me()
            report.check("getMe(A)", True, f"@{me_a.get('username')} id={me_a.get('id')}")
        except ApiError as exc:
            report.check("getMe(A)", False, f"{exc.code} {exc.description}")
            return report
        try:
            me_b = await api_b.get_me()
            report.check("getMe(B)", True, f"@{me_b.get('username')} id={me_b.get('id')}")
        except ApiError as exc:
            report.check("getMe(B)", False, f"{exc.code} {exc.description}")
            return report

        if not report.check("两个 Bot 不同", me_a.get("id") != me_b.get("id"),
                            f"id {me_a.get('id')} vs {me_b.get('id')}"):
            return report

        # 1) 起轮询
        peer_a = Peer("A", api_a, me_a, Poller(api_a, "A", cfg.poll_timeout))
        peer_b = Peer("B", api_b, me_b, Poller(api_b, "B", cfg.poll_timeout))
        peers = [peer_a, peer_b]
        for peer in peers:
            await peer.poller.start()
        report.info(f"已清 webhook 并启动长轮询（A=@{me_a.get('username')} B=@{me_b.get('username')}）")

        # 2) 素材
        workdir = Path(tempfile.mkdtemp(prefix="b2b-probe-"))
        fixtures = build_fixtures(workdir, cfg.media, report)
        report.info(f"测试素材：{', '.join(f'{k}={v.name}' for k, v in fixtures.items()) or '(空)'}")

        # 3) 文本探针先行：模式没开的话，后面文件测试没有意义
        text_ok = await run_text_probe(peer_a, peer_b, cfg, report)
        if cfg.reverse:
            text_ok = await run_text_probe(peer_b, peer_a, cfg, report) and text_ok
        if not text_ok:
            # 收不到消息时，轮询状态往往才是根因，先把它报出来
            check_polling_health(peers, report)
            report.info("文本探针失败，跳过文件测试（先把 Bot-to-Bot 模式/轮询冲突解决）")
            return report

        # 4) 逐类型跑
        for kind, path in fixtures.items():
            await run_direction(peer_a, peer_b, kind, path, cfg, report)
            if cfg.reverse:
                await run_direction(peer_b, peer_a, kind, path, cfg, report)

        # 5) 轮询健康度
        check_polling_health(peers, report)

        # 5) 清理探针消息
        for peer in peers:
            for message in peer.poller._messages:
                await peer.cleanup(message)
        report.info("已尽力清理探针消息（deleteMessage，失败可忽略）")

        shutil.rmtree(workdir, ignore_errors=True)
        return report

    finally:
        for peer in peers:
            await peer.poller.stop()
        await api_a.close()
        await api_b.close()


# ===== 离线自测：本地 mock Bot API =====
def _sha_key(data: bytes) -> str:
    return hashlib.sha256(data[:256]).hexdigest()[:12]


def _parse_multipart(body: bytes, boundary: bytes) -> Tuple[Dict[str, str], Dict[str, bytes]]:
    fields: Dict[str, str] = {}
    files: Dict[str, bytes] = {}
    for chunk in body.split(b"--" + boundary):
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        if chunk.endswith(b"\r\n"):
            chunk = chunk[:-2]
        if not chunk or chunk.startswith(b"--"):
            continue
        head, _, content = chunk.partition(b"\r\n\r\n")
        headers = head.decode("utf-8", "replace")
        match = re.search(r'name="([^"]+)"', headers)
        if not match:
            continue
        name = match.group(1)
        if re.search(r'filename="', headers):
            files[name] = content
        else:
            fields[name] = content.decode("utf-8", "replace")
    return fields, files


class MockTelegram:
    """只在 --selftest 里用：模拟两个 Bot 的 getUpdates / sendDocument 等"""

    def __init__(self):
        self.bots: Dict[int, str] = {111: "test_a_bot", 222: "test_b_bot"}
        self.registry: Dict[str, Tuple[int, str, str]] = {}
        self.queues: Dict[int, List[dict]] = {bot_id: [] for bot_id in self.bots}
        self.cv = threading.Condition()
        self.update_id = 1000
        self.message_id = 1

    # --- HTTP 层 ---
    def start(self) -> ThreadingHTTPServer:
        state = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                return

            def do_POST(self):  # noqa: N802
                parts = urlparse(self.path).path.strip("/").split("/")
                token = parts[0][3:] if parts and parts[0].startswith("bot") else ""
                method = parts[1] if len(parts) > 1 else ""
                try:
                    bot_id = int(token.split(":")[0])
                except ValueError:
                    bot_id = 0
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                content_type = self.headers.get("content-type") or ""
                if content_type.startswith("multipart/form-data") and "boundary=" in content_type:
                    boundary = content_type.split("boundary=", 1)[1].strip('"').encode()
                    fields, files = _parse_multipart(body, boundary)
                else:
                    fields = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
                    files = {}
                ok, payload = state.handle(bot_id, method, fields, files)
                data = json.dumps(payload).encode()
                self.send_response(200 if ok else 400)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        handler_class = type("MockHandler", (Handler,), {})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_class)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server

    def base_url(self, server: ThreadingHTTPServer) -> str:
        return f"http://127.0.0.1:{server.server_address[1]}"

    # --- 业务层 ---
    def handle(self, bot_id: int, method: str, fields: Dict[str, str],
               files: Dict[str, bytes]) -> Tuple[bool, dict]:
        if bot_id not in self.bots:
            return False, {"ok": False, "error_code": 401, "description": "Unauthorized"}
        if method == "getMe":
            return True, {"ok": True, "result": {
                "id": bot_id, "is_bot": True, "first_name": f"Test {bot_id}",
                "username": self.bots[bot_id], "can_join_groups": False,
            }}
        if method == "deleteWebhook":
            return True, {"ok": True, "result": True}
        if method == "deleteMessage":
            return True, {"ok": True, "result": True}
        if method == "getUpdates":
            return True, {"ok": True, "result": self._take_updates(bot_id, float(fields.get("timeout") or 0))}
        if method in ("sendDocument", "sendPhoto", "sendVideo"):
            kind = {"sendDocument": "document", "sendPhoto": "photo", "sendVideo": "video"}[method]
            return self._send(bot_id, kind, fields, files)
        if method == "sendMessage":
            return self._send(bot_id, "text", fields, files)
        return False, {"ok": False, "error_code": 404, "description": f"Not Found: {method}"}

    def _take_updates(self, bot_id: int, timeout: float) -> List[dict]:
        deadline = time.time() + min(timeout, 5)
        with self.cv:
            while not self.queues[bot_id]:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                self.cv.wait(remaining)
            updates = self.queues[bot_id][:]
            self.queues[bot_id] = []
        return updates

    def _resolve(self, chat_ref: str) -> Optional[int]:
        if chat_ref.startswith("@"):
            for bot_id, username in self.bots.items():
                if username.lower() == chat_ref[1:].lower():
                    return bot_id
            return None
        try:
            target = int(chat_ref)
        except ValueError:
            return None
        return target if target in self.bots else None

    def _send(self, bot_id: int, kind: str, fields: Dict[str, str],
              files: Dict[str, bytes]) -> Tuple[bool, dict]:
        target = self._resolve(fields.get("chat_id", ""))
        if target is None:
            return False, {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
        if target == bot_id:
            return False, {"ok": False, "error_code": 400,
                           "description": "Bad Request: bot can't send messages to itself"}

        if kind == "text":
            if fields.get("text"):
                sender_info = {"id": bot_id, "is_bot": True, "username": self.bots[bot_id]}
                with self.cv:
                    self.message_id += 1
                    message_id = self.message_id
                    self.update_id += 1
                    self.queues[target].append({
                        "update_id": self.update_id,
                        "message": {
                            "message_id": message_id, "date": int(time.time()),
                            "chat": {"id": bot_id, "type": "private"},
                            "from": sender_info, "text": fields["text"],
                        },
                    })
                    self.cv.notify_all()
                return True, {"ok": True, "result": {
                    "message_id": message_id, "date": int(time.time()),
                    "chat": {"id": target, "type": "private"},
                    "from": sender_info, "text": fields["text"],
                }}
            return False, {"ok": False, "error_code": 400,
                           "description": "Bad Request: message text is empty"}

        if kind in files:
            key = _sha_key(files[kind])
        elif fields.get(kind):
            entry = self.registry.get(fields[kind])
            if not entry:
                return False, {"ok": False, "error_code": 400,
                               "description": "Bad Request: wrong file identifier/HTTP URL specified"}
            if entry[0] != bot_id:
                # 关键行为：file_id 只对签发它的 Bot 有效
                return False, {"ok": False, "error_code": 400,
                               "description": "Bad Request: wrong file identifier/HTTP URL specified"}
            key = entry[2]
        else:
            return False, {"ok": False, "error_code": 400, "description": "Bad Request: there is no file to send"}

        def file_ids(owner: int) -> List[str]:
            base = f"mock-{owner}-{kind}-{key}"
            # photo 每档尺寸都是独立 file_id，都要登记
            return [base + suffix for suffix in ("-l", "-m", "-s")] if kind == "photo" else [base]

        def media_obj(ids: List[str]) -> object:
            base = {"file_unique_id": f"uniq-{key}", "file_name": f"probe.{kind}", "file_size": 1024}
            if kind == "photo":
                # Telegram 的 photo 字段是尺寸数组，按从小到大排列，[-1] 是最大图
                sizes = ((32, ids[2]), (64, ids[1]), (128, ids[0]))
                return [{**base, "file_id": fid, "width": w, "height": w} for w, fid in sizes]
            return {**base, "file_id": ids[0]}

        sender_ids = file_ids(bot_id)
        receiver_ids = file_ids(target)
        for fid in sender_ids:
            self.registry[fid] = (bot_id, kind, key)
        for fid in receiver_ids:
            self.registry[fid] = (target, kind, key)

        with self.cv:
            self.message_id += 1
            message_id = self.message_id
            caption = fields.get("caption", "")
            sender_info = {"id": bot_id, "is_bot": True, "username": self.bots[bot_id]}
            self.update_id += 1
            self.queues[target].append({
                "update_id": self.update_id,
                "message": {
                    "message_id": message_id, "date": int(time.time()),
                    "chat": {"id": bot_id, "type": "private"},
                    "from": sender_info,
                    kind: media_obj(receiver_ids),
                    "caption": caption,
                },
            })
            self.cv.notify_all()

        return True, {"ok": True, "result": {
            "message_id": message_id, "date": int(time.time()),
            "chat": {"id": target, "type": "private"},
            "from": sender_info,
            kind: media_obj(sender_ids),
            "caption": caption,
        }}


async def run_selftest(args) -> int:
    print("=== 离线自测（本地 mock Bot API，不接触 Telegram）===\n", flush=True)
    mock = MockTelegram()
    server = mock.start()
    try:
        cfg = Config(
            a_token="111:mock",
            b_token="222:mock",
            api_base=mock.base_url(server),
            media=args.media,
            reverse=not args.no_reverse,
            timeout=10.0,
            sleep=0.0,
            poll_timeout=1,
        )
        report = await run_suite(cfg)
        print()
        if args.json_path:
            Path(args.json_path).write_text(
                json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"结果已写入 {args.json_path}")
        if report.failed:
            print(f"自测结果：{len(report.failed)} 项失败 —— 脚本逻辑有问题")
            return 1
        print("自测结果：全部通过，脚本可以拿去跑真实 Bot")
        return 0
    finally:
        server.shutdown()


# ===== CLI =====
def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="两个 Bot 之间 Bot-to-Bot 文件互发链路测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--a-token", default=os.environ.get("TEST_BOT_TOKEN_A", ""),
                        help="A 的 token（或环境变量 TEST_BOT_TOKEN_A）")
    parser.add_argument("--b-token", default=os.environ.get("TEST_BOT_TOKEN_B", ""),
                        help="B 的 token（或环境变量 TEST_BOT_TOKEN_B）")
    parser.add_argument("--media", default="document",
                        help="逗号分隔的媒体类型：document,photo,video（默认 document）")
    parser.add_argument("--no-reverse", action="store_true", help="只测 A→B，不测 B→A")
    parser.add_argument("--timeout", type=float, default=45.0, help="单步等待 update 的秒数（默认 45）")
    parser.add_argument("--sleep", type=float, default=1.0, help="每次 API 调用前休眠（默认 1.0，防限流）")
    parser.add_argument("--api-base", default=os.environ.get("TG_API_BASE", DEFAULT_API_BASE),
                        help="Bot API 根地址（默认官方；自建 Bot API Server 可改）")
    parser.add_argument("--json", dest="json_path", default="", help="把结果写成 JSON")
    parser.add_argument("--selftest", action="store_true", help="本地 mock 自测，不需要真 token")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    media = [m.strip().lower() for m in args.media.split(",") if m.strip()]
    invalid = [m for m in media if m not in MIME_BY_KIND]
    if invalid:
        print(f"未知媒体类型: {invalid}（可选 document,photo,video）")
        return 2
    args.media = media

    if args.selftest:
        return asyncio.run(run_selftest(args))

    if not args.a_token or not args.b_token:
        print("缺少 token：用 --a-token/--b-token 或环境变量 TEST_BOT_TOKEN_A / TEST_BOT_TOKEN_B 传入。\n"
              "（想先验证脚本本身：python scripts/test_bot_to_bot.py --selftest）")
        return 2

    print("=== Bot-to-Bot 文件互发测试 ===")
    print("前置检查（脚本查不到，需要你自己确认）：")
    print("  1. A、B 已在 @BotFather 打开 Bot-to-Bot Communication Mode")
    print("  2. 两个 token 都没有别处正在 getUpdates 轮询")
    print("  3. 两个 Bot 都没有设置 webhook（脚本会自动 deleteWebhook）\n")

    cfg = Config(
        a_token=args.a_token,
        b_token=args.b_token,
        api_base=args.api_base,
        media=args.media,
        reverse=not args.no_reverse,
        timeout=args.timeout,
        sleep=args.sleep,
    )
    report = asyncio.run(run_suite(cfg))

    print()
    passes = sum(1 for c in report.checks if c.status == "PASS")
    warns = sum(1 for c in report.checks if c.status == "WARN")
    skips = sum(1 for c in report.checks if c.status == "SKIP")
    print(f"合计：PASS {passes} / WARN {warns} / SKIP {skips} / FAIL {len(report.failed)}")

    if report.failed:
        print("\n失败项：")
        for check in report.failed:
            print(f"  ❌ {check.name} — {check.detail}")
        print("\n常见原因：Bot-to-Bot 模式只在一边开启；@username 写错；接收方还有 webhook 或别的轮询进程。")
    else:
        print("\n链路可用：可以按 A→B/C 单向同步 + 接收方落库确认 的方式接自动备份了。")

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps(report.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"结果已写入 {args.json_path}")

    return 0 if not report.failed else 1


if __name__ == "__main__":
    sys.exit(main())
