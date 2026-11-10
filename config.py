import os
from pathlib import Path

# 加载 .env 文件
env_path = Path('.env')
if env_path.exists():
    with env_path.open() as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"\'"))

BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
ADMIN_IDS = [int(x) for x in os.environ.get('ADMIN_IDS', '').split(',') if x.strip().isdigit()]
CODE_PREFIX = os.environ.get('CODE_PREFIX', '')  # 自定义代码前缀，默认使用 bot 用户名（不带@）

MAX_COLLECTION_FILES = 666
AUTO_SEND_INTERVAL = 5  # 秒
GROUP_SEND_SIZE = 10  # 每组最多10个
CODE_LENGTH = 32  # 随机码长度
# ===== 数据库配置 =====
# 数据库类型：sqlite（默认）或 mysql
DB_TYPE = os.environ.get('DB_TYPE', 'sqlite')
# 数据库连接 URL（仅 MySQL 时需要配置）
# 格式: mysql+asyncmy://user:password@host:3306/dbname
DATABASE_URL = os.environ.get('DATABASE_URL', '')
# SQLite 数据库路径（仅 SQLite 模式使用）
DB_PATH = os.environ.get('DB_PATH', './data/fileid.db')
MAX_BOTS_PER_USER = int(os.environ.get('MAX_BOTS_PER_USER', '1'))  # 每个用户最多添加的Bot数

# ===== 发送限速与重试配置 =====
SEND_RETRY_COUNT = int(os.environ.get('SEND_RETRY_COUNT', '3'))       # 发送失败重试次数
SEND_RETRY_DELAY = float(os.environ.get('SEND_RETRY_DELAY', '2.0'))   # 重试基础延迟（秒），实际延迟 = delay * (2 ^ retry_count)
SEND_BATCH_DELAY = float(os.environ.get('SEND_BATCH_DELAY', '0.05'))   # 每组发送之间的延迟（秒）
SEND_INDIVIDUAL_DELAY = float(os.environ.get('SEND_INDIVIDUAL_DELAY', '1'))  # 单个文件发送之间的延迟（秒）
SEND_MAX_FILES_PER_REQUEST = int(os.environ.get('SEND_MAX_FILES_PER_REQUEST', '30'))  # 单次请求最大发送文件数
SEND_MIN_INTERVAL = float(os.environ.get('SEND_MIN_INTERVAL', '0'))  # 每 Bot 最小发送间隔（秒），避免并发叠加
API_READ_TIMEOUT = float(os.environ.get('API_READ_TIMEOUT', '30.0'))   # Telegram API 读取超时（秒）
API_WRITE_TIMEOUT = float(os.environ.get('API_WRITE_TIMEOUT', '30.0')) # Telegram API 写入超时（秒）
API_CONNECT_TIMEOUT = float(os.environ.get('API_CONNECT_TIMEOUT', '10.0'))  # Telegram API 连接超时（秒）

# ===== 全局 API 限速配置 =====
# 每 Bot 所有 API 调用的最小间隔（秒），包括 send_message / edit_message / send_file 等
# 设为 0.2 秒 → 每 Bot 最大 5 次 API 调用/秒（Telegram 限制约 30 次/秒，留足余量）
BOT_MIN_API_INTERVAL = float(os.environ.get('BOT_MIN_API_INTERVAL', '0.2'))

# ===== 单机防雪崩配置 =====
WEBHOOK_UPDATE_TIMEOUT = float(os.environ.get('WEBHOOK_UPDATE_TIMEOUT', '59.0'))  # 单个 webhook 更新最大处理时间（秒），必须 < Telegram 60s 超时，否则会 499
RETRY_AFTER_MAX_WAIT = float(os.environ.get('RETRY_AFTER_MAX_WAIT', '60.0'))  # 单次 RetryAfter 最大等待秒数，超过则放弃让 Telegram 重试
PER_BOT_CONCURRENCY = int(os.environ.get('PER_BOT_CONCURRENCY', '3'))  # 每个 Bot 最大并发处理数

# ===== Redis 配置（可选） =====
# 未配置时自动降级为内存方案，不影响正常运行
REDIS_URL = os.environ.get('REDIS_URL', '')  # 如 redis://localhost:6379/0

