"""Whale 13F collector (SEC EDGAR via edgartools).

For each manager in ``config/whales.yaml``, fetch recent 13F-HR filings, normalize the
holdings table, compute the quarter-over-quarter change status against what's already in
the DB, and upsert into ``filings_13f``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import yaml

from moi.config import CONFIG_DIR, get_settings
from moi.logging import get_logger
from moi.runlog import track_run

log = get_logger(__name__)


@dataclass(frozen=True)
class Manager:
    cik: str
    name: str


@dataclass
class Holding:
    manager_cik: str
    manager_name: str
    period: date
    cusip: str
    ticker: str | None
    issuer: str | None
    value_usd: float | None
    shares: float | None
    change_status: str | None
    filed_at: date | None


def load_whales(path: Path | None = None) -> tuple[list[Manager], int]:
    data = yaml.safe_load((path or CONFIG_DIR / "whales.yaml").read_text()) or {}
    managers = [Manager(cik=str(m["cik"]), name=m["name"]) for m in data.get("managers", [])]
    return managers, int(data.get("backfill_quarters", 4))


# --------------------------------------------------------------------------- #
# Normalization (pure — tested without network)
# --------------------------------------------------------------------------- #
def _col(frame: Any, *candidates: str) -> str | None:
    """Find a DataFrame column by case-insensitive candidate names."""
    lowered = {str(c).lower(): str(c) for c in frame.columns}
    for cand in candidates:
        if cand.lower() in lowered:
            return lowered[cand.lower()]
    return None


def normalize_13f_table(
    manager: Manager, period: date, filed_at: date | None, frame: Any
) -> list[Holding]:
    """Normalize an edgartools 13F infotable DataFrame into Holdings.

    Tolerates column-name drift across edgartools versions (Cusip/CUSIP, Value/Value ($),
    Shares/SharesPrnAmount, ...). Aggregates duplicate CUSIPs (multiple share classes /
    discretion rows) by summing value and shares.
    """
    cusip_c = _col(frame, "cusip")
    if cusip_c is None or len(frame) == 0:
        return []
    value_c = _col(frame, "value", "value ($)", "value_usd")
    shares_c = _col(frame, "shares", "sharesprnamount", "shrs_or_prn_amt")
    ticker_c = _col(frame, "ticker", "symbol")
    issuer_c = _col(frame, "issuer", "nameofissuer", "name_of_issuer", "company")

    agg: dict[str, Holding] = {}
    for _, row in frame.iterrows():
        cusip = str(row[cusip_c]).strip()
        if not cusip or cusip.lower() == "nan":
            continue

        def num(col: str | None, row: Any = row) -> float | None:
            if col is None:
                return None
            try:
                v = float(row[col])
                return v if v == v else None  # filter NaN
            except (TypeError, ValueError):
                return None

        def text(col: str | None, row: Any = row) -> str | None:
            if col is None or row[col] is None:
                return None
            s = str(row[col]).strip()
            return s if s and s.lower() != "nan" else None

        value, shares = num(value_c), num(shares_c)
        if cusip in agg:
            prev = agg[cusip]
            prev.value_usd = (prev.value_usd or 0) + (value or 0)
            prev.shares = (prev.shares or 0) + (shares or 0)
        else:
            agg[cusip] = Holding(
                manager_cik=manager.cik,
                manager_name=manager.name,
                period=period,
                cusip=cusip,
                ticker=text(ticker_c),
                issuer=text(issuer_c),
                value_usd=value,
                shares=shares,
                change_status=None,
                filed_at=filed_at,
            )
    return list(agg.values())


def annotate_changes(
    holdings: list[Holding], previous_shares: dict[str, float]
) -> list[Holding]:
    """Set change_status on each holding vs the previous quarter's shares-by-cusip map."""
    for h in holdings:
        prev = previous_shares.get(h.cusip)
        if prev is None:
            h.change_status = "NEW"
        elif h.shares is None or abs(h.shares - prev) < 1:
            h.change_status = "UNCHANGED"
        elif h.shares > prev:
            h.change_status = "INCREASED"
        else:
            h.change_status = "DECREASED"
    return holdings


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def upsert_holdings(con: duckdb.DuckDBPyConnection, holdings: list[Holding]) -> int:
    if not holdings:
        return 0
    con.executemany(
        """
        INSERT INTO filings_13f
            (manager_cik, manager_name, period, cusip, ticker, issuer,
             value_usd, shares, change_status, filed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (manager_cik, period, cusip) DO UPDATE SET
            ticker = excluded.ticker,
            issuer = excluded.issuer,
            value_usd = excluded.value_usd,
            shares = excluded.shares,
            change_status = excluded.change_status,
            filed_at = excluded.filed_at
        """,
        [
            (
                h.manager_cik, h.manager_name, h.period, h.cusip, h.ticker, h.issuer,
                h.value_usd, h.shares, h.change_status, h.filed_at,
            )
            for h in holdings
        ],
    )
    return len(holdings)


def previous_period_shares(
    con: duckdb.DuckDBPyConnection, cik: str, before: date
) -> dict[str, float]:
    """Shares-by-cusip for the manager's latest stored period strictly before `before`."""
    row = con.execute(
        "SELECT max(period) FROM filings_13f WHERE manager_cik = ? AND period < ?",
        [cik, before],
    ).fetchone()
    if not row or row[0] is None:
        return {}
    prev_period = row[0]
    rows = con.execute(
        "SELECT cusip, shares FROM filings_13f WHERE manager_cik = ? AND period = ?",
        [cik, prev_period],
    ).fetchall()
    return {r[0]: float(r[1]) for r in rows if r[1] is not None}


# --------------------------------------------------------------------------- #
# Collection (network via edgartools)
# --------------------------------------------------------------------------- #
def collect_13f(con: duckdb.DuckDBPyConnection, whales_path: Path | None = None) -> int:
    """Fetch and store recent 13F holdings for all configured managers."""
    from edgar import Company, set_identity

    set_identity(get_settings().edgar_identity)
    managers, backfill = load_whales(whales_path)
    total = 0

    with track_run(con, job="collect.13f") as run:
        for mgr in managers:
            try:
                company = Company(int(mgr.cik))
                filings = company.get_filings(form="13F-HR").head(backfill)
            except Exception as exc:
                log.warning("13f_manager_failed", cik=mgr.cik, error=str(exc))
                continue

            # Oldest first so change-status diffs build forward in time.
            parsed: list[tuple[date, date | None, Any]] = []
            for filing in filings:
                try:
                    tf = filing.obj()
                    frame = tf.infotable
                    period = tf.report_period
                    if isinstance(period, str):
                        period = date.fromisoformat(period)
                    filed = getattr(filing, "filing_date", None)
                    parsed.append((period, filed, frame))
                except Exception as exc:
                    log.warning("13f_filing_parse_failed", cik=mgr.cik, error=str(exc))
            parsed.sort(key=lambda t: t[0])

            for period, filed, frame in parsed:
                holdings = normalize_13f_table(mgr, period, filed, frame)
                prev = previous_period_shares(con, mgr.cik, period)
                if prev:  # first stored quarter has no baseline — leave status NULL
                    annotate_changes(holdings, prev)
                written = upsert_holdings(con, holdings)
                total += written
                log.info(
                    "13f_stored", manager=mgr.name, period=str(period), holdings=written
                )
        run.add_rows(total)
        run.detail = f"managers={len(managers)}"
    return total
