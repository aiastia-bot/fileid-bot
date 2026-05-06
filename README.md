# 🤖 FileID Bot 托管平台

基于 [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) 的多Bot托管平台，用户可以通过主Bot创建和管理自己的 FileID Bot，无需独立服务器。

## ✨ 特性

- 🏠 **多Bot托管** - 一个服务器运行多个Bot，用户自助管理
- 🔄 **文件ID互转** - 发送文件获取代码，发送代码获取文件
- 📦 **集合功能** - 批量打包文件，一次发送多个
- 🐳 **Docker部署** - 一键部署，开箱即用
- 📊 **管理面板** - 平台统计数据一目了然
- 🌐 **分布式架构** - 支持多机部署，Master-Worker 负载均衡

## 🏗️ 架构

### 单机模式（Standalone，默认）

```
主Bot (MasterBot)          ← 管理Bot，用户注册/管理
  ├── 用户Bot A (UserBot)   ← 独立Bot，完整FileID功能
  ├── 用户Bot B (UserBot)   ← 独立Bot，完整FileID功能
  └── 用户Bot C (UserBot)   ← 独立Bot，完整FileID功能
```

所有Bot共享同一进程和数据库，资源占用低。适合起步阶段。

### 分布式模式（Master + Worker）

当 Bot 数量增长到几百个时，可以切换到分布式部署：

```
┌──────── 服务器 A（Master）──────────┐
│  主 Bot + 调度器 + 数据库           │
│  接收用户命令，分配 Bot 到 Worker    │
└──────────────┬──────────────────────┘
               │ HTTP API
    ┌──────────┼──────────┐
    ▼                     ▼
┌─ 服务器 B（Worker 1）─┐  ┌─ 服务器 C（Worker 2）─┐
│  Bot #1~#100          │  │  Bot #101~#200        │
│  处理用户消息          │  │  处理用户消息          │
└───────────────────────┘  └───────────────────────┘
```

- **同一个 Docker 镜像**，通过 `ROLE` 环境变量区分角色
- **Standalone 模式零改动**，不设置 `ROLE` 时和之前完全一样
- **平滑迁移**：从单机到分布式只需修改环境变量，代码无需改动

