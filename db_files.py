"""文件操作相关数据库函数"""
import sqlite3
import logging
from datetime import datetime
from typing import Optional, List, Dict

from db_core import get_db

logger = logging.getLogger(__name__)


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
                    "SELECT code FROM file_mappings WHERE file_unique_id = ? AND (bot_db_id = ? OR bot_db_id IS NULL)",
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


def get_files_by_bot_username(bot_username: str) -> List[Dict]:
    """根据 bot_username 导出该 Bot 的所有文件代码（兼容旧调用）"""
    conn = get_db()
    try:
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