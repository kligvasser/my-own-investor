"""Holdings performance analytics."""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from moi.report.performance import (
    holdings_view,
    normalized_window,
    period_return,
)


def _seed(db) -> None:
    dates = pd.bdate_range("2025-01-01", periods=300)
    n = len(dates)
    for ticker, start, end in (("AAA", 100, 200), ("BBB", 100, 80), ("SPY", 100, 120)):
        px = np.linspace(start, end, n)
        db.executemany(
            "INSERT INTO prices_daily (ticker, date, close, adj_close, source) "
            "VALUES (?, ?, ?, ?, 't')",
            [(ticker, d.date(), float(p), float(p)) for d, p in zip(dates, px, strict=True)],
        )
    now = datetime(2026, 3, 1, 12, 0)
    for ticker, qty, cost in (("AAA", 10, 150.0), ("BBB", 20, 100.0)):
        db.execute(
            """INSERT INTO portfolio_snapshots
               (taken_at, account, ticker, quantity, avg_cost, market_value, net_liquidation)
               VALUES (?, 'U1', ?, ?, ?, 0, 10000)""",
            [now, ticker, qty, cost],
        )


def test_period_return() -> None:
    s = pd.Series(np.linspace(100, 110, 50))
    r = period_return(s, 5)
    assert r is not None and r > 0
    assert period_return(s, 100) is None  # not enough history


def test_holdings_view(db) -> None:
    _seed(db)
    view = holdings_view(db)
    assert view is not None
    t = view.table.set_index("ticker")
    # AAA rose to 200 with cost 150 → positive P&L; BBB fell to 80 with cost 100 → negative.
    assert t.loc["AAA", "pnl"] > 0 > t.loc["BBB", "pnl"]
    assert abs(t["weight"].sum() - 1.0) < 1e-9
    # Portfolio 1M return is value-weighted between a riser and a faller.
    assert "1M" in view.portfolio_returns
    assert view.benchmark_returns["1M"] > 0  # SPY trends up
    assert view.net_liquidation == 10000


def test_holdings_view_empty(db) -> None:
    assert holdings_view(db) is None


def test_normalized_window() -> None:
    idx = pd.bdate_range("2026-01-01", periods=40)
    closes = pd.DataFrame({"A": np.linspace(50, 100, 40)}, index=idx)
    norm = normalized_window(closes, 20)
    assert len(norm) == 20
    assert abs(norm["A"].iloc[0] - 100.0) < 1e-9
    assert norm["A"].iloc[-1] > 100.0
