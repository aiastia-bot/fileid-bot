#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""两个 Bot 之间互发文件的真实链路测试（直接打 Telegram Bot API）

【用法】
  1. 把下面 TOKEN_A / TOKEN_B 填成两个【测试 Bot】的 token
  2. 两个 Bot 都要在 @BotFather 里打开 Bot-to-Bot Communication Mode
  3. 运行：python3 scripts/b2b_real_test.py

【它会验证 4 件事】
  ① A 能否直接用 @username 把文件私聊发给 B（不需要群/频道）
  ② B 能否从收到的消息里拿到属于【自己】的 file_id
  ③ B 用自己那个 file_id 能否再发一次（= 备份后能独立分发）
  ④ A 的 file_id 在 B 侧是否确实不可用（= 不能只把 ID 字符串传给对方）

【注意】
  * 脚本会 deleteWebhook 清掉这两个 Bot 的 webhook，只对测试 Bot 用
  * 同一个 token 不能有别的进程在 getUpdates，否则 409 冲突
"""
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import zlib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# ==================== 填这里 ====================
TOKEN_A = ""            # A 的 token
TOKEN_B = ""            # B 的 token

MEDIA = ["document", "photo", "video"]   # 要测的类型；缺 ffmpeg 时 video 自动跳过
WAIT_SECONDS = 45       # 等对方收到消息的超时
ALLOW_DELETE_WEBHOOK = False   # 测试 Bot 上已有 webhook 时，是否允许脚本删掉它
# ===============================================

# 自动化时可改用环境变量，不影响上面直接填
TOKEN_A = TOKEN_A or os.environ.get("B2B_TOKEN_A", "")
TOKEN_B = TOKEN_B or os.environ.get("B2B_TOKEN_B", "")
API = os.environ.get("TG_API_BASE", "https://api.telegram.org").rstrip("/")

MARKER = "#b2bprobe"     # 每轮测试的 caption 前缀，用来认出自己的消息
MEDIA_FIELDS = ("document", "video", "audio", "animation", "voice",
                "video_note", "sticker")
MIME = {
    "document": "application/octet-stream",
    "photo": "image/png",
    "video": "video/mp4",
}

OK, BAD, WARN, SKIP = "✅", "❌", "⚠️ ", "⏭️ "


# ==================== Bot API 调用（标准库实现） ====================
class ApiError(Exception):
    def __init__(self, method, code, description):
        self.method = method
        self.code = code
        self.description = description
        super().__init__(f"{method} 失败 [{code}] {description}")

    @property
    def looks_like_mode_off(self):
        d = self.description.lower()
        # USER_BOT_TO_BOT_DISABLED 是接收方未开启模式的官方错误码
        return ("bot_to_bot" in d or self.code == 403
                or "initiate conversation" in d or "can't send" in d)


def _flatten(params):
    out = {}
    for key, value in (params or {}).items():
        if value is None:
            continue
        out[key] = ("true" if value else "false") if isinstance(value, bool) else str(value)
    return out


def _multipart(fields, files):
    """files: {字段名: (文件名, 二进制内容, MIME)}"""
    boundary = "----b2b" + uuid.uuid4().hex
    body = bytearray()
    for key, value in fields.items():
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"'
                 f"\r\n\r\n{value}\r\n").encode()
    for key, (filename, content, mime) in files.items():
        body += (f'--{boundary}\r\nContent-Disposition: form-data; name="{key}";'
                 f' filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n').encode()
        body += content + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def call(token, method, params=None, files=None, timeout=60):
    """调一个 Bot API 方法，成功返回 result，失败抛 ApiError"""
    fields = _flatten(params)
    if files:
        body, ctype = _multipart(fields, files)
    else:
        body, ctype = urlencode(fields).encode(), "application/x-www-form-urlencoded"

    request = Request(f"{API}/bot{token}/{method}", data=body,
                      headers={"Content-Type": ctype})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode())
    except HTTPError as exc:                      # Telegram 用 4xx 返回错误 JSON
        try:
            payload = json.loads(exc.read().decode())
        except Exception:
            raise ApiError(method, exc.code, str(exc)) from exc
    except URLError as exc:
        raise ApiError(method, 0, f"网络错误: {exc.reason}") from exc

    if not payload.get("ok"):
        raise ApiError(method, int(payload.get("error_code") or 0),
                       str(payload.get("description") or ""))
    return payload.get("result")


# ==================== 工具 ====================
def media_of(message):
    """从 message 里取 (类型, 媒体对象)；photo 是尺寸数组，取最后（最大）一张"""
    photo = message.get("photo")
    if isinstance(photo, list) and photo:
        return "photo", photo[-1]
    for key in MEDIA_FIELDS:
        obj = message.get(key)
        if isinstance(obj, dict) and obj.get("file_id"):
            return key, obj
    return None


def send_media(peer, chat_id, kind, path=None, file_id=None, caption=None):
    params = {"chat_id": chat_id}
    if caption:
        params["caption"] = caption
    files = None
    if path is not None:
        files = {kind: (path.name, path.read_bytes(), MIME[kind])}
    else:
        params[kind] = file_id
    return call(peer.token, "send" + kind.capitalize(), params, files,
                timeout=180)


class Peer:
    def __init__(self, label, token):
        self.label = label
        self.token = token
        self.me = {}
        self.offset = 0
        self.messages = []
        self.claimed = set()

    @property
    def at(self):
        return "@" + self.me["username"]

    def drain(self, poll_timeout=0):
        """取一批 update；poll_timeout>0 时用长轮询等消息"""
        result = call(self.token, "getUpdates",
                      {"offset": self.offset, "timeout": int(poll_timeout),
                       "allowed_updates": json.dumps(["message"])},
                      timeout=poll_timeout + 20)
        for update in result or []:
            self.offset = max(self.offset, int(update.get("update_id", 0)) + 1)
            message = update.get("message")
            if message:
                self.messages.append(message)
        return self.messages

    def wait_for(self, predicate, timeout):
        self.drain(0)
        deadline = time.time() + timeout
        while True:
            for message in self.messages:
                key = message.get("message_id")
                if key in self.claimed:
                    continue
                if predicate(message):
                    self.claimed.add(key)
                    return message
            remain = deadline - time.time()
            if remain <= 0:
                raise TimeoutError
            self.drain(min(25, max(1, int(remain))))

    def cleanup(self, sender_id):
        """删掉自己这边收到的探针消息（失败无所谓）"""
        for message in self.messages:
            try:
                call(self.token, "deleteMessage",
                     {"chat_id": sender_id, "message_id": message.get("message_id")})
            except ApiError:
                pass


def make_png(path, size=96):
    """手搓一张纯色 PNG，不依赖 Pillow"""

    def chunk(tag, data):
        payload = tag + data
        return struct.pack(">I", len(data)) + payload + struct.pack(
            ">I", zlib.crc32(payload) & 0xFFFFFFFF)

    rows = b"".join(b"\x00" + bytes((36, 118, 200)) * size for _ in range(size))
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    path.write_bytes(b"\x89PNG\r\n\x1a\n"
                     + chunk(b"IHDR", header)
                     + chunk(b"IDAT", zlib.compress(rows))
                     + chunk(b"IEND", b""))


def make_mp4(png, out):
    """用 ffmpeg 生成 1 秒视频；没有 ffmpeg 返回原因"""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return "未安装 ffmpeg"
    result = subprocess.run(
        [ffmpeg, "-y", "-loop", "1", "-i", str(png), "-t", "1", "-r", "10",
         "-vf", "scale=160:160", "-pix_fmt", "yuv420p", "-loglevel", "error", str(out)],
        capture_output=True)
    if result.returncode != 0 or not out.exists():
        return "ffmpeg 失败: " + result.stderr.decode("utf-8", "replace")[:100]
    return None


# ==================== 单项测试 ====================
def test_direction(sender, receiver, kind, path, results):
    """sender 用真上传把文件直发 receiver，验证整条链"""
    tag = f"{sender.label}2{receiver.label}"
    stamp = int(time.time() * 1000)
    upload_mark = f"{MARKER} up {tag} {kind} {stamp}"
    resend_mark = f"{MARKER} again {tag} {kind} {stamp}"
    cross_mark = f"{MARKER} cross {tag} {kind} {stamp}"

    def record(name, ok, detail=""):
        results.append((name, OK if ok else BAD, detail))
        print(f"  {OK if ok else BAD} {name}" + (f" — {detail}" if detail else ""), flush=True)

    print(f"\n[{tag} {kind}]", flush=True)

    # ① 发送方真上传文件，直接私聊给对方
    try:
        sent = send_media(sender, receiver.at, kind, path=path, caption=upload_mark)
    except ApiError as exc:
        hint = "｜在 @BotFather 里给两个 Bot 都打开 Bot-to-Bot Communication Mode 再重跑" \
            if exc.looks_like_mode_off else ""
        record(f"{tag} 发送", False, f"{exc.code} {exc.description}{hint}")
        return
    record(f"{tag} 发送", True, f"message_id={sent.get('message_id')} → {receiver.at}")

    sent_media = media_of(sent)
    if not sent_media:
        record(f"{tag} 发送回执含媒体", False, "send 接口没返回媒体对象")
        return
    a_file_id = sent_media[1]["file_id"]
    a_unique = sent_media[1].get("file_unique_id", "")

    # ② 接收方收到消息，拿到自己的 file_id
    try:
        received = receiver.wait_for(
            lambda m: upload_mark == (m.get("caption") or "").strip() and media_of(m),
            WAIT_SECONDS)
    except TimeoutError:
        record(f"{tag} 送达", False,
               f"{WAIT_SECONDS}s 内没收到消息｜可能只有一边开了 Bot-to-Bot 模式，"
               f"或 {receiver.at} 还有别的进程在抢 getUpdates")
        return

    from_id = (received.get("from") or {}).get("id")
    record(f"{tag} 送达", from_id == sender.me["id"], f"from.id={from_id}")

    b_file_id = media_of(received)[1]["file_id"]
    b_unique = media_of(received)[1].get("file_unique_id", "")
    same_file = bool(a_unique) and a_unique == b_unique
    record(f"{tag} 接收方拿到自己的 file_id", b_file_id != a_file_id,
           f"{b_file_id[:30]}…（与 A 的不同 = 预期）"
           + ("，file_unique_id 与 A 一致（同一文件，两条 ID 各自有效）" if same_file else ""))

    # ③ 接收方用自己的 file_id 独立分发（备份激活时要靠这一步）
    try:
        again = send_media(receiver, sender.at, kind, file_id=b_file_id,
                           caption=resend_mark)
        record(f"{tag} 接收方独立分发", True, f"用 B 的 file_id 再发成功 #{again.get('message_id')}")
    except ApiError as exc:
        record(f"{tag} 接收方独立分发", False, f"{exc.code} {exc.description}")

    # ④ 反向验证：A 的 file_id 在 B 侧是否可用
    try:
        send_media(receiver, sender.at, kind, file_id=a_file_id, caption=cross_mark)
        # 实测发现：bot-to-bot 场景 Telegram 允许接收方复用发送方的 file_id
        # （大概率因为接收方持有该文件的聊天记录）。属未文档化行为，不算失败，
        # 但备份设计仍应存接收方自己的 file_id。
        results.append((f"{tag} 跨 Bot file_id 被拒绝", WARN,
                        "意外成功：接收方能复用发送方的 file_id（未文档化，勿依赖）"))
        print(f"  {WARN} {tag} 跨 Bot file_id 被拒绝 — 意外成功：对方 file_id 也能发"
              "（未文档化行为，备份仍应存接收方自己的 file_id）", flush=True)
    except ApiError as exc:
        record(f"{tag} 跨 Bot file_id 被拒绝", True, f"{exc.code} {exc.description}")


def test_text(sender, receiver, results):
    """纯文本探针：把「模式没开」和「文件本身的问题」分开"""
    mark = f"{MARKER} text {sender.label}2{receiver.label} {int(time.time() * 1000)}"
    name = f"{sender.label}→{receiver.label} 文本探针"
    try:
        call(sender.token, "sendMessage",
             {"chat_id": receiver.at, "text": mark, "disable_notification": True})
    except ApiError as exc:
        hint = "｜这就是 Bot-to-Bot 模式没开的典型报错" if exc.looks_like_mode_off else ""
        results.append((name, BAD, f"{exc.code} {exc.description}"))
        print(f"  {BAD} {name} — {exc.code} {exc.description}{hint}", flush=True)
        return False

    try:
        receiver.wait_for(lambda m: (m.get("text") or "").strip() == mark, WAIT_SECONDS)
    except TimeoutError:
        results.append((name, BAD, "发送成功但没收到"))
        print(f"  {BAD} {name} — 发送成功但 {WAIT_SECONDS}s 内没收到，"
              f"检查接收方 webhook/别处轮询", flush=True)
        return False

    results.append((name, OK, "文本链路通"))
    print(f"  {OK} {name} — 文本链路通", flush=True)
    return True


# ==================== 主流程 ====================
def main():
    if not TOKEN_A or not TOKEN_B:
        print("请先在脚本开头填写 TOKEN_A / TOKEN_B（或设置环境变量 B2B_TOKEN_A / B2B_TOKEN_B）")
        return 2

    print("=" * 62)
    print("两个 Bot 互发文件 · 真实链路测试")
    print("=" * 62)

    peer_a, peer_b = Peer("A", TOKEN_A), Peer("B", TOKEN_B)
    results = []

    # 身份确认
    try:
        peer_a.me = call(TOKEN_A, "getMe")
        peer_b.me = call(TOKEN_B, "getMe")
    except ApiError as exc:
        print(f"{BAD} Token 无效或网络不通：{exc.code} {exc.description}")
        return 1
    print(f"A = @{peer_a.me['username']} (id {peer_a.me['id']})")
    print(f"B = @{peer_b.me['username']} (id {peer_b.me['id']})")
    if peer_a.me["id"] == peer_b.me["id"]:
        print(f"{BAD} 两个 token 指向同一个 Bot，改用两个不同的 Bot 测试")
        return 2

    # webhook 检查：删 webhook 会改变 Bot 接收消息的方式
    for peer in (peer_a, peer_b):
        try:
            url = (call(peer.token, "getWebhookInfo") or {}).get("url") or ""
        except ApiError:
            url = ""
        if url and not ALLOW_DELETE_WEBHOOK:
            print(f"\n{BAD} @{peer.me['username']} 已经设置了 webhook：{url}")
            print("   脚本需要 deleteWebhook 才能用 getUpdates 收消息。")
            print("   如果确认这是测试 Bot，把脚本开头 ALLOW_DELETE_WEBHOOK 改成 True 再跑。")
            return 2

    for peer in (peer_a, peer_b):
        call(peer.token, "deleteWebhook", {"drop_pending_updates": True})
    print("\n已清 webhook，开始测试\n")

    # 素材
    workdir = Path(tempfile.mkdtemp(prefix="b2b-"))
    png = workdir / "probe.png"
    make_png(png)
    fixtures = {}
    if "document" in MEDIA:
        txt = workdir / "probe.txt"
        txt.write_text("fileid-bot bot-to-bot probe\n" + "x" * 800, encoding="utf-8")
        fixtures["document"] = txt
    if "photo" in MEDIA:
        fixtures["photo"] = png
    if "video" in MEDIA:
        mp4 = workdir / "probe.mp4"
        problem = make_mp4(png, mp4)
        if problem:
            results.append(("video 素材", SKIP, problem))
            print(f"  {SKIP} video 跳过 — {problem}")
        else:
            fixtures["video"] = mp4
    print(f"素材：{', '.join(f'{k}={v.name}' for k, v in fixtures.items())}\n")

    # 文本探针
    print("[文本探针]")
    text_ok = test_text(peer_a, peer_b, results)
    text_ok = test_text(peer_b, peer_a, results) and text_ok
    if not text_ok:
        print(f"\n文本都不通，后面的文件测试没有意义，先解决模式/轮询问题。")
        _summary(results)
        return 1

    # 真·文件互发
    for kind, path in fixtures.items():
        test_direction(peer_a, peer_b, kind, path, results)
        test_direction(peer_b, peer_a, kind, path, results)

    # 清理
    peer_a.cleanup(peer_b.me["id"])
    peer_b.cleanup(peer_a.me["id"])
    shutil.rmtree(workdir, ignore_errors=True)
    print("\n已清理探针消息")

    return _summary(results)


def _summary(results):
    print("\n" + "=" * 62)
    bad = [r for r in results if r[1] == BAD]
    warn = [r for r in results if r[1] == WARN]
    ok = [r for r in results if r[1] == OK]
    skipped = [r for r in results if r[1] == SKIP]
    print(f"合计：通过 {len(ok)} / 警告 {len(warn)} / 失败 {len(bad)} / 跳过 {len(skipped)}")
    if bad:
        print("\n失败项：")
        for name, _, detail in bad:
            print(f"  {BAD} {name} — {detail}")
        print("\n常见原因：")
        print("  · 只有一边在 BotFather 打开了 Bot-to-Bot Communication Mode（要两边都开）")
        print("  · 接收方还有 webhook 或别的进程在 getUpdates（409）")
        print("  · 发送用的 @username 拼错，或对方 Bot 已被删除")
        return 1
    if warn:
        print("\n警告项（不影响链路可用）：")
        for name, _, detail in warn:
            print(f"  {WARN} {name} — {detail}")
    print("\n链路通了。可以按「A 单向发给 B/C → 接收方落库确认」接自动备份。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
