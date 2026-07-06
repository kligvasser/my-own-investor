"""Urgent watcher — cheap daily trigger checks between weekly runs (PLAN §7)."""

from __future__ import annotations

from dataclasses import dataclass

import duckdb

from moi.ingest.quality import check_freshness
from moi.logging import get_logger

log = get_logger(__name__)

MOVE_THRESHOLD = 0.12  # daily move that warrants an alert


@dataclass(frozen=True)
class Alert:
    kind: str  # big_move | whale_filing | data_quality
    message: str


def big_move_alerts(
    con: duckdb.DuckDBPyConnection, threshold: float = MOVE_THRESHOLD
) -> list[Alert]:
    """Universe tickers whose latest close moved more than ±threshold day-over-day."""
    rows = con.execute(
        """
        WITH latest AS (
            SELECT ticker, date, close,
                   lag(close) OVER (PARTITION BY ticker ORDER BY date) AS prev,
                   row_number() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
            FROM prices_daily
            WHERE ticker IN (SELECT ticker FROM universe WHERE active AND NOT is_benchmark)
        )
        SELECT ticker, date, close / prev - 1 AS ret
        FROM latest WHERE rn = 1 AND prev IS NOT NULL AND abs(close / prev - 1) >= ?
        ORDER BY abs(close / prev - 1) DESC
        """,
        [threshold],
    ).fetchall()
    return [Alert("big_move", f"{t} moved {r:+.1%} on {d}") for t, d, r in rows]


def whale_filing_alerts(con: duckdb.DuckDBPyConnection, days: int = 3) -> list[Alert]:
    """Fresh 13F filings that touch the universe."""
    rows = con.execute(
        """
        SELECT DISTINCT f.manager_name, f.ticker, f.change_status
        FROM filings_13f f
        JOIN universe u ON u.ticker = f.ticker AND u.active AND NOT u.is_benchmark
        WHERE f.filed_at >= current_date - ? * INTERVAL 1 DAY
        """,
        [days],
    ).fetchall()
    return [Alert("whale_filing", f"{m} filed: {t} {c}") for m, t, c in rows]


def data_quality_alerts(con: duckdb.DuckDBPyConnection) -> list[Alert]:
    bad = [t for t in check_freshness(con) if t.state in ("stale", "empty")]
    return [Alert("data_quality", f"{t.table} is {t.state}") for t in bad]


def run_watch(con: duckdb.DuckDBPyConnection) -> list[Alert]:
    """Evaluate all triggers; notify if anything fired."""
    alerts = big_move_alerts(con) + whale_filing_alerts(con) + data_quality_alerts(con)
    if alerts:
        from moi.report.notify import send

        body = "moi urgent alerts:\n" + "\n".join(f"• {a.message}" for a in alerts)
        send(body)
        for a in alerts:
            log.warning("urgent_alert", kind=a.kind, message=a.message)
    else:
        log.info("watch_quiet")
    return alerts
