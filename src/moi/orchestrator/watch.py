"""Urgent watcher — cheap daily trigger checks between weekly runs (PLAN §7).

Alerts are deduplicated via the ``alerts_sent`` table: the same (kind, key) is not
re-sent within its cool-down, so a stale table or a 3-day filing window doesn't
retrain the reader to ignore Telegram.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import duckdb

from moi.ingest.quality import check_freshness, price_gaps
from moi.logging import get_logger

log = get_logger(__name__)

MOVE_THRESHOLD = 0.12  # daily move that warrants an alert
RESEND_AFTER = timedelta(days=3)  # cool-down before an identical alert repeats


@dataclass(frozen=True)
class Alert:
    kind: str  # big_move | whale_filing | data_quality | stuck_order | stale_approval
    #           | market_rotation
    message: str
    key: str = ""  # dedup key within kind (defaults to the message)

    @property
    def dedup_key(self) -> str:
        return self.key or self.message


def big_move_alerts(
    con: duckdb.DuckDBPyConnection, threshold: float = MOVE_THRESHOLD
) -> list[Alert]:
    """Universe tickers whose latest close moved more than ±threshold day-over-day."""
    rows = con.execute(
        """
        WITH latest AS (
            SELECT ticker, date, coalesce(adj_close, close) AS px,
                   lag(coalesce(adj_close, close)) OVER (
                       PARTITION BY ticker ORDER BY date) AS prev,
                   row_number() OVER (PARTITION BY ticker ORDER BY date DESC) AS rn
            FROM prices_daily
            WHERE ticker IN (SELECT ticker FROM universe WHERE active AND NOT is_benchmark)
        )
        SELECT ticker, date, px / prev - 1 AS ret
        FROM latest WHERE rn = 1 AND prev IS NOT NULL AND abs(px / prev - 1) >= ?
        ORDER BY abs(px / prev - 1) DESC
        """,
        [threshold],
    ).fetchall()
    return [Alert("big_move", f"{t} moved {r:+.1%} on {d}", key=f"{t}:{d}") for t, d, r in rows]


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
    return [Alert("whale_filing", f"{m} filed: {t} {c}", key=f"{m}:{t}:{c}") for m, t, c in rows]


def data_quality_alerts(con: duckdb.DuckDBPyConnection) -> list[Alert]:
    bad = [t for t in check_freshness(con) if t.state in ("stale", "empty")]
    alerts = [Alert("data_quality", f"{t.table} is {t.state}", key=t.table) for t in bad]
    alerts += [
        Alert(
            "data_quality",
            f"price gap: {g.ticker} last bar {g.latest or 'never'} ({g.lag_days}d behind)",
            key=f"gap:{g.ticker}",
        )
        for g in price_gaps(con)
    ]
    return alerts


def execution_alerts(con: duckdb.DuckDBPyConnection) -> list[Alert]:
    """Orders stuck in limbo and approvals nobody executed — the loop-closers."""
    alerts = [
        Alert(
            "stuck_order",
            f"order {t} {side} is '{status}' for {age_h:.0f}h — run `moi orders --sync`",
            key=oid,
        )
        for oid, t, side, status, age_h in con.execute(
            """SELECT order_id, ticker, side, status,
                      extract(epoch FROM (current_timestamp - created_at)) / 3600
               FROM orders WHERE status IN ('submitted', 'unknown')
                 AND created_at < current_timestamp - INTERVAL 24 HOUR"""
        ).fetchall()
    ]
    alerts += [
        Alert(
            "stale_approval",
            f"suggestion {t} {action} approved {age_d:.0f}d ago but never executed (expires at 7d)",
            key=sid,
        )
        for sid, t, action, age_d in con.execute(
            """SELECT s.id, s.ticker, s.action,
                      extract(epoch FROM (current_timestamp - s.decided_at)) / 86400
               FROM suggestions s WHERE s.status = 'APPROVED'
                 AND s.decided_at < current_timestamp - INTERVAL 3 DAY
                 AND NOT EXISTS (SELECT 1 FROM orders o
                                 WHERE o.suggestion_id = s.id AND o.status != 'error')"""
        ).fetchall()
    ]
    return alerts


def market_rotation_alerts(
    con: duckdb.DuckDBPyConnection, tracked: set[str] | None = None
) -> list[Alert]:
    """Tracked Polymarket markets that closed or expire soon — rotate the slug.

    Date-dependent markets ("… by July 31") resolve monthly; a closed slug silently
    stops producing signal until someone swaps it in config/polymarket.yaml (or via
    the dashboard's Trends → Manage tracked markets).
    """
    if tracked is None:
        from moi.ingest.polymarket import load_tracked_markets

        try:
            tracked = {m.slug for m in load_tracked_markets()}
        except Exception:  # unreadable config — not this trigger's problem
            return []
    alerts: list[Alert] = []
    now = datetime.now()
    rows = con.execute("SELECT slug, question, closed, end_date FROM polymarket_markets").fetchall()
    for slug, question, closed, end_date in rows:
        if slug not in tracked:
            continue
        if closed:
            alerts.append(
                Alert(
                    "market_rotation",
                    f"polymarket market closed — rotate the slug: {question or slug}",
                    key=f"closed:{slug}",
                )
            )
        elif end_date is not None and end_date < now + timedelta(days=3):
            alerts.append(
                Alert(
                    "market_rotation",
                    f"polymarket market ends {end_date:%Y-%m-%d}: {question or slug}",
                    key=f"expiring:{slug}",
                )
            )
    return alerts


def _fresh(con: duckdb.DuckDBPyConnection, alerts: list[Alert]) -> list[Alert]:
    """Drop alerts already sent within the cool-down; journal the ones that pass."""
    out: list[Alert] = []
    now = datetime.now()
    for a in alerts:
        row = con.execute(
            "SELECT last_sent FROM alerts_sent WHERE kind = ? AND key = ?",
            [a.kind, a.dedup_key],
        ).fetchone()
        if row and now - row[0] < RESEND_AFTER:
            continue
        con.execute(
            "INSERT OR REPLACE INTO alerts_sent (kind, key, last_sent) VALUES (?, ?, ?)",
            [a.kind, a.dedup_key, now],
        )
        out.append(a)
    return out


def run_watch(con: duckdb.DuckDBPyConnection) -> list[Alert]:
    """Evaluate all triggers; notify anything that fired and isn't a recent repeat."""
    alerts = _fresh(
        con,
        big_move_alerts(con)
        + whale_filing_alerts(con)
        + data_quality_alerts(con)
        + execution_alerts(con)
        + market_rotation_alerts(con),
    )
    if alerts:
        from moi.report.notify import send

        body = "moi urgent alerts:\n" + "\n".join(f"• {a.message}" for a in alerts)
        send(body)
        for a in alerts:
            log.warning("urgent_alert", kind=a.kind, message=a.message)
    else:
        log.info("watch_quiet")
    return alerts
