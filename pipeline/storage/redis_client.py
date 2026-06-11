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


def set_ex(key: str, value: Any, ttl: int = 60) -> bool:
    """
    Cache JSON-serialisable value with a TTL in seconds.
    Returns True if the write reached Redis, False if Redis is unavailable or the
    write errored. Callers that treat a write as primary (not best-effort cache)
    should check the return and surface a failure loudly.
    """
    if _CLIENT is None:
        return False
    try:
        _CLIENT.setex(key, ttl, json.dumps(value))
        return True
    except Exception as exc:
        logger.warning(f"Redis set({key!r}, ttl={ttl}) failed: {exc}")
        return False


def rpush_json_many(items: dict[str, Any], ttl: int) -> None:
    """
    RPUSH one JSON-serialised value per key (pipelined), setting the TTL on the
    first push only (list length 1 after push). Silent on error.
    """
    if _CLIENT is None or not items:
        return
    try:
        keys = list(items.keys())
        pipe = _CLIENT.pipeline(transaction=False)
        for k in keys:
            pipe.rpush(k, json.dumps(items[k]))
        lengths = pipe.execute()
        new_keys = [k for k, length in zip(keys, lengths) if length == 1]
        if new_keys:
            pipe = _CLIENT.pipeline(transaction=False)
            for k in new_keys:
                pipe.expire(k, ttl)
            pipe.execute()
    except Exception as exc:
        logger.debug(f"Redis rpush_json_many ({len(items)} keys) failed: {exc}")


def lrange_json(key: str) -> list:
    """Return the full list at key with each item JSON-decoded. [] on miss/error."""
    if _CLIENT is None:
        return []
    try:
        raw = _CLIENT.lrange(key, 0, -1)
        return [json.loads(item) for item in raw]
    except Exception as exc:
        logger.debug(f"Redis lrange_json({key!r}) failed: {exc}")
        return []


def hset_json(key: str, mapping: dict[str, Any], ttl: int | None = None) -> None:
    """HSET each field to its JSON-serialised value; optionally refresh the TTL."""
    if _CLIENT is None or not mapping:
        return
    try:
        _CLIENT.hset(key, mapping={f: json.dumps(v) for f, v in mapping.items()})
        if ttl is not None:
            _CLIENT.expire(key, ttl)
    except Exception as exc:
        logger.debug(f"Redis hset_json({key!r}) failed: {exc}")


def hgetall_json(key: str) -> dict[str, Any]:
    """Return the full hash at key with each value JSON-decoded. {} on miss/error."""
    if _CLIENT is None:
        return {}
    try:
        return {f: json.loads(v) for f, v in _CLIENT.hgetall(key).items()}
    except Exception as exc:
        logger.debug(f"Redis hgetall_json({key!r}) failed: {exc}")
        return {}


def delete_pattern(pattern: str) -> int:
    """Delete all keys matching a glob pattern via SCAN. Returns count deleted."""
    if _CLIENT is None:
        return 0
    try:
        deleted = 0
        batch: list[str] = []
        for k in _CLIENT.scan_iter(match=pattern, count=500):
            batch.append(k)
            if len(batch) >= 500:
                deleted += _CLIENT.delete(*batch)
                batch = []
        if batch:
            deleted += _CLIENT.delete(*batch)
        return deleted
    except Exception as exc:
        logger.debug(f"Redis delete_pattern({pattern!r}) failed: {exc}")
        return 0


# Edge-page data spine: durable per-frame transport to the persist worker.
# One stream entry == one full poll frame (~199 symbols) serialized as JSON.
# Capped at MAXLEN ~ 10000 (approximate trimming): one frame is ~40-60KB, so 10k
# entries bound the worst-case Redis footprint near ~500MB while still buffering
# ~20 trading sessions (~500 frames/session) against a dead persist worker.
# (MAXLEN 50000 would risk ~2-3GB and could OOM the VPS — do not raise this.)
_FRAMES_MAXLEN = 10_000


def xadd_frame(stream: str, frame: Any) -> bool:
    """
    Append one full radar frame to a capped Redis Stream for the persist worker.

    Fire-and-forget: returns True on success, False (logged) on any error or when
    Redis is unavailable. NEVER raises — the live poll loop must be unaffected if
    persistence transport fails (the live snapshot the UI reads is written separately).
    """
    if _CLIENT is None:
        return False
    try:
        _CLIENT.xadd(
            stream,
            {"frame": json.dumps(frame)},
            maxlen=_FRAMES_MAXLEN,
            approximate=True,
        )
        return True
    except Exception as exc:
        logger.warning(f"xadd_frame({stream!r}) failed (non-fatal, frame not persisted): {exc}")
        return False


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
