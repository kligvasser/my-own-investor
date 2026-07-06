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


CHECKS: list[FreshnessCheck] = [
    FreshnessCheck("prices_daily", "date", max_age_days=5),
    FreshnessCheck("filings_13f", "filed_at", max_age_days=120),
    FreshnessCheck("insider_form4", "filed_at", max_age_days=60, optional=True),
    FreshnessCheck("congress_trades", "disclosure_date", max_age_days=21, optional=True),
    FreshnessCheck("polymarket_series", "ts", max_age_days=5),
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
        row = con.execute(
            f"SELECT count(*), max({check.ts_column}) FROM {check.table}"
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
