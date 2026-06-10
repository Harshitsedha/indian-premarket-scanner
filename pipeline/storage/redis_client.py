"""
Thin Redis wrapper for API-level caching.
Falls back silently when Redis is unavailable — callers never crash on cache misses.
"""
from __future__ import annotations

import json
from typing import Any

from loguru import logger

try:
    import redis as _redis_lib
    from utils.config import settings as _settings
    _CLIENT = _redis_lib.Redis(
        host=_settings.redis_host,
        port=_settings.redis_port,
        decode_responses=True,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    _CLIENT.ping()   # fail fast at import if Redis is down
    logger.debug(f"Redis connected: {_settings.redis_host}:{_settings.redis_port}")
except Exception as _exc:
    logger.warning(f"Redis unavailable ({_exc}) — caching disabled")
    _CLIENT = None   # type: ignore[assignment]


def get(key: str) -> Any | None:
    """Return cached value or None on miss / error."""
    if _CLIENT is None:
        return None
    try:
        raw = _CLIENT.get(key)
        return json.loads(raw) if raw is not None else None
    except Exception as exc:
        logger.debug(f"Redis get({key!r}) failed: {exc}")
        return None


def set_ex(key: str, value: Any, ttl: int = 60) -> None:
    """Cache JSON-serialisable value with a TTL in seconds. Silent on error."""
    if _CLIENT is None:
        return
    try:
        _CLIENT.setex(key, ttl, json.dumps(value))
    except Exception as exc:
        logger.debug(f"Redis set({key!r}, ttl={ttl}) failed: {exc}")


def setnx_ex(key: str, ttl: int) -> bool:
    """
    Set key with TTL only if it does not already exist.
    Returns True if the key was newly set (first caller wins), False if it already existed.
    On Redis unavailability or error, returns True so alerts are not silently suppressed.
    """
    if _CLIENT is None:
        return True   # no Redis → cannot dedup; allow fire
    try:
        result = _CLIENT.set(key, 1, ex=ttl, nx=True)
        return result is not None   # None → key already existed
    except Exception as exc:
        logger.debug(f"Redis setnx_ex({key!r}) failed: {exc}")
        return True   # on error, allow alert to fire
