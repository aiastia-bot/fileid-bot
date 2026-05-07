"""用户Bot管理相关数据库函数"""
import sqlite3
import logging
from datetime import datetime
from typing import Optional, List, Dict

from db_core import get_db

logger = logging.getLogger(__name__)


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
                       status = 'active', updated_at = ?, node_id = 'local'
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