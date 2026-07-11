"""Data-freshness checks powering ``moi status``.

Each check defines where to read the latest timestamp and how stale is acceptable.
Optional sources (those needing API keys) report ``skipped`` instead of red when empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import duckdb


@dataclass(frozen=True)
class FreshnessCheck:
    table: str
    ts_column: str
    max_age_days: int
    optional: bool = False  # True → empty table reports "skipped", not "stale"
    where: str | None = None  # extra filter (constant SQL, never user input)


CHECKS: list[FreshnessCheck] = [
    FreshnessCheck("prices_daily", "date", max_age_days=5),
    FreshnessCheck("filings_13f", "filed_at", max_age_days=120),
    FreshnessCheck("insider_form4", "filed_at", max_age_days=60, optional=True),
    FreshnessCheck("congress_trades", "disclosure_date", max_age_days=21, optional=True),
    # Resolved markets stop ticking — only open ones can be "stale".
    FreshnessCheck(
        "polymarket_series",
        "ts",
        max_age_days=5,
        optional=True,
        where="slug IN (SELECT slug FROM polymarket_markets WHERE NOT closed)",
    ),
    FreshnessCheck("news_items", "published_at", max_age_days=5),
    FreshnessCheck("macro_series", "date", max_age_days=45, optional=True),
]


@dataclass(frozen=True)
class TableStatus:
    table: str
    rows: int
    latest: str | None
    state: str  # "ok" | "stale" | "empty" | "skipped"


def _age_days(latest: object) -> int | None:
    if latest is None:
        return None
    if isinstance(latest, datetime):
        return (datetime.now() - latest).days
    if isinstance(latest, date):
        return (date.today() - latest).days
    return None


def check_freshness(con: duckdb.DuckDBPyConnection) -> list[TableStatus]:
    """Evaluate every freshness check against the database."""
    results: list[TableStatus] = []
    for check in CHECKS:
        clause = f" WHERE {check.where}" if check.where else ""
        row = con.execute(
            f"SELECT count(*), max({check.ts_column}) FROM {check.table}{clause}"
        ).fetchone()
        rows = int(row[0]) if row else 0
        latest = row[1] if row else None

        if rows == 0:
            state = "skipped" if check.optional else "empty"
        else:
            age = _age_days(latest)
            state = "ok" if age is not None and age <= check.max_age_days else "stale"
        results.append(
            TableStatus(
                table=check.table,
                rows=rows,
                latest=str(latest) if latest is not None else None,
                state=state,
            )
        )
    return results


@dataclass(frozen=True)
class PriceGap:
    ticker: str
    latest: date | None
    lag_days: int


def price_gaps(con: duckdb.DuckDBPyConnection, max_lag_days: int = 5) -> list[PriceGap]:
    """Tickers whose own latest bar lags the table-wide latest bar.

    The global freshness check can't see per-ticker holes (one healthy ticker keeps
    the global max fresh) — this is the net under a rate-limited or delisted symbol.
    """
    rows = con.execute(
        """WITH per AS (
               SELECT ticker, max(date) AS latest FROM prices_daily GROUP BY ticker
           ), overall AS (SELECT max(date) AS m FROM prices_daily)
           SELECT u.ticker, per.latest, coalesce(overall.m - per.latest, 99999) AS lag
           FROM universe u
           LEFT JOIN per ON per.ticker = u.ticker
           CROSS JOIN overall
           WHERE u.active AND (per.latest IS NULL OR overall.m - per.latest > ?)
           ORDER BY lag DESC""",
        [max_lag_days],
    ).fetchall()
    return [PriceGap(ticker=t, latest=latest, lag_days=int(lag)) for t, latest, lag in rows]
