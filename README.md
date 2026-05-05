# 🤖 FileID Bot 托管平台

基于 [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot) 的多Bot托管平台，用户可以通过主Bot创建和管理自己的 FileID Bot，无需独立服务器。

## ✨ 特性

- 🏠 **多Bot托管** - 一个服务器运行多个Bot，用户自助管理
- 🔄 **文件ID互转** - 发送文件获取代码，发送代码获取文件
- 📦 **集合功能** - 批量打包文件，一次发送多个
- 🐳 **Docker部署** - 一键部署，开箱即用
- 📊 **管理面板** - 平台统计数据一目了然

## 🏗️ 架构

```
主Bot (MasterBot)          ← 管理Bot，用户注册/管理
  ├── 用户Bot A (UserBot)   ← 独立Bot，完整FileID功能
  ├── 用户Bot B (UserBot)   ← 独立Bot，完整FileID功能
  └── 用户Bot C (UserBot)   ← 独立Bot，完整FileID功能
```

所有Bot共享同一进程和数据库，资源占用低。

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

## 📁 项目结构

```
├── main.py              # 主入口：启动主Bot + 加载用户Bot
├── config.py            # 配置管理
├── database.py          # 数据库操作（含 user_bots 表）
├── bot_manager.py       # Bot管理器：动态创建/停止用户Bot
├── handlers_master.py   # 主Bot命令处理器
├── handlers_commands.py # 用户Bot命令处理器
├── handlers_messages.py # 用户Bot消息处理器
├── handlers_callbacks.py# 用户Bot回调处理器
├── senders.py           # 文件发送逻辑
├── utils.py             # 工具函数
├── Dockerfile           # Docker镜像
├── docker-compose.yml   # Docker Compose配置
└── requirements.txt     # Python依赖
```

## 🔧 配置说明

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

##  License

MIT