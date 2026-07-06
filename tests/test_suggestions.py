"""Suggestion diffing and persistence."""

from __future__ import annotations

import pandas as pd

from moi.report.suggestions import Action, diff_actions, store_suggestions


def test_diff_actions_all_cases() -> None:
    target = {"A": 0.08, "B": 0.08, "C": 0.08}
    current = {"B": 0.02, "C": 0.085, "D": 0.05}
    actions = {a.ticker: a.action for a in diff_actions(target, current)}
    assert actions == {"A": "BUY", "B": "ADD", "D": "SELL"}  # C within band → no action


def test_diff_orders_buys_and_sells_first() -> None:
    target = {"A": 0.08, "B": 0.10}
    current = {"B": 0.02, "Z": 0.05}
    kinds = [a.action for a in diff_actions(target, current)]
    assert kinds == ["BUY", "SELL", "ADD"]


def test_diff_empty_current_is_fresh_build() -> None:
    target = {"A": 0.05, "B": 0.05}
    actions = diff_actions(target, {})
    assert all(a.action == "BUY" for a in actions)


def test_store_suggestions(db) -> None:
    acts = [Action("ALAB", "BUY", 0.0, 0.083, score=0.5, thesis="t", bear_case="b")]
    n = store_suggestions(db, pd.Timestamp("2026-07-10"), acts)
    assert n == 1
    row = db.execute("SELECT ticker, action, status, thesis FROM suggestions").fetchone()
    assert row == ("ALAB", "BUY", "PENDING", "t")
