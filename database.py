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


def _backfill_bot_db_id(conn):
    """智能回填 bot_db_id：根据时间戳将数据分配到正确的同名 Bot。

    逻辑：
    1. 获取所有同名 Bot 记录（包括 deleted/revoked），按 created_at 排序
    2. 对每条数据（file_mappings / collections），找到时间上对应的 Bot：
       - 数据创建时间 >= Bot 创建时间
       - 且在下一个同名 Bot 创建时间之前
    3. 如果只有一个同名 Bot，直接关联
    """
    # 检查是否需要回填
    unlinked_files = conn.execute(
        "SELECT COUNT(*) as c FROM file_mappings WHERE bot_db_id IS NULL"
    ).fetchone()['c']
    unlinked_cols = conn.execute(
        "SELECT COUNT(*) as c FROM collections WHERE bot_db_id IS NULL"
    ).fetchone()['c']

    if unlinked_files == 0 and unlinked_cols == 0:
        logger.info("无需回填 bot_db_id")
        return

    logger.info("开始回填 bot_db_id: %d 个文件, %d 个集合待处理", unlinked_files, unlinked_cols)

    # 获取所有 Bot 记录（包含 deleted/revoked），按 bot_username + created_at 排序
    all_bots = conn.execute(
        "SELECT id, bot_username, created_at FROM user_bots ORDER BY bot_username, created_at"
    ).fetchall()

    # 按 bot_username 分组
    bots_by_username: Dict[str, list] = {}
    for bot in all_bots:
        uname = bot['bot_username']
        if uname not in bots_by_username:
            bots_by_username[uname] = []
        bots_by_username[uname].append({
            'id': bot['id'],
            'created_at': bot['created_at'] or '',
        })

    # 对每个 bot_username，回填 file_mappings
    for username, bots in bots_by_username.items():
        if len(bots) == 1:
            # 只有一个同名 Bot，直接全部关联
            conn.execute(
                "UPDATE file_mappings SET bot_db_id = ? WHERE bot_username = ? AND bot_db_id IS NULL",
                (bots[0]['id'], username)
            )
            conn.execute(
                "UPDATE collections SET bot_db_id = ? WHERE bot_username = ? AND bot_db_id IS NULL",
                (bots[0]['id'], username)
            )
            continue

        # 多个同名 Bot：按时间区间分配
        # bots 已按 created_at 排序
        # 对于每条数据，找到 created_at <= 数据创建时间的最后一个 Bot
        for file_table in ['file_mappings', 'collections']:
            rows = conn.execute(
                f"SELECT rowid, created_at FROM {file_table} WHERE bot_username = ? AND bot_db_id IS NULL",
                (username,)
            ).fetchall()

            for row in rows:
                data_created = row['created_at'] or ''
                matched_bot_id = None

                # 找到 created_at <= data_created 的最后一个 Bot
                for bot in bots:
                    if bot['created_at'] <= data_created:
                        matched_bot_id = bot['id']
                    else:
                        break

                # 如果数据比所有 Bot 都早，关联到最早的 Bot
                if matched_bot_id is None and bots:
                    matched_bot_id = bots[0]['id']

                if matched_bot_id is not None:
                    conn.execute(
                        f"UPDATE {file_table} SET bot_db_id = ? WHERE rowid = ?",
                        (matched_bot_id, row['rowid'])
                    )

    logger.info("bot_db_id 回填完成")


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

            CREATE TABLE IF NOT EXISTS worker_nodes (
                node_id TEXT PRIMARY KEY,
                node_url TEXT NOT NULL,
                webhook_host TEXT DEFAULT '',
                max_bots INTEGER DEFAULT 100,
                current_bots INTEGER DEFAULT 0,
                status TEXT DEFAULT 'offline',
                last_heartbeat TEXT,
                created_at TEXT
            );
        ''')
        # 迁移：添加 is_valid 字段
        try:
            conn.execute("ALTER TABLE file_mappings ADD COLUMN is_valid INTEGER DEFAULT 1")
        except Exception:
            pass

        # 迁移：添加 node_id 字段（分布式架构：记录 Bot 分配到哪个节点）
        try:
            conn.execute("ALTER TABLE user_bots ADD COLUMN node_id TEXT DEFAULT 'local'")
        except Exception:
            pass

        # 迁移：添加 bot_db_id 字段（用于区分同名 Bot 的数据）
        try:
            conn.execute("ALTER TABLE file_mappings ADD COLUMN bot_db_id INTEGER")
        except Exception:
            pass
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_file_bot_db ON file_mappings(bot_db_id)")
        except Exception:
            pass

        try:
            conn.execute("ALTER TABLE collections ADD COLUMN bot_db_id INTEGER")
        except Exception:
            pass
        try:
            conn.execute("CREATE INDEX IF NOT EXISTS idx_col_bot_db ON collections(bot_db_id)")
        except Exception:
            pass

        # 智能回填旧数据：根据 bot_username + 时间戳匹配正确的 bot_db_id
        # 对于同名 Bot（删除后重建），根据数据创建时间分配到对应时期的 Bot
        try:
            _backfill_bot_db_id(conn)
        except Exception as e:
            logger.warning("回填 bot_db_id 失败（可忽略）: %s", e)

        conn.commit()
        logger.info("数据库初始化完成")
    finally:
        conn.close()



def get_active_bot_files(since_date: str = None) -> List[Dict]:
    """获取活跃Bot的文件列表，按bot_username+created_at排序，支持日期过滤"""
    conn = get_db()
    try:
        sql = """
            SELECT fm.code, fm.bot_username, fm.file_type, fm.file_size, fm.user_id, fm.created_at
            FROM file_mappings fm
            INNER JOIN user_bots ub ON fm.bot_db_id = ub.id
            WHERE ub.status = 'active' AND (fm.is_valid IS NULL OR fm.is_valid = 1)
        """
        params = []
        if since_date:
            sql += " AND fm.created_at >= ?"
            params.append(since_date)
        sql += " ORDER BY fm.bot_username, fm.created_at"
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
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
              code_prefix: str, bot_db_id: int = None) -> Optional[str]:
    """保存文件到数据库，返回完整代码。
    bot_db_id 用于区分同名 Bot 的数据，同名 Bot 删除后重建不会混淆。
    """
    import string, random
    from config import CODE_LENGTH

    conn = get_db()
    try:
        # 去重：如果同一 bot 下已存在相同 file_unique_id，直接返回已有代码
        if file_unique_id:
            if bot_db_id:
                existing = conn.execute(
                    "SELECT code FROM file_mappings WHERE file_unique_id = ? AND bot_db_id = ?",
                    (file_unique_id, bot_db_id)
                ).fetchone()
            else:
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
               (code, bot_username, file_type, telegram_file_id, file_size, file_unique_id, user_id, created_at, bot_db_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (full_code, bot_username, file_type, file_id, file_size, file_unique_id, user_id, now, bot_db_id)
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


def create_collection(code: str, bot_username: str, name: str, user_id: int,
                      bot_db_id: int = None) -> bool:
    """创建新集合。bot_db_id 用于区分同名 Bot 的数据。"""
    conn = get_db()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """INSERT INTO collections (code, bot_username, name, user_id, file_count, status, created_at, updated_at, bot_db_id)
               VALUES (?, ?, ?, ?, 0, 'open', ?, ?, ?)""",
            (code, bot_username, name, user_id, now, now, bot_db_id)
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
    conn = get_db()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # Reuse deleted record with same Telegram bot_id to keep bot_db_id consistent
        deleted_row = conn.execute(
            "SELECT id FROM user_bots WHERE bot_id = ? AND status = 'deleted' LIMIT 1",
            (bot_id,)
        ).fetchone()

        if deleted_row:
            old_id = deleted_row['id']
            conn.execute(
                """UPDATE user_bots
                   SET owner_id = ?, bot_token = ?, bot_username = ?, bot_firstname = ?,
                       status = 'active', updated_at = ?
                   WHERE id = ?""",
                (owner_id, bot_token, bot_username, bot_firstname, now, old_id)
            )
            conn.commit()
            logger.info("Reused deleted bot record (bot_db_id=%d, telegram_id=%d)", old_id, bot_id)
            return old_id

        cursor = conn.execute(
            """INSERT INTO user_bots (owner_id, bot_token, bot_id, bot_username, bot_firstname, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'active', ?, ?)""",
            (owner_id, bot_token, bot_id, bot_username, bot_firstname, now, now)
        )
        conn.commit()
        return cursor.lastrowid
    except sqlite3.IntegrityError:
        logger.error("Bot Token already exists")
        return None
    except Exception as e:
        logger.error("Failed to add user bot: %s", e)
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


def get_all_owner_ids() -> List[int]:
    """获取所有Bot所有者ID（去重）"""
    conn = get_db()
    try:
        rows = conn.execute("SELECT DISTINCT owner_id FROM user_bots WHERE status != 'deleted'").fetchall()
        return [r['owner_id'] for r in rows]
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


def update_user_bot_token(bot_db_id: int, new_token: str, new_bot_id: int = None) -> bool:
    """更新用户Bot的Token（用于Token失效后重新绑定）"""
    conn = get_db()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if new_bot_id is not None:
            conn.execute(
                "UPDATE user_bots SET bot_token = ?, bot_id = ?, status = 'active', updated_at = ? WHERE id = ?",
                (new_token, new_bot_id, now, bot_db_id)
            )
        else:
            conn.execute(
                "UPDATE user_bots SET bot_token = ?, status = 'active', updated_at = ? WHERE id = ?",
                (new_token, now, bot_db_id)
            )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        logger.error("更新Token失败：Token已存在")
        return False
    except Exception as e:
        logger.error("更新Bot Token失败: %s", e)
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
    """获取平台中每个 Bot 的详细信息（含文件数、集合数），使用 bot_db_id 区分同名 Bot"""
    conn = get_db()
    try:
        bots = conn.execute(
            "SELECT id, owner_id, bot_id, bot_username, bot_firstname, status, created_at FROM user_bots WHERE status != 'deleted' ORDER BY created_at"
        ).fetchall()
        result = []
        for bot in bots:
            bot_dict = dict(bot)
            bot_db_id = bot['id']
            # 优先用 bot_db_id 统计，回退到 bot_username
            file_count = conn.execute(
                "SELECT COUNT(*) as c FROM file_mappings WHERE bot_db_id = ?",
                (bot_db_id,)
            ).fetchone()['c']
            col_count = conn.execute(
                "SELECT COUNT(*) as c FROM collections WHERE bot_db_id = ?",
                (bot_db_id,)
            ).fetchone()['c']
            user_count = conn.execute(
                "SELECT COUNT(DISTINCT user_id) as c FROM file_mappings WHERE bot_db_id = ?",
                (bot_db_id,)
            ).fetchone()['c']
            # 如果 bot_db_id 统计为 0，回退到 bot_username（兼容旧数据）
            if file_count == 0:
                file_count = conn.execute(
                    "SELECT COUNT(*) as c FROM file_mappings WHERE bot_username = ? AND bot_db_id IS NULL",
                    (bot['bot_username'],)
                ).fetchone()['c']
            if col_count == 0:
                col_count = conn.execute(
                    "SELECT COUNT(*) as c FROM collections WHERE bot_username = ? AND bot_db_id IS NULL",
                    (bot['bot_username'],)
                ).fetchone()['c']
            if user_count == 0:
                user_count = conn.execute(
                    "SELECT COUNT(DISTINCT user_id) as c FROM file_mappings WHERE bot_username = ? AND bot_db_id IS NULL",
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
    """根据 bot_username 导出该 Bot 的所有文件代码（兼容旧调用）"""
    conn = get_db()
    try:
        # 优先用 bot_db_id 精确查询，避免同名 Bot 数据混淆
        rows = conn.execute(
            "SELECT code, file_type, file_size, user_id, created_at FROM file_mappings WHERE bot_username = ? ORDER BY created_at DESC",
            (bot_username,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_files_by_bot_db_id(bot_db_id: int) -> List[Dict]:
    """根据 bot_db_id 导出该 Bot 的所有文件代码（精确匹配，避免同名混淆）"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT code, file_type, file_size, user_id, created_at FROM file_mappings WHERE bot_db_id = ? ORDER BY created_at DESC",
            (bot_db_id,)
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


# ==================== Worker 节点管理 ====================

def register_worker_node(node_id: str, node_url: str, webhook_host: str = '',
                         max_bots: int = 100) -> bool:
    """注册或更新 Worker 节点"""
    conn = get_db()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """INSERT INTO worker_nodes (node_id, node_url, webhook_host, max_bots, status, last_heartbeat, created_at)
               VALUES (?, ?, ?, ?, 'online', ?, ?)
               ON CONFLICT(node_id) DO UPDATE SET
                   node_url = excluded.node_url,
                   webhook_host = excluded.webhook_host,
                   max_bots = excluded.max_bots,
                   status = 'online',
                   last_heartbeat = excluded.last_heartbeat""",
            (node_id, node_url, webhook_host, max_bots, now, now)
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error("注册 Worker 节点失败: %s", e)
        return False
    finally:
        conn.close()


def update_worker_heartbeat(node_id: str, current_bots: int) -> bool:
    """更新 Worker 节点心跳"""
    conn = get_db()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE worker_nodes SET status = 'online', current_bots = ?, last_heartbeat = ? WHERE node_id = ?",
            (current_bots, now, node_id)
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error("更新 Worker 心跳失败: %s", e)
        return False
    finally:
        conn.close()


def set_worker_offline(node_id: str) -> bool:
    """设置 Worker 节点为离线"""
    conn = get_db()
    try:
        conn.execute(
            "UPDATE worker_nodes SET status = 'offline' WHERE node_id = ?",
            (node_id,)
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error("设置 Worker 离线失败: %s", e)
        return False
    finally:
        conn.close()


def get_all_worker_nodes() -> List[Dict]:
    """获取所有 Worker 节点"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM worker_nodes ORDER BY node_id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_online_worker_nodes() -> List[Dict]:
    """获取所有在线的 Worker 节点"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM worker_nodes WHERE status = 'online' ORDER BY current_bots ASC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_best_worker_node() -> Optional[Dict]:
    """获取最佳 Worker 节点（在线且 Bot 数量最少的）"""
    conn = get_db()
    try:
        row = conn.execute(
            """SELECT * FROM worker_nodes
               WHERE status = 'online' AND current_bots < max_bots
               ORDER BY current_bots ASC
               LIMIT 1"""
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def update_user_bot_node(bot_db_id: int, node_id: str) -> bool:
    """更新用户 Bot 分配的节点"""
    conn = get_db()
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE user_bots SET node_id = ?, updated_at = ? WHERE id = ?",
            (node_id, now, bot_db_id)
        )
        conn.commit()
        return True
    except Exception as e:
        logger.error("更新 Bot 节点分配失败: %s", e)
        return False
    finally:
        conn.close()


def get_active_bots_by_node(node_id: str) -> List[Dict]:
    """获取指定节点上的所有活跃 Bot"""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT * FROM user_bots WHERE status = 'active' AND node_id = ? ORDER BY created_at",
            (node_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def remove_worker_node(node_id: str) -> bool:
    """删除 Worker 节点"""
    conn = get_db()
    try:
        conn.execute("DELETE FROM worker_nodes WHERE node_id = ?", (node_id,))
        conn.commit()
        return True
    except Exception as e:
        logger.error("删除 Worker 节点失败: %s", e)
        return False
    finally:
        conn.close()
