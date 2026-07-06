"""13F normalization, change annotation, and idempotent upsert."""

from __future__ import annotations

from datetime import date

import pandas as pd

from moi.ingest.edgar_13f import (
    Holding,
    Manager,
    annotate_changes,
    normalize_13f_table,
    previous_period_shares,
    upsert_holdings,
)

MGR = Manager(cik="1067983", name="Berkshire Hathaway")


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Issuer": ["Apple Inc", "Coherent Corp", "Coherent Corp"],
            "Cusip": ["037833100", "19247G107", "19247G107"],
            "Ticker": ["AAPL", "COHR", "COHR"],
            "Value": [1_000_000.0, 500_000.0, 250_000.0],
            "Shares": [10_000.0, 5_000.0, 2_500.0],
        }
    )


def test_normalize_aggregates_duplicate_cusips() -> None:
    holdings = normalize_13f_table(MGR, date(2026, 3, 31), date(2026, 5, 10), _frame())
    assert len(holdings) == 2
    cohr = next(h for h in holdings if h.cusip == "19247G107")
    assert cohr.value_usd == 750_000.0
    assert cohr.shares == 7_500.0
    assert cohr.ticker == "COHR"


def test_annotate_changes() -> None:
    holdings = normalize_13f_table(MGR, date(2026, 3, 31), None, _frame())
    prev = {"037833100": 10_000.0, "19247G107": 9_000.0, "GONE00000": 1.0}
    annotate_changes(holdings, prev)
    by_cusip = {h.cusip: h.change_status for h in holdings}
    assert by_cusip["037833100"] == "UNCHANGED"
    assert by_cusip["19247G107"] == "DECREASED"


def test_upsert_and_previous_period(db) -> None:
    q1 = normalize_13f_table(MGR, date(2026, 3, 31), None, _frame())
    assert upsert_holdings(db, q1) == 2
    upsert_holdings(db, q1)  # idempotent
    assert db.execute("SELECT count(*) FROM filings_13f").fetchone()[0] == 2

    prev = previous_period_shares(db, MGR.cik, date(2026, 6, 30))
    assert prev["037833100"] == 10_000.0

    # New quarter with a NEW position diffs against Q1.
    q2_frame = pd.DataFrame(
        {
            "Issuer": ["Vertiv"], "Cusip": ["92537N108"], "Ticker": ["VRT"],
            "Value": [100.0], "Shares": [10.0],
        }
    )
    q2 = normalize_13f_table(MGR, date(2026, 6, 30), None, q2_frame)
    annotate_changes(q2, previous_period_shares(db, MGR.cik, date(2026, 6, 30)))
    assert q2[0].change_status == "NEW"


def test_normalize_handles_missing_columns() -> None:
    frame = pd.DataFrame({"SomethingElse": [1]})
    assert normalize_13f_table(MGR, date(2026, 3, 31), None, frame) == []


def test_holding_dataclass_defaults() -> None:
    h = Holding(
        manager_cik="1", manager_name="x", period=date(2026, 1, 1), cusip="c",
        ticker=None, issuer=None, value_usd=None, shares=None,
        change_status=None, filed_at=None,
    )
    assert h.change_status is None
