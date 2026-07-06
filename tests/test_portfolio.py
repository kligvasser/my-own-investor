"""Regime rules and sector-capped selection."""

from __future__ import annotations

import pandas as pd

from moi.ml.portfolio import select_with_sector_caps
from moi.ml.regime import GROSS_EXPOSURE, classify


def test_regime_rules() -> None:
    assert classify({"mkt_smh_ret_13w": 0.15, "mkt_hy_spread_13w_chg": -0.1}) == "risk_on"
    assert classify({"mkt_smh_ret_13w": -0.15, "mkt_hy_spread_13w_chg": 0.0}) == "risk_off"
    assert classify({"mkt_smh_ret_13w": 0.05, "mkt_hy_spread_13w_chg": 1.0}) == "risk_off"
    assert classify({"mkt_smh_ret_13w": -0.05, "mkt_hy_spread_13w_chg": 0.0}) == "neutral"
    assert classify({}) == "neutral"
    assert GROSS_EXPOSURE["risk_off"] < GROSS_EXPOSURE["neutral"] < GROSS_EXPOSURE["risk_on"]


def test_sector_caps_enforced() -> None:
    ranked = pd.DataFrame(
        {
            "ticker": [f"T{i}" for i in range(10)],
            "score": [1.0 - i / 10 for i in range(10)],
        }
    )
    # First five tickers all in the same hot sector.
    sectors = {f"T{i}": ("hot" if i < 5 else f"s{i}") for i in range(10)}
    picked = select_with_sector_caps(ranked, sectors, top_n=6, max_sector_share=0.34)
    tickers = [t for t, _ in picked]
    assert len(picked) == 6
    # cap = ceil(0.34 * 6) = 3 → only three "hot" names admitted, rest skipped over.
    assert sum(1 for t in tickers if sectors[t] == "hot") == 3
    assert tickers[:3] == ["T0", "T1", "T2"]


def test_selection_respects_ranking_order() -> None:
    ranked = pd.DataFrame({"ticker": ["A", "B", "C"], "score": [0.9, 0.5, 0.1]})
    picked = select_with_sector_caps(ranked, {"A": "x", "B": "y", "C": "z"}, top_n=2)
    assert [t for t, _ in picked] == ["A", "B"]
