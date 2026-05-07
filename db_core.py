"""数据库核心操作 - 连接管理、表创建与迁移"""
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
    """回填 bot_db_id：将 NULL 的数据关联到正确的 Bot 记录"""
    # 检查是否需要回填
    try:
        null_files = conn.execute(
            "SELECT COUNT(*) as c FROM file_mappings WHERE bot_db_id IS NULL"
        ).fetchone()['c']
        null_cols = conn.execute(
            "SELECT COUNT(*) as c FROM collections WHERE bot_db_id IS NULL"
        ).fetchone()['c']
    except Exception as e:
        logger.error("检查 NULL bot_db_id 失败: %s", e)
        return

    if null_files == 0 and null_cols == 0:
        logger.info("无需回填 bot_db_id")
        return

    logger.info("开始回填 bot_db_id: %d 个文件, %d 个集合", null_files, null_cols)

    # 获取所有 Bot 记录（包含 deleted/revoked），按 bot_username + created_at 排序
    try:
        all_bots = conn.execute(
            "SELECT id, bot_username, created_at FROM user_bots ORDER BY bot_username, created_at"
        ).fetchall()
    except Exception as e:
        logger.error("获取 Bot 记录失败: %s", e)
        return

    # 按 bot_username 分组
    bots_by_username = {}
    for bot in all_bots:
        uname = bot['bot_username']
        if uname not in bots_by_username:
            bots_by_username[uname] = []
        bots_by_username[uname].append({
            'id': bot['id'],
            'created_at': bot['created_at'] or '',
        })

    logger.info("找到 %d 个不同 bot_username 的 Bot 记录", len(bots_by_username))

    updated_files = 0
    updated_cols = 0

    for username, bots in bots_by_username.items():
        if len(bots) == 1:
            # 只有一个同名 Bot，直接全部关联
            bot_id = bots[0]['id']
            r1 = conn.execute(
                "UPDATE file_mappings SET bot_db_id = ? WHERE bot_username = ? AND bot_db_id IS NULL",
                (bot_id, username)
            ).rowcount
            r2 = conn.execute(
                "UPDATE collections SET bot_db_id = ? WHERE bot_username = ? AND bot_db_id IS NULL",
                (bot_id, username)
            ).rowcount
            updated_files += r1
            updated_cols += r2
            if r1 > 0 or r2 > 0:
                logger.info("  %s: 单 Bot，关联 %d 文件, %d 集合", username, r1, r2)
            continue

        # 多个同名 Bot：按时间区间分配
        for file_table in ['file_mappings', 'collections']:
            rows = conn.execute(
                f"SELECT id, created_at FROM {file_table} WHERE bot_username = ? AND bot_db_id IS NULL",
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
                        f"UPDATE {file_table} SET bot_db_id = ? WHERE id = ?",
                        (matched_bot_id, row['id'])
                    )
                    if file_table == 'file_mappings':
                        updated_files += 1
                    else:
                        updated_cols += 1

        logger.info("  %s: %d 个同名 Bot，已分配", username, len(bots))

    # 检查是否还有残留 NULL
    remain_files = conn.execute(
        "SELECT COUNT(*) as c FROM file_mappings WHERE bot_db_id IS NULL"
    ).fetchone()['c']
    remain_cols = conn.execute(
        "SELECT COUNT(*) as c FROM collections WHERE bot_db_id IS NULL"
    ).fetchone()['c']

    logger.info("回填完成: %d 文件, %d 集合已关联。剩余 NULL: %d 文件, %d 集合",
                updated_files, updated_cols, remain_files, remain_cols)

    # 残留 NULL 的原因：bot_username 在 user_bots 中不存在（被硬删除）
    if remain_files > 0 or remain_cols > 0:
        orphan_usernames = conn.execute(
            "SELECT DISTINCT bot_username FROM file_mappings WHERE bot_db_id IS NULL"
        ).fetchall()
        for row in orphan_usernames:
            logger.warning("  未匹配的 bot_username: %s（Bot 记录可能已被硬删除）", row['bot_username'])


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
        _backfill_bot_db_id(conn)

        conn.commit()
        logger.info("数据库初始化完成")
    finally:
        conn.close()