import json
import logging
from functools import lru_cache
from uuid import UUID

import redis

from app.config import get_settings

logger = logging.getLogger(__name__)


class TaskCache:
    """Caches task list pages per user. Every method swallows Redis errors: the cache
    is an optimisation, so a Redis outage must never fail a request."""

    def __init__(self, client: redis.Redis | None, ttl_seconds: int):
        self.client = client
        self.ttl = ttl_seconds

    @property
    def enabled(self) -> bool:
        return self.client is not None

    def _version(self, user_id: UUID) -> str:
        return self.client.get(f"tasks:version:{user_id}") or "0"

    def _key(self, user_id: UUID, params: str) -> str:
        return f"tasks:list:{user_id}:v{self._version(user_id)}:{params}"

    def get_page(self, user_id: UUID, params: str) -> dict | None:
        if not self.client:
            return None
        try:
            raw = self.client.get(self._key(user_id, params))
            return json.loads(raw) if raw else None
        except redis.RedisError as exc:
            logger.warning("cache read failed: %s", exc)
            return None

    def set_page(self, user_id: UUID, params: str, page: dict) -> None:
        if not self.client:
            return
        try:
            self.client.setex(self._key(user_id, params), self.ttl, json.dumps(page))
        except redis.RedisError as exc:
            logger.warning("cache write failed: %s", exc)

    def invalidate_user(self, user_id: UUID) -> None:
        # Bumping a version number makes every cached page for the user unreachable
        # in one O(1) call; the old keys simply expire via their TTL.
        if not self.client:
            return
        try:
            self.client.incr(f"tasks:version:{user_id}")
        except redis.RedisError as exc:
            logger.warning("cache invalidation failed: %s", exc)

    def ping(self) -> bool:
        if not self.client:
            return False
        try:
            return bool(self.client.ping())
        except redis.RedisError:
            return False


@lru_cache
def get_cache() -> TaskCache:
    settings = get_settings()
    client = None
    if settings.redis_url:
        client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=0.3,
            socket_timeout=0.3,
        )
    return TaskCache(client, settings.task_cache_ttl_seconds)
