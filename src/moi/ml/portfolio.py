"""Portfolio constructor v1: top-N composite scores with sector caps and regime scaling.

Deterministic and constraint-first (no optimizer yet — skfolio mean-variance is a later
refinement). Constraints: max positions, max share of the book per sub-sector,
gross exposure scaled by the regime; the remainder stays in cash.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import duckdb
import pandas as pd

from moi.logging import get_logger
from moi.ml.composite import latest_scores
from moi.ml.regime import Regime, current_regime

log = get_logger(__name__)


@dataclass(frozen=True)
class Position:
    ticker: str
    weight: float
    sub_sector: str
    score: float


@dataclass(frozen=True)
class TargetPortfolio:
    week_end: pd.Timestamp
    regime: Regime
    positions: list[Position]
    cash_weight: float


def select_with_sector_caps(
    ranked: pd.DataFrame,
    sectors: dict[str, str],
    *,
    top_n: int = 12,
    max_sector_share: float = 0.30,
) -> list[tuple[str, float]]:
    """Walk the ranking, admitting tickers while each sub-sector stays under its cap.

    Returns [(ticker, score)]. Cap = ceil(max_sector_share * top_n) names per sub-sector.
    """
    cap = math.ceil(max_sector_share * top_n)
    counts: dict[str, int] = {}
    picked: list[tuple[str, float]] = []
    for row in ranked.itertuples(index=False):
        if len(picked) >= top_n:
            break
        sector = sectors.get(str(row.ticker), "unknown")
        if counts.get(sector, 0) >= cap:
            continue
        counts[sector] = counts.get(sector, 0) + 1
        picked.append((str(row.ticker), float(row.score)))
    return picked


def build_portfolio(
    con: duckdb.DuckDBPyConnection,
    *,
    top_n: int = 12,
    max_sector_share: float = 0.30,
) -> TargetPortfolio:
    """Latest scores + regime → equal-weight target portfolio under constraints."""
    ranked = latest_scores(con)
    if ranked.empty:
        raise RuntimeError("No scores available — run `moi features build` first.")
    sectors = dict(
        con.execute(
            "SELECT ticker, coalesce(sub_sector, 'unknown') FROM universe WHERE active"
        ).fetchall()
    )
    regime = current_regime(con)
    picked = select_with_sector_caps(
        ranked, sectors, top_n=top_n, max_sector_share=max_sector_share
    )
    weight = regime.gross / len(picked) if picked else 0.0
    positions = [
        Position(ticker=t, weight=weight, sub_sector=sectors.get(t, "unknown"), score=s)
        for t, s in picked
    ]
    portfolio = TargetPortfolio(
        week_end=ranked["week_end"].iloc[0],
        regime=regime,
        positions=positions,
        cash_weight=1.0 - regime.gross,
    )
    log.info(
        "portfolio_built",
        positions=len(positions),
        gross=regime.gross,
        regime=regime.name,
    )
    return portfolio
