"""
pipeline/backtest/loader.py — Dynamic strategy loader.

Fetches validated strategy code from backtest_strategies, execs it in an
isolated namespace with pre-seeded types (Action, Signal, BarContext, pd, np,
math, deque), validates Protocol compliance, and returns the class ready to
instantiate. Caches loaded classes in-process by (name, updated_at) so
repeated jobs in the same worker process never re-exec the same code.

The engine receives class instances, not this module — it remains unchanged.
"""
from __future__ import annotations

import math
import sys
from collections import deque
from pathlib import Path
from typing import Any

_PIPELINE = Path(__file__).resolve().parents[1]
if str(_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_PIPELINE))

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras
from loguru import logger

from backtest.strategy import Action, BarContext, Signal
from utils.config import settings


# ── In-process cache: (name, updated_at) → class ─────────────────────────────
_CACHE: dict[tuple[str, Any], type] = {}

# Names pre-seeded into every strategy exec namespace.
# Strategies must NOT import anything — everything they need is here.
_BASE_GLOBALS: dict[str, Any] = {
    "__builtins__": __builtins__,
    "Action":    Action,
    "BarContext": BarContext,
    "Signal":    Signal,
    "pd":        pd,
    "np":        np,
    "math":      math,
    "deque":     deque,
}


def _db():
    return psycopg2.connect(
        host     = settings.postgres_host,
        port     = settings.postgres_port,
        dbname   = settings.postgres_db,
        user     = settings.postgres_user,
        password = settings.postgres_password,
    )


def load_strategy(name: str) -> type:
    """
    Return a strategy class by DB name.

    Looks up backtest_strategies WHERE name=%s AND validated=true.
    Execs the code in an isolated namespace, extracts the new class, checks
    Protocol compliance, caches by (name, updated_at), and returns the class.
    Caller instantiates it with cls(**(strategy_params or {})).

    Raises:
        ValueError — strategy not found, not validated, code defines no class,
                     or class is missing required methods.
    """
    conn = _db()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT code, validated, updated_at
                  FROM backtest_strategies
                 WHERE name = %s AND deleted_at IS NULL
                """,
                (name,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        raise ValueError(f"Strategy {name!r} not found in the strategy registry")
    if not row["validated"]:
        raise ValueError(
            f"Strategy {name!r} exists but has not been validated — "
            "validate it via the Strategy Manager before running jobs"
        )

    cache_key = (name, row["updated_at"])
    if cache_key in _CACHE:
        logger.debug(f"load_strategy: cache hit {name!r} @ {row['updated_at']}")
        return _CACHE[cache_key]

    # ── Exec in isolated namespace ─────────────────────────────────────────────
    namespace: dict[str, Any] = {**_BASE_GLOBALS}
    pre_keys = set(namespace.keys())

    try:
        exec(row["code"], namespace)  # noqa: S102  — only runs validated code
    except Exception as exc:
        raise ValueError(f"Strategy {name!r}: exec failed — {exc}") from exc

    # ── Find the class the code defines (anything new that is a type) ──────────
    new_classes = [
        v for k, v in namespace.items()
        if k not in pre_keys and isinstance(v, type)
    ]
    if not new_classes:
        raise ValueError(f"Strategy {name!r}: no class found in code after exec")
    if len(new_classes) > 1:
        logger.warning(
            f"Strategy {name!r}: {len(new_classes)} classes found "
            f"({[c.__name__ for c in new_classes]}), using first"
        )
    cls = new_classes[0]

    # ── Validate Protocol compliance at runtime ───────────────────────────────
    missing = [
        m for m in ("reset_day", "on_bar")
        if not callable(getattr(cls, m, None))
    ]
    if missing:
        raise ValueError(
            f"Strategy {name!r} class {cls.__name__!r} is missing: {missing}. "
            "Must implement reset_day(self) and on_bar(self, ctx)."
        )

    _CACHE[cache_key] = cls
    logger.info(f"load_strategy: loaded + cached {name!r} → {cls.__name__}")
    return cls
