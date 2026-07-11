"""Holdings X-ray analytics."""

from __future__ import annotations

import numpy as np
import pandas as pd

from moi.report.xray import (
    contribution,
    correlation_matrix,
    effective_positions,
    growth_frame,
    insights,
    portfolio_returns,
    risk_table,
)


def _closes(n: int = 260) -> pd.DataFrame:
    idx = pd.bdate_range("2025-01-01", periods=n)
    rng = np.random.default_rng(5)
    spy = 100 * np.cumprod(1 + rng.normal(0.0004, 0.01, n))
    return pd.DataFrame(
        {
            "SPY": spy,
            "HI_BETA": 100 * np.cumprod(1 + 2 * pd.Series(spy).pct_change().fillna(0).to_numpy()),
            "FLAT": np.full(n, 100.0),
            "TWIN": spy * 1.5,  # perfectly correlated with SPY
        },
        index=idx,
    )


WEIGHTS = {"HI_BETA": 0.5, "FLAT": 0.3, "TWIN": 0.2}


def test_portfolio_returns_frozen_weights() -> None:
    closes = _closes()
    port = portfolio_returns(closes, {"FLAT": 1.0})
    assert abs(port).max() < 1e-12  # all-flat book has zero returns


def test_growth_frame_anchors_at_100() -> None:
    growth = growth_frame(_closes(), WEIGHTS, 100)
    assert abs(growth.iloc[0] - 100.0).max() < 1e-9
    assert "Portfolio" in growth.columns
    assert "SPY" in growth.columns


def test_risk_table_beta_ordering() -> None:
    risk = risk_table(_closes(), WEIGHTS, 200)
    assert risk.loc["HI_BETA", "beta"] > 1.5
    assert abs(risk.loc["TWIN", "beta"] - 1.0) < 0.1  # scaled copy of SPY → beta 1
    assert risk.loc["FLAT", "ann_vol"] < 0.01
    assert abs(risk.loc["SPY", "corr"] - 1.0) < 1e-9


def test_contribution_signs() -> None:
    contrib = contribution(_closes(), WEIGHTS, 200)
    assert abs(contrib["FLAT"]) < 1e-9
    assert contrib["HI_BETA"] != 0


def test_correlation_and_effective_positions() -> None:
    corr = correlation_matrix(_closes(), ["TWIN", "SPY", "FLAT"], 200)
    assert abs(corr.loc["TWIN", "SPY"] - 1.0) < 1e-6
    assert abs(effective_positions({"A": 0.5, "B": 0.5}) - 2.0) < 1e-9
    assert effective_positions({"A": 0.9, "B": 0.1}) < 1.3


def test_insights_mention_concentration_and_lockstep() -> None:
    closes = _closes()
    weights = {"TWIN": 0.6, "SPY": 0.3, "FLAT": 0.1}
    risk = risk_table(closes, weights, 200)
    corr = correlation_matrix(closes, list(weights), 200)
    contrib = contribution(closes, weights, 200)
    notes = " ".join(insights(weights, risk, corr, contrib))
    assert "Concentration" in notes
    assert "lockstep" in notes  # TWIN vs SPY correlation 1.0
