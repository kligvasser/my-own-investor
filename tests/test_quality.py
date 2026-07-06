"""Freshness board states."""

from __future__ import annotations

from datetime import date

from moi.ingest.quality import check_freshness


def test_empty_db_states(db) -> None:
    states = {t.table: t.state for t in check_freshness(db)}
    assert states["prices_daily"] == "empty"        # required source
    assert states["congress_trades"] == "skipped"   # optional (needs API key)
    assert states["macro_series"] == "skipped"


def test_fresh_and_stale(db) -> None:
    db.execute(
        "INSERT INTO prices_daily (ticker, date, close, source) VALUES ('X', ?, 1.0, 't')",
        [date.today()],
    )
    db.execute(
        "INSERT INTO macro_series (series_id, date, value) VALUES ('OLD', '2020-01-01', 1.0)"
    )
    states = {t.table: t.state for t in check_freshness(db)}
    assert states["prices_daily"] == "ok"
    assert states["macro_series"] == "stale"
