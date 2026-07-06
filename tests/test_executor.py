"""Executor safety rails — each one must refuse, loudly."""

from __future__ import annotations

from datetime import datetime

import pytest

from moi.execute.executor import (
    SafetyError,
    kill_switch_on,
    plan_order,
    set_kill_switch,
)
from moi.execute.queue import decide

NET_LIQ = 100_000.0


def _seed(db, *, ticker="ALAB", status="APPROVED", benchmark=False, price=100.0) -> str:
    db.execute(
        "INSERT OR REPLACE INTO universe (ticker, is_benchmark, active) VALUES (?, ?, TRUE)",
        [ticker, benchmark],
    )
    db.execute(
        "INSERT INTO prices_daily (ticker, date, close, source) VALUES (?, current_date, ?, 't')",
        [ticker, price],
    )
    sid = f"sug-{ticker}"
    db.execute(
        """INSERT INTO suggestions (id, created_at, week_end, ticker, action,
           current_weight, target_weight, status)
           VALUES (?, ?, '2026-07-10', ?, 'BUY', 0.0, 0.05, ?)""",
        [sid, datetime.now(), ticker, status],
    )
    return sid


def _plan(db, sid, **overrides):
    kwargs = dict(
        suggestion_id=sid,
        ticker="ALAB",
        action="BUY",
        current_weight=0.0,
        target_weight=0.05,
        net_liquidation=NET_LIQ,
        held_quantity=0.0,
    )
    kwargs.update(overrides)
    return plan_order(db, **kwargs)


def test_happy_path_sizes_correctly(db) -> None:
    sid = _seed(db)
    plan = _plan(db, sid)
    assert plan.side == "BUY"
    assert plan.quantity == 50  # 5% of 100k = $5,000 at $100
    assert plan.limit_price == pytest.approx(101.0)  # +1% slippage guard


def test_refuses_unapproved(db) -> None:
    sid = _seed(db, status="PENDING")
    with pytest.raises(SafetyError, match="not APPROVED"):
        _plan(db, sid)


def test_refuses_non_whitelisted_ticker(db) -> None:
    sid = _seed(db)  # approves ALAB
    with pytest.raises(SafetyError, match="whitelist"):
        _plan(db, sid, ticker="GME")  # not in universe at all


def test_refuses_benchmark_ticker(db) -> None:
    sid = _seed(db, ticker="SPY", benchmark=True)
    with pytest.raises(SafetyError, match="whitelist"):
        _plan(db, sid, ticker="SPY")


def test_refuses_oversized_order(db) -> None:
    sid = _seed(db)
    with pytest.raises(SafetyError, match="max_order_usd"):
        _plan(db, sid, target_weight=0.20)  # $20k > $8k default cap


def test_refuses_when_kill_switch_on(db) -> None:
    sid = _seed(db)
    set_kill_switch(db, True)
    assert kill_switch_on(db)
    with pytest.raises(SafetyError, match="kill switch"):
        _plan(db, sid)
    set_kill_switch(db, False)
    assert _plan(db, sid).quantity > 0  # recovers when switched off


def test_refuses_short_sell(db) -> None:
    sid = _seed(db)
    db.execute("UPDATE suggestions SET action = 'SELL' WHERE id = ?", [sid])
    with pytest.raises(SafetyError, match="short"):
        _plan(db, sid, action="SELL", current_weight=0.05, target_weight=0.0, held_quantity=0.0)


def test_sell_capped_at_held_quantity(db) -> None:
    sid = _seed(db)
    db.execute("UPDATE suggestions SET action = 'SELL' WHERE id = ?", [sid])
    plan = _plan(db, sid, action="SELL", current_weight=0.10, target_weight=0.0, held_quantity=30)
    assert plan.side == "SELL"
    assert plan.quantity == 30  # $10k delta would be 100 shares, capped at held 30


def test_refuses_daily_cap(db) -> None:
    sid = _seed(db)
    # Journal a fake prior order consuming most of the daily budget.
    db.execute(
        """INSERT INTO orders (order_id, suggestion_id, created_at, ticker, side, quantity,
           est_value, status) VALUES ('o1', 's1', ?, 'X', 'BUY', 1, 29000, 'submitted')""",
        [datetime.now()],
    )
    with pytest.raises(SafetyError, match="max_daily_usd"):
        _plan(db, sid)  # $5k more would exceed the $30k daily cap


def test_decide_transitions(db) -> None:
    sid = _seed(db, status="PENDING")
    assert decide(db, sid, "approved")
    assert (
        db.execute("SELECT status FROM suggestions WHERE id=?", [sid]).fetchone()[0] == "APPROVED"
    )
    assert not decide(db, sid, "REJECTED")  # already decided
    assert not decide(db, "missing", "APPROVED")
    with pytest.raises(ValueError, match="Invalid decision"):
        decide(db, sid, "EXECUTE")
