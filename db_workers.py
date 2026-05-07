"""Worker 节点管理相关数据库函数"""
import logging
from datetime import datetime
from typing import Optional, List, Dict

from db_core import get_db

logger = logging.getLogger(__name__)


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