详见下方 [分布式部署](#-分布式部署可选) 章节。

## 🚀 快速开始

### 1. 创建主Bot

在 [@BotFather](https://t.me/BotFather) 创建一个Bot，这个Bot将作为管理Bot（平台入口）。

### 2. 配置环境

```bash
cp .env.example .env
```

编辑 `.env` 文件：

```env
# 主Bot Token
BOT_TOKEN=123456:ABC-DEF...

# 管理员ID（从 @userinfobot 获取）
ADMIN_IDS=123456789

# 每用户最大Bot数
MAX_BOTS_PER_USER=5
```

### 3. Docker 部署（推荐）

```bash
docker compose up -d
```

### 4. 手动部署

```bash
pip install -r requirements.txt
python main.py
```

## 🌐 Webhook 模式（可选）

默认使用 **Polling 模式**（长轮询），无需额外配置。如果需要更低延迟和更好的稳定性，可以切换到 **Webhook 模式**。

### 架构说明

```
Telegram服务器
    ↓ HTTPS POST
你的反代（Nginx/Caddy等，处理SSL）
    ↓ HTTP 转发
FileID Bot 容器（aiohttp 监听 8080）
    ├── POST /webhook/master    → 主Bot
    ├── POST /webhook/1         → 用户Bot #1
    ├── POST /webhook/2         → 用户Bot #2
    └── GET  /health            → 健康检查
```

### 配置步骤

**1. 准备域名和反代**

你需要一个域名（如 `bots.example.com`）并将其反向代理到容器的 8080 端口。反代负责 SSL 终止。

Nginx 配置示例：

```nginx
server {
    listen 443 ssl;
    server_name bots.example.com;

    ssl_certificate     /path/to/cert.pem;
    ssl_certificate_key /path/to/key.pem;

    location /webhook/ {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    location /health {
        proxy_pass http://127.0.0.1:8080;
    }
}
```

Caddy 配置示例（自动HTTPS）：

```
bots.example.com {
    reverse_proxy localhost:8080
}
```

**2. 配置环境变量**

在 `.env` 文件中添加：

```env
# 切换到 webhook 模式
BOT_MODE=webhook

# 你的外部域名（不含 https://）
WEBHOOK_HOST=bots.example.com

# 本地监听端口（默认8080）
WEBHOOK_PORT=8080

# Webhook URL 基路径（默认 /webhook）
WEBHOOK_PATH=/webhook

# 可选：验证请求来源的密钥
WEBHOOK_SECRET=your_random_secret_here
```

**3. 启动服务**

```bash
docker compose up -d
```

**4. 验证**

```bash
# 健康检查
curl http://localhost:8080/health

# 返回示例
# {"status":"ok","mode":"webhook","bots":3}
```

### 模式对比

| 特性 | Polling 模式 | Webhook 模式 |
|------|-------------|-------------|
| 延迟 | 较高（轮询间隔） | 低（实时推送） |
| 资源占用 | 较高（持续轮询） | 较低（事件驱动） |
| 配置难度 | 零配置 | 需要域名+反代 |
| 网络要求 | 出站连接 | 入站连接（公网可达） |
| 多Bot性能 | 每个 Bot 独立轮询 | 共享一个 HTTP 服务器 |
| 适用场景 | 开发测试、少量Bot | 生产环境、大量Bot |

### 注意事项

- Webhook 模式要求服务器**公网可达**，Telegram 需要能访问到你的域名
- 必须使用 **HTTPS**（通过反代实现，程序本身只监听 HTTP）
- 切换模式后需要**重启服务**
- `WEBHOOK_SECRET` 建议设置，防止非法请求伪造更新

## 📱 使用方法

### 平台用户流程

1. **创建Bot** - 在 [@BotFather](https://t.me/BotFather) 创建自己的Bot
2. **添加到平台** - 向主Bot发送 `/addbot <Token>`
3. **使用Bot** - 直接向自己的Bot发送文件即可

### 主Bot命令

| 命令 | 说明 |
|------|------|
| `/start` | 查看平台介绍和使用说明 |
| `/addbot <Token>` | 添加你的Bot到平台 |
| `/mybots` | 查看你的Bot列表和状态 |
| `/delbot @username` | 删除指定Bot |
| `/botstatus` | 查看Bot运行状态 |
| `/platform` | 平台统计（管理员） |

### 用户Bot命令

| 命令 | 说明 |
|------|------|
| `/start` | 查看帮助 |
| `/create 名称` | 创建集合 |
| `/done` | 完成集合并生成代码 |
| `/cancel` | 取消当前操作 |
| `/getid` | 回复消息获取文件ID |
| `/mycol` | 查看我的集合 |
| `/delcol 代码` | 删除集合 |
| `/stats` | 统计信息 |
| `/export` | 导出数据 |

## 🌐 分布式部署（可选）

当 Bot 数量超过 100 个或需要跨服务器负载均衡时，可以切换到分布式模式。

### 三种角色

| 角色 | 说明 | 需要的配置 |
|------|------|-----------|
| `standalone` | 单机模式（默认），和之前完全一样 | 只需 `BOT_TOKEN` |
| `master` | 主控节点，运行主 Bot + 调度器 | `BOT_TOKEN` + `WORKER_SECRET` |
| `worker` | 工作节点，只运行用户 Bot | `MASTER_URL` + `WORKER_SECRET` |

### 架构说明

```
用户创建Bot（/newbot）
    ↓
Master 节点选择最空闲的 Worker
    ↓
通知 Worker 启动该 Bot（HTTP API）
    ↓
设置 Bot 的 webhook 指向该 Worker
    ↓
用户消息直接到达 Worker 处理（不经过 Master）
```

### 单机模拟分布式

可以先在一台服务器上测试分布式：

```bash
# Master（端口 8080）
docker run -d --name master \
  -e ROLE=master \
  -e BOT_MODE=webhook \
  -e BOT_TOKEN=你的Token \
  -e WEBHOOK_HOST=bots.example.com \
  -e WORKER_SECRET=mysecret123 \
  -v ./data:/app/data \
  -p 8080:8080 \
  fileid-bot

# Worker 1（端口 8081）
docker run -d --name worker-1 \
  -e ROLE=worker \
  -e BOT_MODE=webhook \
  -e NODE_ID=worker-1 \
  -e MASTER_URL=http://master:8080 \
  -e WORKER_SECRET=mysecret123 \
  -e WORKER_PORT=8081 \
  -e WORKER_WEBHOOK_HOST=bots.example.com \
  -v ./data:/app/data \
  -p 8081:8081 \
  fileid-bot
```

### 多机部署示例

使用 Docker Compose 分别在不同服务器上部署：

**服务器 A（Master）：**

```yaml
# docker-compose.yml
services:
  master:
    build: .
    environment:
      - ROLE=master
      - BOT_MODE=webhook
      - BOT_TOKEN=${BOT_TOKEN}
      - ADMIN_IDS=${ADMIN_IDS}
      - WEBHOOK_HOST=bots.example.com
      - WORKER_SECRET=your_strong_secret
    volumes:
      - ./data:/app/data
    ports:
      - "8080:8080"
```

**服务器 B（Worker 1）：**

```yaml
services:
  worker:
    build: .
    environment:
      - ROLE=worker
      - BOT_MODE=webhook
      - NODE_ID=worker-1
      - MASTER_URL=https://1.1.1.1:8080
      - WORKER_SECRET=your_strong_secret
      - WORKER_PORT=8081
      - WORKER_WEBHOOK_HOST=node1.example.com
    volumes:
      - ./data:/app/data
    ports:
      - "8081:8081"
```

### 内部 API

Master 和 Worker 之间通过以下 HTTP API 通信（使用 `X-Worker-Secret` 头鉴权）：

| API | 方法 | 说明 |
|-----|------|------|
| `/internal/register` | POST | Worker 注册到 Master |
| `/internal/heartbeat` | POST | Worker 定期心跳 |
| `/internal/worker_offline` | POST | Worker 通知 Master 离线 |
| `/internal/workers` | GET | 查看所有 Worker 状态 |
| `/internal/start` | POST | Master 通知 Worker 启动 Bot |
| `/internal/stop` | POST | Master 通知 Worker 停止 Bot |
| `/internal/restart` | POST | Master 通知 Worker 重启 Bot |
| `/internal/status` | GET | 查询 Worker 详细状态 |
| `/internal/health` | GET | 健康检查（无需鉴权） |

### 从 Standalone 迁移到分布式

1. 在新服务器上部署 Worker 容器（使用相同的 Docker 镜像）
2. 修改 Master 服务器的环境变量：`ROLE=standalone` → `ROLE=master`，添加 `WORKER_SECRET`
3. 重启 Master 容器
4. 调度器自动将已有 Bot 分配到 Worker 节点

**注意：** 分布式模式支持 **Polling 和 Webhook** 两种模式。推荐使用 Webhook 模式（性能更好、资源占用更低）；Polling 模式也可以工作（配置更简单，不需要公网入站连接），但每个 Worker 上的 Bot 会独立轮询，资源占用较高。

### 容量参考

| 部署方式 | 预估 Bot 数量 | 说明 |
|----------|-------------|------|
| Standalone + Polling | 50~100 | 每个 Bot 独立轮询 |
| Standalone + Webhook | 100~300 | 共享 HTTP 服务器 |
| 1 Master + 2 Worker | 300~600 | 每个Worker 100~300 |
| 1 Master + N Worker | N × 300 | 线性扩展 |

## 📁 项目结构

```
├── main.py              # 主入口：根据 ROLE 选择 standalone/master/worker 启动
├── config.py            # 配置管理（含分布式配置）
├── database.py          # 数据库操作（含 worker_nodes 表）
├── bot_manager.py       # Bot管理器 + Master调度器
├── worker_server.py     # Worker 节点 HTTP 服务（启动/停止/心跳 API）
├── handlers_master.py   # 主Bot命令处理器
├── handlers_commands.py # 用户Bot命令处理器
├── handlers_messages.py # 用户Bot消息处理器
├── handlers_callbacks.py# 用户Bot回调处理器
├── senders.py           # 文件发送逻辑
├── utils.py             # 工具函数
├── Dockerfile           # Docker镜像（Master/Worker 共用）
├── docker-compose.yml   # Docker Compose配置
└── requirements.txt     # Python依赖
```

## 🔧 配置说明

### 基础配置

| 环境变量 | 必填 | 默认值 | 说明 |
|----------|------|--------|------|
| `BOT_TOKEN` | ✅ | - | 主Bot Token |
| `ADMIN_IDS` | ❌ | - | 管理员Telegram ID |
| `MAX_BOTS_PER_USER` | ❌ | 1 | 每用户最大Bot数 |
| `CODE_PREFIX` | ❌ | Bot用户名 | 文件代码前缀 |
| `BOT_MODE` | ❌ | `polling` | 运行模式：`polling` 或 `webhook` |
| `WEBHOOK_HOST` | webhook时必填 | - | 外部域名（不含 https://） |
| `WEBHOOK_PORT` | ❌ | `8080` | 本地监听端口 |
| `WEBHOOK_PATH` | ❌ | `/webhook` | Webhook URL 基路径 |
| `WEBHOOK_SECRET` | ❌ | - | 验证请求来源的密钥 |
| `ALLOW_GROUP` | ❌ | `false` | 是否允许群组使用 |

### 分布式配置

| 环境变量 | 必填 | 默认值 | 说明 |
|----------|------|--------|------|
| `ROLE` | ❌ | `standalone` | 节点角色：`standalone` / `master` / `worker` |
| `MASTER_URL` | Worker 必填 | - | Master 节点地址（如 `http://master:8080`） |
| `NODE_ID` | Worker 必填 | `local` | Worker 节点唯一标识 |
| `WORKER_SECRET` | 分布式必填 | - | 内部通信密钥，Master 和 Worker 必须一致 |
| `WORKER_PORT` | ❌ | `8081` | Worker HTTP 服务端口 |
| `MAX_BOTS_PER_WORKER` | ❌ | `100` | 每个 Worker 最大 Bot 数量 |
| `HEALTH_CHECK_INTERVAL` | ❌ | `60` | Worker 心跳间隔（秒） |
| `WORKER_WEBHOOK_HOST` | Worker 必填 | - | Worker 对外 Webhook 域名 |

##  License

MIT