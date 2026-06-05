"""
Rule applicator — pure function, no I/O, no engine imports.

apply_rules(candidate_row, rules_parsed) -> bool

Evaluates every filter in rules_parsed["filters"] against one candidate row's
feature values. All filters must pass (AND logic). A filter referencing a
feature whose value is None returns False — conservative, don't take a trade
you can't evaluate.

Supported ops: between, lt, gt, lte, gte, in, not_in
"""
from __future__ import annotations


def apply_rules(candidate_row: dict, rules_parsed: dict) -> bool:
    """
    Return True iff all filters in rules_parsed pass for this candidate row.

    Args:
        candidate_row: dict with feature column values (float | None allowed).
        rules_parsed:  validated rule dict with a "filters" list.

    Returns:
        True if every filter passes, False otherwise.
    """
    for filt in rules_parsed.get("filters", []):
        feature = filt["feature"]
        op      = filt["op"]

        raw = candidate_row.get(feature)
        if raw is None:
            return False                        # cannot evaluate → conservative reject
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return False

        if op == "lt":
            if not (value < filt["value"]):
                return False
        elif op == "gt":
            if not (value > filt["value"]):
                return False
        elif op == "lte":
            if not (value <= filt["value"]):
                return False
        elif op == "gte":
            if not (value >= filt["value"]):
                return False
        elif op == "between":
            if not (filt["low"] <= value <= filt["high"]):
                return False
        elif op == "in":
            if value not in [float(v) for v in filt["values"]]:
                return False
        elif op == "not_in":
            if value in [float(v) for v in filt["values"]]:
                return False
        else:
            return False                        # unknown op → conservative reject

    return True
