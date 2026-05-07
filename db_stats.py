"""统计和导出相关数据库函数"""
import logging
from datetime import datetime
from typing import Dict, List

from db_core import get_db

logger = logging.getLogger(__name__)


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