# ===== 用户限流配置 =====
RATE_LIMIT_WINDOW = int(os.environ.get('RATE_LIMIT_WINDOW', '60'))     # 限流窗口（秒）
RATE_LIMIT_MAX = int(os.environ.get('RATE_LIMIT_MAX', '30'))           # 窗口内最大请求数
RATE_LIMIT_MAX_WAIT = float(os.environ.get('RATE_LIMIT_MAX_WAIT', '30'))  # 排队最大等待时间（秒）

# ===== Webhook 模式配置 =====
BOT_MODE = os.environ.get('BOT_MODE', 'polling')  # 'polling' 或 'webhook'
WEBHOOK_HOST = os.environ.get('WEBHOOK_HOST', '')  # 外部域名，如 'bots.example.com'（不含 https://）
WEBHOOK_PORT = int(os.environ.get('WEBHOOK_PORT', '8080'))  # 本地监听端口
WEBHOOK_PATH = os.environ.get('WEBHOOK_PATH', '/webhook')  # URL 基路径
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', '')  # 可选的 webhook secret token

# ===== 群组支持 =====
ALLOW_GROUP = os.environ.get('ALLOW_GROUP', 'false').lower() in ('true', '1', 'yes')  # 是否允许群组使用（默认仅私聊）

# ===== VIP 等级配置 =====
# VIP等级: {level: {name, max_bots, monthly_price, yearly_price}}
# VIP 0 使用 MAX_BOTS_PER_USER，VIP 1-3 可通过 VIP1_MAX_BOTS 等环境变量配置
# 基础版（BASIC_LEVEL=4）：权益与免费一致，仅在「免费注册关闭」时作为付费入口（10⭐/月）
VIP_PLANS = {
    0: {'name': '免费用户', 'max_bots': int(os.environ.get('MAX_BOTS_PER_USER', '1')), 'monthly_price': 0, 'yearly_price': 0},
    1: {'name': 'VIP 1', 'max_bots': int(os.environ.get('VIP1_MAX_BOTS', os.environ.get('MAX_BOTS_PER_USER', '1'))), 'monthly_price': 100, 'yearly_price': 1000},
    2: {'name': 'VIP 2', 'max_bots': int(os.environ.get('VIP2_MAX_BOTS', '3')), 'monthly_price': 150, 'yearly_price': 1500},
    3: {'name': 'VIP 3', 'max_bots': int(os.environ.get('VIP3_MAX_BOTS', '6')), 'monthly_price': 500, 'yearly_price': 5000},
    4: {'name': '基础版', 'max_bots': int(os.environ.get('MAX_BOTS_PER_USER', '1')), 'monthly_price': 10, 'yearly_price': 100},
}

# 基础版等级号（过期后不降级回 0，保持 level 4 + 过期时间，max_bots 视为 0）
BASIC_LEVEL = 4

# VIP 特权功能（高等级包含低等级所有特权）
# forward_mode: Bot 主人可控制发送的图片/视频是否允许转发 (VIP 1+，基础版不含)
VIP_FEATURES = {
    0: {'forward_mode': False},
    1: {'forward_mode': True},
    2: {'forward_mode': True},
    3: {'forward_mode': True},
    4: {'forward_mode': False},
}

# 转发模式常量（整数）
FORWARD_MODE_ALLOW = 0       # 默认允许
FORWARD_MODE_DENY = -1       # 禁止转发
FORWARD_MODE_USER_CHOICE = 1 # 用户自定义

# VIP 0（免费）用户最大数量限制，超过后提示升级 VIP
MAX_VIP0_USERS = int(os.environ.get('MAX_VIP0_USERS', '0'))  # 0 = 不限制

# ===== Bot 文件数量上限（按 Bot 计，超限后文件仍入库但不返回代码） =====
# 免费 / 基础版可运行时调整（/setfree files N、/setfree basicfiles N），VIP 1-3 不限制
FREE_BOT_FILES_LIMIT = int(os.environ.get('FREE_BOT_FILES_LIMIT', '20000'))
BASIC_BOT_FILES_LIMIT = int(os.environ.get('BASIC_BOT_FILES_LIMIT', '50000'))

# VIP 到期提醒提前天数
VIP_EXPIRE_NOTICE_DAYS = 3

# Telegram Premium 会员赠送基础价（Bot 用 Stars 支付给 Telegram 的成本，官方固定价）
# month_count -> star_count（来自 Bot API giftPremiumSubscription 文档）
PREMIUM_GIFT_PRICES = {3: 1000, 6: 1500, 12: 2500}

