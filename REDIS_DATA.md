# Redis 数据结构说明

> FileID Bot 项目中 Redis 存储的所有键类型、数据格式和用途说明

---

## 概览

| 键前缀 | Redis 类型 | 过期时间 | 功能模块 |
|--------|-----------|---------|---------|
| `col:` | String (JSON) | 5 分钟 | 集合元信息缓存 |
| `col_files:` | String (JSON) | 5 分钟 | 集合文件列表缓存 |
| `stats:sent:` | String (整数) | 2 天 | 每日发送统计计数器 |
| `sq:` | List | 无（手动管理） | 发送任务队列持久化 |
| `rate:user:` | Sorted Set | 限流窗口+1秒 | 用户请求限流 |

---

## 1. `col:{code}` — 集合元信息缓存

### 键格式

```
col:{bot_username}_col:{raw_code}
```

**示例**: `col:******_col:******************************`

### 数据结构

- **Redis 类型**: String
- **存储方式**: JSON 字符串（通过 `cache_set_json` / `cache_get_json`）
- **TTL**: 300 秒（5 分钟自动过期）
- **底层命令**: `SETEX`

### 内容格式

```json
{
  "id": *,
  "code": "******_col:******************************",
  "bot_username": "******",
  "name": "我的集合",
  "user_id": *********,
  "file_count": 5,
  "status": "open",
  "created_at": "2026-05-13 12:00:00",
  "updated_at": "2026-05-13 12:05:00",
  "bot_db_id": *
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 数据库自增 ID |
| `code` | string | 集合唯一代码 |
| `bot_username` | string | 所属 Bot 用户名 |
| `name` | string | 集合名称 |
| `user_id` | int | 创建者 Telegram 用户 ID |
| `file_count` | int | 集合内文件数量 |
| `status` | string | 集合状态：`open`（创建中）/ `completed`（已完成） |
| `created_at` | string | 创建时间 |
| `updated_at` | string | 更新时间 |
| `bot_db_id` | int | Bot 数据库记录 ID |

### 用途

缓存集合的基本信息，当用户发送集合代码时，避免每次查询数据库。被 `get_collection(code)` 函数使用。

### 相关代码

- **写入**: `db_collections.py` — `get_collection()` 中先查缓存
- **读取**: `db_collections.py` — `get_collection()` 返回缓存数据
- **删除**: `db_collections.py` — 集合变更时清除缓存

---

## 2. `col_files:{code}` — 集合文件列表缓存

### 键格式

```
col_files:{bot_username}_col:{raw_code}
```

**示例**: `col_files:******_col:******************************`

### 数据结构

- **Redis 类型**: String
- **存储方式**: JSON 数组字符串
- **TTL**: 300 秒（5 分钟自动过期）
- **底层命令**: `SETEX`

### 内容格式

```json
[
  {
    "id": **,
    "code": "******_p:******",
    "bot_username": "******",
    "file_type": "photo",
    "telegram_file_id": "********************************",
    "file_size": ******,
    "file_unique_id": "********",
    "user_id": *********,
    "created_at": "2026-05-13 12:00:00"
  },
  {
    "id": **,
    "code": "******_v:******",
    "bot_username": "******",
    "file_type": "video",
    "telegram_file_id": "********************************",
    "file_size": *******,
    "file_unique_id": "********",
    "user_id": *********,
    "created_at": "2026-05-13 12:01:00"
  }
]
```

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | int | 文件记录数据库 ID |
| `code` | string | 文件唯一代码（格式：`{bot}_{type}:{hash}`） |
| `bot_username` | string | 所属 Bot 用户名 |
| `file_type` | string | 文件类型：`photo` / `video` / `audio` / `document` / `voice` |
| `telegram_file_id` | string | Telegram 文件 ID（用于发送） |
| `file_size` | int | 文件大小（字节） |
| `file_unique_id` | string | Telegram 文件唯一 ID |
| `user_id` | int | 上传者 Telegram 用户 ID |
| `created_at` | string | 创建时间 |

### 用途

缓存集合内的完整文件列表，当用户请求发送集合时，直接从缓存获取所有文件信息，避免多次查询数据库。

### 相关代码

- **写入/读取**: `db_collections.py` — `get_collection_files()`
- **清除**: `db_collections.py` — 集合文件变更时

---

## 3. `stats:sent:{date}:{bot_name}` — 每日发送统计

### 键格式

有两种格式：

```
stats:sent:{YYYY-MM-DD}:{bot_username}    # 按 Bot 统计
stats:sent:{YYYY-MM-DD}:total             # 全平台汇总
```

**示例**:
- `stats:sent:2026-05-14:**************` — 该 Bot 在 2026-05-14 的发送批次数
- `stats:sent:2026-05-15:total` — 全平台在 2026-05-15 的发送批次总数

### 数据结构

- **Redis 类型**: String（整数计数器）
- **底层命令**: `INCR` + `EXPIRE`
- **TTL**: 172800 秒（2 天自动过期）

### 内容格式

值为纯整数值，表示当天累计的 **发送批次数**（不是文件数量）：

```
42
```

### 用途

每次 `send_batch()` 成功发送一批文件后，计数器 +1：

```python
# senders.py 中
await r.counter_incr(f"stats:sent:{today}:{bot_name}", ttl=86400 * 2)  # 按 Bot +1
await r.counter_incr(f"stats:sent:{today}:total", ttl=86400 * 2)       # 全局 +1
```

> ⚠️ **注意**: 当前代码中 **只有写入逻辑，没有读取逻辑**。统计数据存储在 Redis 中但尚未被使用。

### 相关代码

- **写入**: `senders.py` — `send_batch()` 函数
- **读取**: 当前无（`counter_get()` 方法已定义但未被调用读取统计数据）

---

## 4. `sq:{bot_name}` — 发送队列持久化

### 键格式

```
sq:{bot_username}
```

**示例**: `sq:***********`

### 数据结构

- **Redis 类型**: List（列表）
- **TTL**: 无（持久存在，直到任务处理完毕后手动清理）
- **底层命令**: `RPUSH`（写入）、`LRANGE`（读取）、`LREM`（删除已完成）、`DELETE`（清空重建）

### 内容格式

每个列表元素是一个 JSON 字符串，表示一个发送任务：

```json
{
  "id": "*************",
  "chat_id": *********,
  "files": [
    {
      "file_type": "photo",
      "telegram_file_id": "********************************",
      "code": "******_p:******"
    },
    {
      "file_type": "video",
      "telegram_file_id": "********************************",
      "code": "******_v:******"
    }
  ],
  "user_id": *********,
  "bot_username": "***********",
  "batch_id": "****************",
  "progress_msg_id": null,
  "total_files": 5,
  "sent_files": 0,
  "status": "pending"
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 任务唯一 ID（uuid4 hex[:12]） |
| `chat_id` | int | 目标聊天 ID（用户/群组） |
| `files` | array | 待发送文件列表 |
| `files[].file_type` | string | 文件类型：photo/video/audio/document/voice |
| `files[].telegram_file_id` | string | Telegram file_id |
| `files[].code` | string | 文件代码 |
| `user_id` | int | 请求发送的用户 ID |
| `bot_username` | string | Bot 用户名 |
| `batch_id` | string | 批次 ID |
| `progress_msg_id` | int/null | 进度消息 ID |
| `total_files` | int | 总文件数 |
| `sent_files` | int | 已发送文件数 |
| `status` | string | 任务状态：pending/sending/completed |

### 用途

每个 Bot 实例拥有一个独立的发送队列，用于：

1. **任务持久化**: Bot 进程重启后可恢复未完成的发送任务
2. **防丢失**: 进程崩溃/重启不会导致用户提交的任务丢失
3. **隔离**: 每个 Bot 的队列互相独立

### 相关代码

- **核心实现**: `send_queue.py` — `SendQueue` 类
- **Redis 操作**: `redis_manager.py` — `queue_push()` / `queue_pop()` / `queue_len()`
- **恢复逻辑**: `send_queue.py` — 启动时调用 `_restore_from_redis()`

---

## 5. `rate:user:{bot_username}:{user_id}` — 用户限流

### 键格式

```
rate:user:{bot_username}:{user_id}
```

**示例**: `rate:user:******:**********`

### 数据结构

- **Redis 类型**: Sorted Set (ZSET)
- **TTL**: 限流窗口 + 1 秒自动过期
- **底层命令**: `ZREMRANGEBYSCORE` + `ZCARD` + `ZADD` + `EXPIRE`（Pipeline 事务）

### 内容格式

ZSET 的 member 和 score 都是请求时间戳：

```
member: "*************"    score: *************
member: "*************"    score: *************
member: "*************"    score: *************
```

### 工作原理

滑动窗口限流算法：

1. **清理过期**: `ZREMRANGEBYSCORE` 清除窗口外的旧记录
2. **计数**: `ZCARD` 获取当前窗口内请求数
3. **记录**: `ZADD` 添加当前请求时间戳
4. **判断**: 如果请求数 ≥ 限制数，返回 `False` 并计算等待时间

```python
# redis_manager.py 中的限流检查
async def rate_limit_check(self, key, limit, window):
    pipe = self._redis.pipeline(transaction=True)
    pipe.zremrangebyscore(key, 0, window_start)  # 清除过期
    pipe.zcard(key)                                # 当前请求数
    pipe.zadd(key, {str(now): now})                # 添加当前请求
    pipe.expire(key, window + 1)                   # 设置过期
```

### 用途

限制单个用户在指定时间窗口内向特定 Bot 发送请求的频率，防止滥用。

**触发场景**:
- 用户发送图片/视频/音频/文档 → `handle_attachment()`
- 用户发送文本消息（可能是文件代码）→ `handle_text()`

### 相关代码

- **调用**: `handlers_messages.py` — `handle_attachment()` / `handle_text()`
- **实现**: `redis_manager.py` — `rate_limit_check()` / `rate_limit_wait()`
- **配置**: 限流参数来自 VIP 等级配置

---

## 键的生命周期

```
用户发送文件 ──→ rate:user: 写入（限流检查）
      │
      ├─→ sq: 写入（入发送队列）
      │
      └─→ stats:sent: +1（发送成功后计数）

用户查询集合 ──→ col: 查询（缓存命中/回源）
      │
      └─→ col_files: 查询（缓存命中/回源）
```

## 内存降级

当 Redis 未配置（`REDIS_URL` 为空）或连接失败时，所有功能自动降级为内存实现：

- **缓存**: Python `Dict[str, tuple]`
- **限流**: Python `Dict[str, list]`
- **计数器**: Python `Dict[str, int]`
- **队列**: 不持久化（进程重启后丢失）

> 降级模式下功能正常可用，但不支持跨进程共享和持久化。