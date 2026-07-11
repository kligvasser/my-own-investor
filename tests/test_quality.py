"""Freshness board states."""

from __future__ import annotations

from datetime import date

from moi.ingest.quality import check_freshness


def test_empty_db_states(db) -> None:
    states = {t.table: t.state for t in check_freshness(db)}
    assert states["prices_daily"] == "empty"  # required source
    assert states["congress_trades"] == "skipped"  # optional (needs API key)
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


def test_price_gaps_flags_laggards_and_missing(db) -> None:
    from moi.ingest.quality import price_gaps

    db.execute("INSERT INTO universe (ticker, active) VALUES ('FRESH', TRUE)")
    db.execute("INSERT INTO universe (ticker, active) VALUES ('LAGGY', TRUE)")
    db.execute("INSERT INTO universe (ticker, active) VALUES ('NEVER', TRUE)")
    db.execute(
        "INSERT INTO prices_daily (ticker, date, close, source) "
        "VALUES ('FRESH', current_date, 1, 't')"
    )
    db.execute(
        "INSERT INTO prices_daily (ticker, date, close, source) "
        "VALUES ('LAGGY', current_date - INTERVAL 20 DAY, 1, 't')"
    )
    gaps = {g.ticker: g for g in price_gaps(db)}
    assert set(gaps) == {"LAGGY", "NEVER"}
    assert gaps["LAGGY"].lag_days == 20
    assert gaps["NEVER"].latest is None