# 用户购买 Premium 时 Bot 额外收取的费用（利润，单位 Stars）
PREMIUM_USER_MARKUP = int(os.environ.get('PREMIUM_USER_MARKUP', '100'))

FILE_TYPE_MAP = {
    'photo': '🖼 图片',
    'video': '🎬 视频',
    'audio': '🎵 音频',
    'document': '📄 文档',
    'voice': '🎤 语音',
}

# ===== 主 Bot 快捷命令列表 =====
# 普通用户菜单可见
MASTER_USER_COMMANDS = [
    ("start", "开始使用 / 查看帮助"),
    ("vip", "VIP 会员 / 购买星星"),
    ("premium", "购买 Telegram Premium 会员"),
    ("newbot", "一键创建你的 Bot"),
    ("addbot", "添加你的 Bot"),
    ("mybots", "查看我的 Bot 列表"),
    ("delbot", "删除 Bot"),
    ("botstatus", "查看 Bot 运行状态"),
    ("updatetoken", "更新失效的 Token"),
]

# 管理员专属（仅管理员的命令菜单可见，普通用户菜单不显示）
MASTER_ADMIN_COMMANDS = [
    ("helpadmin", "管理员命令帮助"),
    ("setvip", "会员管理：设置/查询/列表"),
    ("setplan", "配置套餐价格与权益"),
    ("vipadmin", "调整时长 / 审计 / 订单核查"),
    ("tasks", "查看失败消息和发送任务"),
    ("setfree", "免费注册/名额/文件额度管理"),
    ("mystars", "星星资产 / 礼物 / Premium 代充"),
    ("setgroup", "用户 Bot 沟通群组管理"),
    ("platform", "平台统计"),
    ("blacklist", "黑名单管理"),
    ("export", "导出数据"),
    ("broadcast", "广播消息"),
    ("startbot", "重启/启动Bot"),
    ("stopbot", "停止指定Bot"),
]

MASTER_BOT_COMMANDS = MASTER_USER_COMMANDS + MASTER_ADMIN_COMMANDS

# ===== 用户子 Bot 快捷命令列表 =====
USER_BOT_COMMANDS = [
    ("start", "开始使用 / 查看帮助"),
    ("help", "查看帮助"),
    ("create", "创建集合 create 名称"),
    ("pack", "通过代码打包创建集合"),
    ("done", "完成集合"),
    ("cancel", "取消当前操作"),
    ("getid", "回复消息获取文件ID"),
    ("mycol", "查看我的集合"),
    ("stop", "停止所有发送任务"),
    ("queue", "查看发送进度和等待数量"),
    ("delcol", "删除集合 delcol 代码"),
    ("settings", "用户偏好设置"),
]

FILE_TYPE_PREFIX = {
    'photo': 'p',
    'video': 'v',
    'document': 'd',
    'audio': 'd',
    'voice': 'd',
}


# ===== 分布式架构配置 =====
# 节点角色：standalone（单机，默认）/ master（主控节点）/ worker（工作节点）
ROLE = os.environ.get('ROLE', 'standalone')

# ===== 日志级别 =====
# 可选: DEBUG, INFO, WARNING, ERROR
# 日常推荐 WARNING（只显示警告和错误），排查问题时用 INFO 或 DEBUG
LOG_LEVEL = os.environ.get('LOG_LEVEL', 'WARNING')

# Master 节点地址（Worker 需要配置，用于向 Master 汇报状态）
MASTER_URL = os.environ.get('MASTER_URL', '')  # 如 https://1.1.1.1:8080

# Worker 节点标识（每个 Worker 唯一）
NODE_ID = os.environ.get('NODE_ID', 'local')

# Worker 内部通信密钥（Master 和 Worker 必须一致）
WORKER_SECRET = os.environ.get('WORKER_SECRET', '')

# Worker 内部 API 端口（Worker 节点监听的端口）
WORKER_PORT = int(os.environ.get('WORKER_PORT', '8081'))

# 每个 Worker 节点最大 Bot 数量
MAX_BOTS_PER_WORKER = int(os.environ.get('MAX_BOTS_PER_WORKER', '100'))

# Worker 健康检查间隔（秒）
HEALTH_CHECK_INTERVAL = int(os.environ.get('HEALTH_CHECK_INTERVAL', '60'))

# Worker 自身对外 Webhook 域名（Worker 模式下需要配置，用于设置 Bot webhook）
WORKER_WEBHOOK_HOST = os.environ.get('WORKER_WEBHOOK_HOST', '')  # 如 node1.example.com


