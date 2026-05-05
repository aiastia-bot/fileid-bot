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
