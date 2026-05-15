"""Worker 节点管理相关数据库函数（Async SQLAlchemy）"""
import logging
from datetime import datetime
from typing import Optional, List, Dict

from sqlalchemy import select, update

from db.core import get_session, _model_to_dict
from db.models import WorkerNode

logger = logging.getLogger(__name__)


async def register_worker_node(node_id: str, node_url: str, webhook_host: str, max_bots: int) -> bool:
    """注册或更新 Worker 节点（INSERT OR REPLACE 语义）"""
    async with get_session() as session:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            result = await session.execute(
                select(WorkerNode).where(WorkerNode.node_id == node_id)
            )
            existing = result.scalar_one_or_none()
            if existing:
                existing.node_url = node_url
                existing.webhook_host = webhook_host
                existing.max_bots = max_bots
                existing.status = 'online'
                existing.last_heartbeat = now
            else:
                session.add(WorkerNode(
                    node_id=node_id, node_url=node_url, webhook_host=webhook_host,
                    max_bots=max_bots, current_bots=0, status='online',
                    last_heartbeat=now, created_at=now
                ))
            await session.commit()
            return True
        except Exception as e:
            logger.error("注册 Worker 节点失败: %s", e)
            return False


async def update_worker_heartbeat(node_id: str, active_bots: int) -> bool:
    """更新 Worker 节点心跳"""
    async with get_session() as session:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await session.execute(
                update(WorkerNode)
                .where(WorkerNode.node_id == node_id)
                .values(status='online', current_bots=active_bots, last_heartbeat=now)
            )
            await session.commit()
            return True
        except Exception as e:
            logger.error("更新 Worker 心跳失败: %s", e)
            return False


async def set_worker_offline(node_id: str) -> bool:
    """将 Worker 节点设为离线"""
    async with get_session() as session:
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            await session.execute(
                update(WorkerNode)
                .where(WorkerNode.node_id == node_id)
                .values(status='offline', current_bots=0, last_heartbeat=now)
            )
            await session.commit()
            return True
        except Exception as e:
            logger.error("设置 Worker 离线失败: %s", e)
            return False


async def get_all_worker_nodes() -> List[Dict]:
    """获取所有 Worker 节点"""
    async with get_session() as session:
        result = await session.execute(
            select(WorkerNode).order_by(WorkerNode.created_at)
        )
        nodes = result.scalars().all()
        return [_model_to_dict(n) for n in nodes]


async def get_online_worker_nodes() -> List[Dict]:
    """获取所有在线 Worker 节点"""
    async with get_session() as session:
        result = await session.execute(
            select(WorkerNode).where(WorkerNode.status == 'online').order_by(WorkerNode.created_at)
        )
        nodes = result.scalars().all()
        return [_model_to_dict(n) for n in nodes]


async def get_best_worker_node() -> Optional[Dict]:
    """获取最空闲的 Worker 节点（在线且 current_bots < max_bots，按负载排序）"""
    async with get_session() as session:
        result = await session.execute(
            select(WorkerNode)
            .where(WorkerNode.status == 'online')
            .order_by(WorkerNode.current_bots)
            .limit(1)
        )
        node = result.scalar_one_or_none()
        if node and node.current_bots < node.max_bots:
            return _model_to_dict(node)
        return None


async def remove_worker_node(node_id: str) -> bool:
    """移除 Worker 节点"""
    async with get_session() as session:
        try:
            result = await session.execute(
                select(WorkerNode).where(WorkerNode.node_id == node_id)
            )
            node = result.scalar_one_or_none()
            if node:
                await session.delete(node)
                await session.commit()
            return True
        except Exception as e:
            logger.error("移除 Worker 节点失败: %s", e)
            return False