# Reliability and resource limits
WORKER_URL = os.environ.get('WORKER_URL', '')
INBOX_WORKERS = int(os.environ.get('INBOX_WORKERS', '4'))
INBOX_MAX_ATTEMPTS = int(os.environ.get('INBOX_MAX_ATTEMPTS', '8'))
SEND_QUEUE_MAX_PENDING = int(os.environ.get('SEND_QUEUE_MAX_PENDING', '2000'))
SEND_QUEUE_MAX_PER_USER = int(os.environ.get('SEND_QUEUE_MAX_PER_USER', '100'))
SEND_QUEUE_MAX_RETRIES = int(os.environ.get('SEND_QUEUE_MAX_RETRIES', '3'))
DATA_RETENTION_DAYS = int(os.environ.get('DATA_RETENTION_DAYS', '30'))
# Existing installations retain the old behaviour unless explicitly changed.
FILE_QUOTA_POLICY = os.environ.get('FILE_QUOTA_POLICY', 'retain')  # retain | reject


def validate_config():
    if ROLE not in ('standalone', 'master', 'worker') or BOT_MODE not in ('polling', 'webhook'):
        raise ValueError('Invalid ROLE or BOT_MODE')
    if DB_TYPE not in ('sqlite', 'mysql'):
        raise ValueError('DB_TYPE must be sqlite or mysql')
    if ROLE in ('master', 'worker') and not WORKER_SECRET:
        raise ValueError('Distributed mode requires WORKER_SECRET')
    if BOT_MODE == 'webhook':
        import re
        if not re.fullmatch(r'[A-Za-z0-9_-]{16,256}', WEBHOOK_SECRET):
            raise ValueError('Webhook mode requires WEBHOOK_SECRET (16-256 letters, digits, _ or -)')
    if ROLE == 'worker' and (not WORKER_URL or not MASTER_URL or not WORKER_WEBHOOK_HOST or BOT_MODE != 'webhook'):
        raise ValueError('Worker requires WORKER_URL, MASTER_URL, WORKER_WEBHOOK_HOST and BOT_MODE=webhook')
    if ROLE == 'worker' and NODE_ID == 'local':
        raise ValueError('Worker requires a unique NODE_ID other than local')
    import math
    for value in (API_MESSAGES_PER_SECOND, API_PRIVATE_INTERVAL, API_GROUP_INTERVAL, API_EDIT_INTERVAL):
        if not math.isfinite(value) or value <= 0:
            raise ValueError('API message rate and chat/edit intervals must be positive and finite')
    for value in (BOT_MIN_API_INTERVAL, API_429_JITTER, RETRY_AFTER_MAX_WAIT):
        if not math.isfinite(value) or value < 0:
            raise ValueError('API pacing, jitter and retry wait must be non-negative and finite')
    if API_MAX_INFLIGHT <= 0 or API_429_RETRIES < 0:
        raise ValueError('API_MAX_INFLIGHT must be positive and API_429_RETRIES non-negative')
    if FILE_QUOTA_POLICY not in ('retain', 'reject'):
        raise ValueError('FILE_QUOTA_POLICY must be retain or reject')
    for value in (INBOX_WORKERS, INBOX_MAX_ATTEMPTS, SEND_QUEUE_MAX_PENDING,
                  SEND_QUEUE_MAX_PER_USER, SEND_QUEUE_MAX_RETRIES, PER_BOT_CONCURRENCY, DATA_RETENTION_DAYS):
        if value <= 0:
            raise ValueError('Concurrency, queue, retry and retention limits must be positive')


# Unified Telegram API pacing. Albums count their individual media items.
API_MESSAGES_PER_SECOND = float(os.environ.get('API_MESSAGES_PER_SECOND', '20'))
API_PRIVATE_INTERVAL = float(os.environ.get('API_PRIVATE_INTERVAL', '1.1'))
API_GROUP_INTERVAL = float(os.environ.get('API_GROUP_INTERVAL', '3.2'))
API_EDIT_INTERVAL = float(os.environ.get('API_EDIT_INTERVAL', '2.0'))
API_MAX_INFLIGHT = int(os.environ.get('API_MAX_INFLIGHT', '8'))
API_429_RETRIES = int(os.environ.get('API_429_RETRIES', '2'))
API_429_JITTER = float(os.environ.get('API_429_JITTER', '0.2'))
