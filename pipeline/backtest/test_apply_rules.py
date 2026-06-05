"""
Tests for rules.apply_rules() — pure function, no I/O.

Verifies:
  - All filters passing → True
  - One filter failing → False
  - None feature value → False (conservative)
  - not_in on day_of_week → works correctly
  - between, lt, gt, lte, gte, in → all supported ops
"""
import pytest
from backtest.rules import apply_rules

_RULES_ALL_PASS = {
    "filters": [
        {"feature": "gap_pct",     "op": "between", "low": 1.0,  "high": 5.0},
        {"feature": "or_range_pct","op": "lt",      "value": 3.0},
        {"feature": "vol_ratio",   "op": "gte",     "value": 0.5},
        {"feature": "day_of_week", "op": "not_in",  "values": [4]},  # not Friday
    ]
}

_ROW_GOOD = {
    "gap_pct":      2.5,
    "or_range_pct": 1.2,
    "vol_ratio":    0.8,
    "day_of_week":  1,       # Tuesday
}


def test_all_filters_pass():
    assert apply_rules(_ROW_GOOD, _RULES_ALL_PASS) is True


def test_one_filter_fails_between():
    row = {**_ROW_GOOD, "gap_pct": 0.5}   # gap too small
    assert apply_rules(row, _RULES_ALL_PASS) is False


def test_one_filter_fails_lt():
    row = {**_ROW_GOOD, "or_range_pct": 5.0}   # too wide
    assert apply_rules(row, _RULES_ALL_PASS) is False


def test_none_feature_value_returns_false():
    row = {**_ROW_GOOD, "vol_ratio": None}
    assert apply_rules(row, _RULES_ALL_PASS) is False


def test_not_in_day_of_week():
    friday = {**_ROW_GOOD, "day_of_week": 4}    # Friday → rejected
    tuesday = {**_ROW_GOOD, "day_of_week": 1}   # Tuesday → accepted
    assert apply_rules(friday, _RULES_ALL_PASS)  is False
    assert apply_rules(tuesday, _RULES_ALL_PASS) is True


def test_gt_op():
    rules = {"filters": [{"feature": "gap_pct", "op": "gt", "value": 1.0}]}
    assert apply_rules({"gap_pct": 1.5}, rules) is True
    assert apply_rules({"gap_pct": 0.9}, rules) is False
    assert apply_rules({"gap_pct": 1.0}, rules) is False    # strict


def test_lte_op():
    rules = {"filters": [{"feature": "gap_pct", "op": "lte", "value": 2.0}]}
    assert apply_rules({"gap_pct": 2.0}, rules) is True    # inclusive
    assert apply_rules({"gap_pct": 2.1}, rules) is False


def test_in_op():
    rules = {"filters": [{"feature": "day_of_week", "op": "in", "values": [0, 1, 2]}]}
    assert apply_rules({"day_of_week": 1}, rules) is True
    assert apply_rules({"day_of_week": 4}, rules) is False


def test_missing_feature_key_returns_false():
    row   = {"gap_pct": 2.0}    # or_range_pct is absent → treated as None
    rules = {"filters": [{"feature": "or_range_pct", "op": "lt", "value": 3.0}]}
    assert apply_rules(row, rules) is False


def test_no_filters_returns_true():
    assert apply_rules(_ROW_GOOD, {"filters": []}) is True


def test_unknown_op_returns_false():
    rules = {"filters": [{"feature": "gap_pct", "op": "near", "value": 2.0}]}
    assert apply_rules({"gap_pct": 2.0}, rules) is False
