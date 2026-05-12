"""
Redis 管理器
可选组件：未配置 Redis 时自动降级为内存实现，不影响正常运行

功能：
- 缓存（VIP 状态、集合信息）
- 用户限流（滑动窗口，排队模式）
- 发送队列持久化
- Bot 状态共享
- 文件发送计数
"""
import asyncio
import json
import logging
import time
from typing import Optional, Any, Dict, List

from config import REDIS_URL

logger = logging.getLogger(__name__)

# 全局实例
_instance: Optional['RedisManager'] = None


class RedisManager:
    """Redis 管理器，支持优雅降级"""

    def __init__(self):
        self._redis = None
        self._available = False
        # 内存降级方案
        self._memory_cache: Dict[str, tuple] = {}  # key -> (value, expire_at)
        self._rate_windows: Dict[str, list] = {}  # key -> [timestamp, ...]
        self._counters: Dict[str, int] = {}

    async def init(self):
        """初始化 Redis 连接"""
        if not REDIS_URL:
            logger.info("📦 未配置 REDIS_URL，使用内存降级方案")
            self._available = False
            return

        try:
            import redis.asyncio as aioredis
            self._redis = aioredis.from_url(
                REDIS_URL,
                decode_responses=True,
                socket_timeout=5,
                socket_connect_timeout=3,
                retry_on_timeout=True,
            )
            await self._redis.ping()
            self._available = True
            logger.info("✅ Redis 连接成功")
        except ImportError:
            logger.warning("📦 未安装 redis 包，使用内存降级方案（pip install redis）")
            self._available = False
        except Exception as e:
            logger.warning("📦 Redis 连接失败: %s，使用内存降级方案", e)
            self._available = False

    async def close(self):
        """关闭连接"""
        if self._redis:
            await self._redis.close()
            self._redis = None
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    # ==================== 缓存 ====================

    async def cache_get(self, key: str) -> Optional[str]:
        """获取缓存"""
        if self._available:
            try:
                return await self._redis.get(key)
            except Exception as e:
                logger.warning("Redis cache_get 失败: %s", e)
                return None
        else:
            # 内存降级
            item = self._memory_cache.get(key)
            if item:
                value, expire_at = item
                if expire_at and time.time() > expire_at:
                    del self._memory_cache[key]
                    return None
                return value
            return None

    async def cache_set(self, key: str, value: str, ttl: int = 300):
        """设置缓存，ttl 秒"""
        if self._available:
            try:
                await self._redis.setex(key, ttl, value)
            except Exception as e:
                logger.warning("Redis cache_set 失败: %s", e)
        else:
            self._memory_cache[key] = (value, time.time() + ttl)

    async def cache_delete(self, key: str):
        """删除缓存"""
        if self._available:
            try:
                await self._redis.delete(key)
            except Exception as e:
                logger.warning("Redis cache_delete 失败: %s", e)
        else:
            self._memory_cache.pop(key, None)

    async def cache_get_json(self, key: str) -> Optional[Any]:
        """获取 JSON 缓存"""
        raw = await self.cache_get(key)
        if raw:
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                return None
        return None

    async def cache_set_json(self, key: str, value: Any, ttl: int = 300):
        """设置 JSON 缓存"""
        await self.cache_set(key, json.dumps(value, ensure_ascii=False, default=str), ttl)

    # ==================== 用户限流（排队模式） ====================

    async def rate_limit_check(self, key: str, limit: int, window: int) -> tuple:
        """滑动窗口限流检查

        Args:
            key: 限流 key（如 rate:user:12345）
            limit: 窗口内最大请求数
            window: 窗口大小（秒）

        Returns:
            (allowed: bool, current: int, retry_after: float)
            retry_after: 需要等待的秒数（0 表示不需要等待）
        """
        now = time.time()
        window_start = now - window

        if self._available:
            try:
                pipe = self._redis.pipeline(transaction=True)
                pipe.zremrangebyscore(key, 0, window_start)
                pipe.zcard(key)
                pipe.zadd(key, {str(now): now})
                pipe.expire(key, window + 1)
                results = await pipe.execute()
                current = results[1]

                if current >= limit:
                    # 获取最早的请求时间，计算等待时间
                    earliest = await self._redis.zrange(key, 0, 0, withscores=True)
                    if earliest:
                        wait = earliest[0][1] + window - now
                        return False, current, max(0, wait)
                    return False, current, window
                return True, current + 1, 0
            except Exception as e:
                logger.warning("Redis rate_limit 失败: %s", e)
                return True, 0, 0  # 降级时放行
        else:
            # 内存降级
            timestamps = self._rate_windows.get(key, [])
            # 清理过期
            timestamps = [t for t in timestamps if t > window_start]
            timestamps.append(now)
            self._rate_windows[key] = timestamps

            current = len(timestamps)
            if current > limit:
                wait = timestamps[0] + window - now
                return False, current, max(0, wait)
            return True, current, 0

    async def rate_limit_wait(self, key: str, limit: int, window: int, max_wait: float = 30.0) -> bool:
        """限流等待（排队模式）

        如果超限，等待到可以执行。
        Returns: True 可以执行, False 等待超时
        """
        while True:
            allowed, _, retry_after = await self.rate_limit_check(key, limit, window)
            if allowed:
                return True
            if retry_after > max_wait:
                return False
            await asyncio.sleep(min(retry_after + 0.1, 1.0))

    # ==================== 计数器 ====================

    async def counter_incr(self, key: str, ttl: int = 86400) -> int:
        """递增计数器，返回递增后的值"""
        if self._available:
            try:
                pipe = self._redis.pipeline(transaction=True)
                pipe.incr(key)
                pipe.expire(key, ttl)
                result = await pipe.execute()
                return result[0]
            except Exception as e:
                logger.warning("Redis counter_incr 失败: %s", e)
                return 0
        else:
            self._counters[key] = self._counters.get(key, 0) + 1
            return self._counters[key]

    async def counter_get(self, key: str) -> int:
        """获取计数器值"""
        if self._available:
            try:
                val = await self._redis.get(key)
                return int(val) if val else 0
            except Exception as e:
                logger.warning("Redis counter_get 失败: %s", e)
                return 0
        else:
            return self._counters.get(key, 0)

    # ==================== 发送队列持久化 ====================

    async def queue_push(self, queue_key: str, data: str):
        """推送到队列"""
        if self._available:
            try:
                await self._redis.rpush(queue_key, data)
            except Exception as e:
                logger.warning("Redis queue_push 失败: %s", e)

    async def queue_pop(self, queue_key: str) -> Optional[str]:
        """从队列弹出"""
        if self._available:
            try:
                return await self._redis.lpop(queue_key)
            except Exception as e:
                logger.warning("Redis queue_pop 失败: %s", e)
                return None
        return None

    async def queue_len(self, queue_key: str) -> int:
        """队列长度"""
        if self._available:
            try:
                return await self._redis.llen(queue_key)
            except Exception as e:
                logger.warning("Redis queue_len 失败: %s", e)
                return 0
        return 0

    # ==================== Bot 状态 ====================

    async def set_bot_status(self, bot_db_id: int, status: dict, ttl: int = 120):
        """设置 Bot 状态（心跳式，自动过期）"""
        await self.cache_set_json(f"bot_status:{bot_db_id}", status, ttl)

    async def get_bot_status(self, bot_db_id: int) -> Optional[dict]:
        """获取 Bot 状态"""
        return await self.cache_get_json(f"bot_status:{bot_db_id}")


async def get_redis() -> RedisManager:
    """获取 RedisManager 全局实例"""
    global _instance
    if _instance is None:
        _instance = RedisManager()
        await _instance.init()
    return _instance


async def close_redis():
    """关闭 Redis"""
    global _instance
    if _instance:
        await _instance.close()
        _instance = None