"""集合操作相关数据库函数"""
import logging
from datetime import datetime
from typing import Optional, List, Dict

from db_core import get_db

logger = logging.getLogger(__name__)


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


def get_collection_by_id(col_id: int) -> Optional[Dict]:
    """通过数据库ID获取集合信息"""
    conn = get_db()
    try:
        row = conn.execute("SELECT * FROM collections WHERE id = ?", (col_id,)).fetchone()
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