"""
Shared exception types for the backtest execution pipeline.

Defined here (not in worker.py) so data.py, run.py, and train_test.py can
raise them without a circular dependency on the worker module.
"""
from __future__ import annotations


class _JobCancelled(Exception):
    """
    Raised at a safe execution checkpoint when cancel_event.is_set() is True.
    Always raised before any result CSV is written so no partial output is left.
    """
