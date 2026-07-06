"""Regime model v1 — transparent rules, no fitting.

Classifies the market week as risk-on / neutral / risk-off from theme momentum and
credit conditions, and maps that to a gross-exposure scalar the portfolio constructor
applies. This is where the market-level features (constant cross-sectionally, useless
to the ranker) do their real job.

Rules (v1, deliberately coarse):
    risk_off  if SMH 13w return < -10%  OR  HY spread widened > +0.75pt over 13w
    risk_on   if SMH 13w return > 0     AND HY spread not widening (≤ +0.25pt)
    neutral   otherwise
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb
import pandas as pd

from moi.features.market import MARKET_TICKER
from moi.logging import get_logger

log = get_logger(__name__)

GROSS_EXPOSURE = {"risk_on": 1.0, "neutral": 0.75, "risk_off": 0.45}


@dataclass(frozen=True)
class Regime:
    week_end: pd.Timestamp | None
    name: str  # risk_on | neutral | risk_off
    gross: float
    inputs: dict[str, float]


def classify(inputs: dict[str, float]) -> str:
    """Pure rule evaluation on {feature: value}. Missing inputs default to neutral."""
    smh_13w = inputs.get("mkt_smh_ret_13w")
    hy_chg = inputs.get("mkt_hy_spread_13w_chg")

    if (smh_13w is not None and smh_13w < -0.10) or (hy_chg is not None and hy_chg > 0.75):
        return "risk_off"
    if smh_13w is not None and smh_13w > 0 and (hy_chg is None or hy_chg <= 0.25):
        return "risk_on"
    return "neutral"


def current_regime(con: duckdb.DuckDBPyConnection) -> Regime:
    """Classify the latest week with market features available."""
    df = con.execute(
        """SELECT week_end, feature, value FROM features_weekly
           WHERE ticker = ? AND week_end = (
               SELECT max(week_end) FROM features_weekly WHERE ticker = ?
           )""",
        [MARKET_TICKER, MARKET_TICKER],
    ).df()
    if df.empty:
        log.warning("regime_no_market_features")
        return Regime(week_end=None, name="neutral", gross=GROSS_EXPOSURE["neutral"], inputs={})

    inputs = {str(r.feature): float(r.value) for r in df.itertuples(index=False)}
    name = classify(inputs)
    week = pd.to_datetime(df["week_end"].iloc[0])
    log.info("regime", week=str(week.date()), name=name)
    return Regime(week_end=week, name=name, gross=GROSS_EXPOSURE[name], inputs=inputs)
