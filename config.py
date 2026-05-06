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
                os.environ[key.strip()] = value.strip()

BOT_TOKEN = os.environ.get('BOT_TOKEN', '')
ADMIN_IDS = [int(x) for x in os.environ.get('ADMIN_IDS', '').split(',') if x.strip().isdigit()]
CODE_PREFIX = os.environ.get('CODE_PREFIX', '')  # 自定义代码前缀，默认使用 bot 用户名（不带@）

MAX_COLLECTION_FILES = 999
AUTO_SEND_INTERVAL = 5  # 秒
GROUP_SEND_SIZE = 10  # 每组最多10个
CODE_LENGTH = 32  # 随机码长度
DB_PATH = './data/fileid.db'
MAX_BOTS_PER_USER = int(os.environ.get('MAX_BOTS_PER_USER', '1'))  # 每个用户最多添加的Bot数

# ===== 发送限速与重试配置 =====
SEND_RETRY_COUNT = int(os.environ.get('SEND_RETRY_COUNT', '3'))       # 发送失败重试次数
SEND_RETRY_DELAY = float(os.environ.get('SEND_RETRY_DELAY', '2.0'))   # 重试基础延迟（秒），实际延迟 = delay * (2 ^ retry_count)
SEND_BATCH_DELAY = float(os.environ.get('SEND_BATCH_DELAY', '1.0'))   # 每组发送之间的延迟（秒）
SEND_INDIVIDUAL_DELAY = float(os.environ.get('SEND_INDIVIDUAL_DELAY', '0.5'))  # 单个文件发送之间的延迟（秒）
API_READ_TIMEOUT = float(os.environ.get('API_READ_TIMEOUT', '60.0'))   # Telegram API 读取超时（秒）
API_WRITE_TIMEOUT = float(os.environ.get('API_WRITE_TIMEOUT', '60.0')) # Telegram API 写入超时（秒）
API_CONNECT_TIMEOUT = float(os.environ.get('API_CONNECT_TIMEOUT', '30.0'))  # Telegram API 连接超时（秒）

# ===== Webhook 模式配置 =====
BOT_MODE = os.environ.get('BOT_MODE', 'polling')  # 'polling' 或 'webhook'
WEBHOOK_HOST = os.environ.get('WEBHOOK_HOST', '')  # 外部域名，如 'bots.example.com'（不含 https://）
WEBHOOK_PORT = int(os.environ.get('WEBHOOK_PORT', '8080'))  # 本地监听端口
WEBHOOK_PATH = os.environ.get('WEBHOOK_PATH', '/webhook')  # URL 基路径
WEBHOOK_SECRET = os.environ.get('WEBHOOK_SECRET', '')  # 可选的 webhook secret token

# ===== 群组支持 =====
ALLOW_GROUP = os.environ.get('ALLOW_GROUP', 'false').lower() in ('true', '1', 'yes')  # 是否允许群组使用（默认仅私聊）

FILE_TYPE_MAP = {
    'photo': '🖼 图片',
    'video': '🎬 视频',
    'audio': '🎵 音频',
    'document': '📄 文档',
    'voice': '🎤 语音',
}

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
