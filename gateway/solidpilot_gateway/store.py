"""Redis-backed shared state. Every serverless instance sees the same tokens, codes and lockouts.

Tokens and codes are stored only as SHA-256 digests of the secret value.
"""

import hashlib
import json
from typing import Any

import redis.asyncio as aioredis


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def connect(url: str) -> "aioredis.Redis":
    # BLPOP is held open for up to poll_wait (25 s) / SLICE (10 s), so the socket read timeout must exceed that.
    # redis-py 8 defaults socket_timeout to 5 s, which would abort every blocking pop; set it explicitly.
    # Upstash needs TLS (rediss://).
    return aioredis.from_url(url, decode_responses=True, socket_connect_timeout=5, socket_timeout=40,
                             socket_keepalive=True, health_check_interval=30)


class Store:
    def __init__(self, redis: "aioredis.Redis"):
        self.redis = redis

    @staticmethod
    def _k(table: str, key: str) -> str:
        return f"sp:{table}:{key}"

    async def put(self, table: str, key: str, value: dict[str, Any], ttl: float | None = None) -> None:
        await self.redis.set(self._k(table, key), json.dumps(value), ex=int(ttl) if ttl else None)

    async def get(self, table: str, key: str) -> dict[str, Any] | None:
        raw = await self.redis.get(self._k(table, key))
        return json.loads(raw) if raw else None

    async def pop(self, table: str, key: str) -> dict[str, Any] | None:
        """Atomic get-and-delete: a code or refresh token can be redeemed exactly once, even across instances."""
        raw = await self.redis.getdel(self._k(table, key))
        return json.loads(raw) if raw else None

    async def delete(self, table: str, key: str) -> None:
        await self.redis.delete(self._k(table, key))

    # ----- failure counters / lockouts (shared, so scaling out does not reset the brute-force limit) ------
    async def bump(self, name: str, window: int) -> int:
        key = self._k("count", name)
        n = await self.redis.incr(key)
        if n == 1:
            await self.redis.expire(key, window)
        return int(n)

    async def clear(self, name: str) -> None:
        await self.redis.delete(self._k("count", name))

    async def lock(self, name: str, seconds: int) -> None:
        await self.redis.set(self._k("lock", name), "1", ex=seconds)

    async def is_locked(self, name: str) -> bool:
        return bool(await self.redis.exists(self._k("lock", name)))
