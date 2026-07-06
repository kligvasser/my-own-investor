"""Turn target-vs-current portfolio deltas into suggestion rows (the approval queue)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import duckdb
import pandas as pd

from moi.logging import get_logger
from moi.runlog import new_run_id

log = get_logger(__name__)

WEIGHT_BAND = 0.015  # ignore drifts smaller than 1.5% of the book


@dataclass
class Action:
    ticker: str
    action: str  # BUY | ADD | TRIM | SELL
    current_weight: float
    target_weight: float
    score: float | None = None
    thesis: str | None = None
    bear_case: str | None = None
    confidence: str = "moderate"


def diff_actions(
    target: dict[str, float], current: dict[str, float], band: float = WEIGHT_BAND
) -> list[Action]:
    """Compare target weights vs current weights (universe tickers only)."""
    actions: list[Action] = []
    for ticker, tw in target.items():
        cw = current.get(ticker, 0.0)
        if cw == 0.0 and tw > 0:
            actions.append(Action(ticker, "BUY", cw, tw))
        elif tw - cw > band:
            actions.append(Action(ticker, "ADD", cw, tw))
        elif cw - tw > band:
            actions.append(Action(ticker, "TRIM", cw, tw))
    for ticker, cw in current.items():
        if ticker not in target and cw > 0:
            actions.append(Action(ticker, "SELL", cw, 0.0))
    order = {"BUY": 0, "SELL": 1, "ADD": 2, "TRIM": 3}
    actions.sort(key=lambda a: (order[a.action], -a.target_weight))
    return actions


def current_universe_weights(con: duckdb.DuckDBPyConnection) -> dict[str, float] | None:
    """Current book weights for universe tickers from the live IBKR account.

    Returns None when TWS is unreachable (report then proposes a fresh build).
    Non-universe holdings are ignored — the system never advises outside its universe.
    """
    try:
        from moi.ingest.ibkr import ping

        info = ping()
    except Exception as exc:
        log.warning("positions_unavailable", error=str(exc)[:120])
        return None

    if not info.net_liquidation:
        return None
    universe = {t for (t,) in con.execute("SELECT ticker FROM universe WHERE active").fetchall()}
    now = datetime.now()
    weights: dict[str, float] = {}
    for symbol, qty, avg_cost in info.positions:
        row = con.execute(
            "SELECT close FROM prices_daily WHERE ticker = ? ORDER BY date DESC LIMIT 1",
            [symbol],
        ).fetchone()
        price = float(row[0]) if row else float(avg_cost)
        market_value = qty * price
        con.execute(
            """INSERT OR REPLACE INTO portfolio_snapshots
               (taken_at, account, ticker, quantity, avg_cost, market_value, net_liquidation)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            [now, info.account, symbol, qty, avg_cost, market_value, info.net_liquidation],
        )
        if symbol in universe:
            weights[symbol] = market_value / info.net_liquidation
    return weights


def store_suggestions(
    con: duckdb.DuckDBPyConnection, week_end: pd.Timestamp, actions: list[Action]
) -> int:
    for a in actions:
        con.execute(
            """INSERT INTO suggestions
               (id, created_at, week_end, ticker, action, current_weight, target_weight,
                score, thesis, bear_case, confidence, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')""",
            [
                new_run_id(),
                datetime.now(),
                pd.Timestamp(week_end).date(),
                a.ticker,
                a.action,
                a.current_weight,
                a.target_weight,
                a.score,
                a.thesis,
                a.bear_case,
                a.confidence,
            ],
        )
    return len(actions)
