"""Urgent trigger checks on synthetic data."""

from __future__ import annotations

from datetime import date, timedelta

from moi.orchestrator.watch import big_move_alerts, whale_filing_alerts


def _seed_universe(db, ticker: str = "ALAB") -> None:
    db.execute(
        "INSERT OR REPLACE INTO universe (ticker, is_benchmark, active) VALUES (?, FALSE, TRUE)",
        [ticker],
    )


def test_big_move_fires(db) -> None:
    _seed_universe(db)
    d1, d2 = date.today() - timedelta(days=1), date.today()
    db.execute(
        "INSERT INTO prices_daily (ticker, date, close, source) VALUES ('ALAB', ?, 100, 't')",
        [d1],
    )
    db.execute(
        "INSERT INTO prices_daily (ticker, date, close, source) VALUES ('ALAB', ?, 85, 't')",
        [d2],
    )
    alerts = big_move_alerts(db)
    assert len(alerts) == 1
    assert "ALAB" in alerts[0].message
    assert "-15.0%" in alerts[0].message


def test_small_move_quiet(db) -> None:
    _seed_universe(db)
    d1, d2 = date.today() - timedelta(days=1), date.today()
    db.execute(
        "INSERT INTO prices_daily (ticker, date, close, source) VALUES ('ALAB', ?, 100, 't')",
        [d1],
    )
    db.execute(
        "INSERT INTO prices_daily (ticker, date, close, source) VALUES ('ALAB', ?, 103, 't')",
        [d2],
    )
    assert big_move_alerts(db) == []


def test_whale_filing_alert(db) -> None:
    _seed_universe(db, "COHR")
    db.execute(
        """INSERT INTO filings_13f
           (manager_cik, manager_name, period, cusip, ticker, change_status, filed_at)
           VALUES ('1', 'Berkshire', '2026-03-31', 'c1', 'COHR', 'NEW', current_date)"""
    )
    alerts = whale_filing_alerts(db)
    assert len(alerts) == 1
    assert "COHR" in alerts[0].message
