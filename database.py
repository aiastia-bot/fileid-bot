"""数据库操作模块"""
import sqlite3
import logging
from datetime import datetime
from typing import Optional, List, Dict

from config import DB_PATH

logger = logging.getLogger(__name__)


def get_db():
    """获取数据库连接（启用 WAL 模式和超时）"""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    """初始化数据库表"""
    conn = get_db()
    try:
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS user_bots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER NOT NULL,
                bot_token TEXT NOT NULL,
                bot_id INTEGER,
                bot_username TEXT,
                bot_firstname TEXT,
                status TEXT DEFAULT 'active',
                created_at TEXT,
                updated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ub_owner ON user_bots(owner_id);
            CREATE INDEX IF NOT EXISTS idx_ub_bot_id ON user_bots(bot_id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_ub_token ON user_bots(bot_token);

            CREATE TABLE IF NOT EXISTS file_mappings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                bot_username TEXT,
                file_type TEXT NOT NULL,
                telegram_file_id TEXT NOT NULL,
                file_size INTEGER DEFAULT 0,
                file_unique_id TEXT,
                user_id INTEGER,
                created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_file_code ON file_mappings(code);
            CREATE INDEX IF NOT EXISTS idx_file_user ON file_mappings(user_id);

            CREATE TABLE IF NOT EXISTS collections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT UNIQUE NOT NULL,
                bot_username TEXT,
                name TEXT DEFAULT '',
                user_id INTEGER,
                file_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'open',
                created_at TEXT,
                updated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_col_code ON collections(code);
            CREATE INDEX IF NOT EXISTS idx_col_user ON collections(user_id);

            CREATE TABLE IF NOT EXISTS collection_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collection_code TEXT NOT NULL,
                file_code TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0,
                FOREIGN KEY (collection_code) REFERENCES collections(code),
                FOREIGN KEY (file_code) REFERENCES file_mappings(code)
            );
            CREATE INDEX IF NOT EXISTS idx_ci_col ON collection_items(collection_code);

            CREATE TABLE IF NOT EXISTS user_blacklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                reason TEXT DEFAULT '',
                created_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_bl_user ON user_blacklist(user_id);

            CREATE TABLE IF NOT EXISTS platform_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        ''')
        # 迁移：添加 is_valid 字段
        try:
            conn.execute("ALTER TABLE file_mappings ADD COLUMN is_valid INTEGER DEFAULT 1")
        except Exception:
            pass

        conn.commit()
        logger.info("数据库初始化完成")
    finally:
        conn.close()



def mark_file_invalid(code: str) -> bool:
    """标记文件为无效（file_id 失效）"""
    conn = get_db()
    try:
        cursor = conn.execute(
            "UPDATE file_mappings SET is_valid = 0 WHERE code = ?",
            (code,)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def save_file(user_id: int, file_type: str, file_id: str,
              file_size: int, file_unique_id: str, bot_username: str,
              code_prefix: str) -> Optional[str]:
    """保存文件到数据库，返回完整代码"""
    import string, random
    from config import CODE_LENGTH

    conn = get_db()
    try:
        # 去重：如果同一 bot 下已存在相同 file_unique_id，直接返回已有代码
        if file_unique_id:
            existing = conn.execute(
                "SELECT code FROM file_mappings WHERE file_unique_id = ? AND bot_username = ?",
                (file_unique_id, bot_username)
            ).fetchone()
            if existing:
                logger.info("文件已存在，复用代码: %s (file_unique_id=%s)", existing['code'], file_unique_id)
                return existing['code']

        # 生成唯一代码
        chars = string.ascii_letters + string.digits
        while True:
            raw_code = ''.join(random.choices(chars, k=CODE_LENGTH))
            row = conn.execute(
                "SELECT id FROM file_mappings WHERE code = ? UNION SELECT id FROM collections WHERE code = ?",
                (raw_code, raw_code)
            ).fetchone()
            if not row:
                break

        from config import FILE_TYPE_PREFIX
        prefix = FILE_TYPE_PREFIX.get(file_type, 'd')
        full_code = f"{code_prefix}_{prefix}:{raw_code}"

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        conn.execute(
            """INSERT INTO file_mappings 
               (code, bot_username, file_type, telegram_file_id, file_size, file_unique_id, user_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (full_code, bot_username, file_type, file_id, file_size, file_unique_id, user_id, now)
        )
        conn.commit()
        return full_code
    except sqlite3.IntegrityError:
        logger.error("代码重复（极少发生）")
        return None
    except Exception as e:
        logger.error("保存文件失败: %s", e)
        return None
    finally:
        conn.close()


def get_file(code: str) -> Optional[Dict]:
    """根据代码获取文件信息"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM file_mappings WHERE code = ?", (code,)
        ).fetchone()
        if row:
            return dict(row)
        return None
    finally:
        conn.close()


def get_collection(code: str) -> Optional[Dict]:
    """获取集合信息"""
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM collections WHERE code = ?", (code,)).fetchone()
        if row:
            return dict(row)
        return None
    finally:
        conn.close()


def get_collection_files(code: str) -> List[Dict]:
    """获取集合中的所有文件"""
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT fm.* FROM file_mappings fm
               JOIN collection_items ci ON fm.code = ci.file_code
               WHERE ci.collection_code = ?
               ORDER BY ci.sort_order""",
            (code,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def create_collection(code: str, bot_username: str, name: str, user_id: int) -> bool:
    """创建新集合"""
    conn = get_db()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """INSERT INTO collections (code, bot_username, name, user_id, file_count, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 0, 'open', ?, ?)""",
            (code, bot_username, name, user_id, now, now)
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error("创建集合失败: %s", e)
        return False
    finally:
        conn.close()


def add_file_to_collection(col_code: str, file_code: str, sort_order: int) -> bool:
    """添加文件到集合"""
    conn = get_db()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT INTO collection_items (collection_code, file_code, sort_order) VALUES (?, ?, ?)",
            (col_code, file_code, sort_order)
        )
        conn.execute(
            "UPDATE collections SET file_count = ?, updated_at = ? WHERE code = ?",
            (sort_order, now, col_code)
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error("添加文件到集合失败: %s", e)
        return False
    finally:
        conn.close()


def complete_collection(col_code: str, file_count: int) -> bool:
    """完成集合"""
    conn = get_db()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE collections SET status = 'completed', file_count = ?, updated_at = ? WHERE code = ?",
            (file_count, now, col_code)
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error("完成集合失败: %s", e)
        return False
    finally:
        conn.close()


def delete_collection(col_code: str) -> bool:
    """删除集合及其文件项"""
    conn = get_db()
    try:
        conn.execute("DELETE FROM collection_items WHERE collection_code = ?", (col_code,))
        conn.execute("DELETE FROM collections WHERE code = ?", (col_code,))
        conn.commit()
        return True
    except Exception as e:
        logger.error("删除集合失败: %s", e)
        return False
    finally:
        conn.close()


def get_user_collections(user_id: int, limit: int = 20) -> List[Dict]:
    """获取用户集合列表"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT code, name, file_count, status, created_at FROM collections WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_stats() -> Dict:
    """获取统计数据"""
    conn = get_db()
    try:
        file_count = conn.execute("SELECT COUNT(*) as c FROM file_mappings").fetchone()['c']
        col_count = conn.execute("SELECT COUNT(*) as c FROM collections").fetchone()['c']
        user_count = conn.execute("SELECT COUNT(DISTINCT user_id) as c FROM file_mappings").fetchone()['c']
        today = datetime.now().strftime("%Y-%m-%d")
        today_files = conn.execute(
            "SELECT COUNT(*) as c FROM file_mappings WHERE created_at LIKE ?", (f"{today}%",)
        ).fetchone()['c']
        type_stats = conn.execute(
            "SELECT file_type, COUNT(*) as c FROM file_mappings GROUP BY file_type"
        ).fetchall()
        return {
            'file_count': file_count,
            'col_count': col_count,
            'user_count': user_count,
            'today_files': today_files,
            'type_stats': [dict(r) for r in type_stats],
        }
    finally:
        conn.close()


def get_all_files_for_export() -> List[Dict]:
    """导出所有文件记录"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT code, file_type, file_size, user_id, created_at FROM file_mappings ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ==================== 用户Bot管理 ====================

def add_user_bot(owner_id: int, bot_token: str, bot_id: int,
                 bot_username: str, bot_firstname: str) -> Optional[int]:
    """添加用户Bot到数据库，返回记录ID"""
    conn = get_db()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor = conn.execute(
            """INSERT INTO user_bots (owner_id, bot_token, bot_id, bot_username, bot_firstname, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
            (owner_id, bot_token, bot_id, bot_username, bot_firstname, now, now)
        )
        conn.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        logger.error("Bot Token 已存在")
        return None
    except Exception as e:
        logger.error("添加用户Bot失败: %s", e)
        return None
    finally:
        conn.close()


def get_all_active_user_bots() -> List[Dict]:
    """获取所有活跃的用户Bot"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM user_bots WHERE status = 'active' ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_user_bots_by_owner(owner_id: int) -> List[Dict]:
    """获取用户的所有Bot"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM user_bots WHERE owner_id = ? AND status != 'deleted' ORDER BY created_at",
            (owner_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_user_bot_by_id(bot_db_id: int) -> Optional[Dict]:
    """根据数据库ID获取用户Bot"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM user_bots WHERE id = ?", (bot_db_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_bot_by_token(bot_token: str) -> Optional[Dict]:
    """根据Token获取用户Bot"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM user_bots WHERE bot_token = ? AND status != 'deleted'", (bot_token,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_bot_by_telegram_id(bot_id: int) -> Optional[Dict]:
    """根据Telegram Bot ID获取用户Bot"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM user_bots WHERE bot_id = ? AND status != 'deleted'", (bot_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_user_bot_status(bot_db_id: int, status: str) -> bool:
    """更新用户Bot状态"""
    conn = get_db()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE user_bots SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, bot_db_id)
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error("更新Bot状态失败: %s", e)
        return False
    finally:
        conn.close()


def delete_user_bot(bot_db_id: int) -> bool:
    """软删除用户Bot"""
    return update_user_bot_status(bot_db_id, 'deleted')


def get_platform_stats() -> Dict:
    """获取平台级统计数据"""
    conn = get_db()
    try:
        bot_count = conn.execute("SELECT COUNT(*) as c FROM user_bots WHERE status = 'active'").fetchone()['c']
        owner_count = conn.execute("SELECT COUNT(DISTINCT owner_id) as c FROM user_bots WHERE status = 'active'").fetchone()['c']
        file_count = conn.execute("SELECT COUNT(*) as c FROM file_mappings").fetchone()['c']
        col_count = conn.execute("SELECT COUNT(*) as c FROM collections").fetchone()['c']
        return {
            'bot_count': bot_count,
            'owner_count': owner_count,
            'file_count': file_count,
            'col_count': col_count,
        }
    finally:
        conn.close()


def get_platform_bot_details() -> List[Dict]:
    """获取平台中每个 Bot 的详细信息（含文件数、集合数）"""
    conn = get_db()
    try:
        bots = conn.execute(
            "SELECT id, owner_id, bot_id, bot_username, bot_firstname, status, created_at FROM user_bots WHERE status != 'deleted' ORDER BY created_at"
        ).fetchall()
        result = []
        for bot in bots:
            bot_dict = dict(bot)
            # 统计该 Bot 的文件数
            file_count = conn.execute(
                "SELECT COUNT(*) as c FROM file_mappings WHERE bot_username = ?",
                (bot['bot_username'],)
            ).fetchone()['c']
            # 统计该 Bot 的集合数
            col_count = conn.execute(
                "SELECT COUNT(*) as c FROM collections WHERE bot_username = ?",
                (bot['bot_username'],)
            ).fetchone()['c']
            # 统计该 Bot 的独立用户数
            user_count = conn.execute(
                "SELECT COUNT(DISTINCT user_id) as c FROM file_mappings WHERE bot_username = ?",
                (bot['bot_username'],)
            ).fetchone()['c']
            bot_dict['file_count'] = file_count
            bot_dict['col_count'] = col_count
            bot_dict['user_count'] = user_count
            result.append(bot_dict)
        return result
    finally:
        conn.close()


# ==================== 黑名单/白名单管理 ====================

def add_to_blacklist(user_id: int, reason: str = '') -> bool:
    """添加用户到黑名单"""
    conn = get_db()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "INSERT OR REPLACE INTO user_blacklist (user_id, reason, created_at) VALUES (?, ?, ?)",
            (user_id, reason, now)
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error("添加黑名单失败: %s", e)
        return False
    finally:
        conn.close()


def remove_from_blacklist(user_id: int) -> bool:
    """从黑名单移除用户"""
    conn = get_db()
    try:
        cursor = conn.execute(
            "DELETE FROM user_blacklist WHERE user_id = ?", (user_id,)
        )
        conn.commit()
        return cursor.rowcount > 0
    except Exception as e:
        logger.error("移除黑名单失败: %s", e)
        return False
    finally:
        conn.close()


def is_user_blacklisted(user_id: int) -> bool:
    """检查用户是否在黑名单中"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM user_blacklist WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def get_blacklist() -> List[Dict]:
    """获取黑名单列表"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM user_blacklist ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_blacklist_count() -> int:
    """获取黑名单用户数量"""
    conn = get_db()
    try:
        return conn.execute("SELECT COUNT(*) as c FROM user_blacklist").fetchone()['c']
    finally:
        conn.close()


# ==================== 平台设置 ====================

def get_platform_setting(key: str, default: str = '') -> str:
    """获取平台设置"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT value FROM platform_settings WHERE key = ?", (key,)
        ).fetchone()
        return row['value'] if row else default
    finally:
        conn.close()


def set_platform_setting(key: str, value: str) -> bool:
    """设置平台设置"""
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO platform_settings (key, value) VALUES (?, ?)",
            (key, value)
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error("设置平台配置失败: %s", e)
        return False
    finally:
        conn.close()


# ==================== 按 Bot 导出文件代码 ====================

def get_files_by_bot_username(bot_username: str) -> List[Dict]:
    """根据 bot_username 导出该 Bot 的所有文件代码"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT code, file_type, file_size, user_id, created_at FROM file_mappings WHERE bot_username = ? ORDER BY created_at DESC",
            (bot_username,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ==================== 平台数据导出 ====================

def get_platform_export_data() -> Dict:
    """获取平台完整导出数据（用于代码导出）"""
    conn = get_db()
    try:
        bots = conn.execute(
            "SELECT id, owner_id, bot_id, bot_username, bot_firstname, status, created_at FROM user_bots WHERE status != 'deleted'"
        ).fetchall()
        files = conn.execute(
            "SELECT code, bot_username, file_type, file_size, user_id, created_at FROM file_mappings ORDER BY created_at DESC"
        ).fetchall()
        collections = conn.execute(
            "SELECT code, bot_username, name, user_id, file_count, status, created_at FROM collections ORDER BY created_at DESC"
        ).fetchall()
        blacklist = conn.execute(
            "SELECT user_id, reason, created_at FROM user_blacklist ORDER BY created_at DESC"
        ).fetchall()
        return {
            'bots': [dict(r) for r in bots],
            'files': [dict(r) for r in files],
            'collections': [dict(r) for r in collections],
            'blacklist': [dict(r) for r in blacklist],
        }
    finally:
        conn.close()
