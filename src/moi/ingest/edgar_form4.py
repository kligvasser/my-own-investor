"""Insider Form 4 collector for universe tickers (SEC EDGAR via edgartools).

Stores open-market insider transactions in ``insider_form4``. Extraction is defensive:
edgartools' Form 4 object model has shifted across versions, so we probe a few known
shapes and normalize a flexible DataFrame the same way the 13F collector does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import duckdb

from moi.config import get_settings
from moi.logging import get_logger
from moi.runlog import track_run
from moi.universe import candidate_tickers

log = get_logger(__name__)


@dataclass(frozen=True)
class InsiderTx:
    accession: str
    seq: int
    ticker: str
    insider: str | None
    role: str | None
    tx_date: date | None
    code: str | None
    shares: float | None
    price: float | None
    value_usd: float | None
    filed_at: datetime | None


def _col(frame: Any, *candidates: str) -> str | None:
    lowered = {str(c).lower(): str(c) for c in frame.columns}
    for cand in candidates:
        if cand.lower() in lowered:
            return lowered[cand.lower()]
    return None


def _num(row: Any, col: str | None) -> float | None:
    if col is None:
        return None
    try:
        v = float(row[col])
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _text(row: Any, col: str | None) -> str | None:
    if col is None or row[col] is None:
        return None
    s = str(row[col]).strip()
    return s if s and s.lower() != "nan" else None


def _to_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def normalize_form4_frame(
    ticker: str,
    accession: str,
    filed_at: datetime | None,
    insider: str | None,
    role: str | None,
    frame: Any,
) -> list[InsiderTx]:
    """Normalize a Form 4 transactions DataFrame into InsiderTx rows."""
    if frame is None or len(frame) == 0:
        return []
    date_c = _col(frame, "date", "transaction_date", "transactiondate")
    code_c = _col(frame, "code", "transaction_code", "transactioncode")
    shares_c = _col(frame, "shares", "quantity", "amount")
    price_c = _col(frame, "price", "price_per_share")
    value_c = _col(frame, "value", "value_usd", "total")
    insider_c = _col(frame, "insider", "owner", "name", "reporting_owner")
    role_c = _col(frame, "position", "role", "title", "officer_title")

    rows: list[InsiderTx] = []
    for i, (_, row) in enumerate(frame.iterrows()):
        shares, price = _num(row, shares_c), _num(row, price_c)
        value = _num(row, value_c)
        if value is None and shares is not None and price is not None:
            value = shares * price
        rows.append(
            InsiderTx(
                accession=accession,
                seq=i,
                ticker=ticker,
                insider=_text(row, insider_c) or insider,
                role=_text(row, role_c) or role,
                tx_date=_to_date(row[date_c]) if date_c else None,
                code=_text(row, code_c),
                shares=shares,
                price=price,
                value_usd=value,
                filed_at=filed_at,
            )
        )
    return rows


def _extract_frame(form4: Any) -> Any:
    """Probe known edgartools Form4 shapes for a transactions DataFrame."""
    for attr in ("market_trades", "transactions", "non_derivative_trades"):
        frame = getattr(form4, attr, None)
        if frame is not None and hasattr(frame, "columns") and len(frame) > 0:
            return frame
    to_df = getattr(form4, "to_dataframe", None)
    if callable(to_df):
        try:
            return to_df()
        except Exception:
            return None
    return None


def upsert_insider(con: duckdb.DuckDBPyConnection, rows: list[InsiderTx]) -> int:
    if not rows:
        return 0
    con.executemany(
        """
        INSERT INTO insider_form4
            (accession, seq, ticker, insider, role, tx_date, code,
             shares, price, value_usd, filed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (accession, seq) DO UPDATE SET
            insider = excluded.insider,
            role = excluded.role,
            tx_date = excluded.tx_date,
            code = excluded.code,
            shares = excluded.shares,
            price = excluded.price,
            value_usd = excluded.value_usd,
            filed_at = excluded.filed_at
        """,
        [
            (
                r.accession, r.seq, r.ticker, r.insider, r.role, r.tx_date, r.code,
                r.shares, r.price, r.value_usd, r.filed_at,
            )
            for r in rows
        ],
    )
    return len(rows)


def collect_form4(
    con: duckdb.DuckDBPyConnection,
    tickers: list[str] | None = None,
    filings_per_ticker: int = 10,
) -> int:
    """Fetch recent Form 4 filings for universe tickers and store transactions."""
    from edgar import Company, set_identity

    set_identity(get_settings().edgar_identity)
    syms = tickers if tickers is not None else candidate_tickers()
    total = 0

    with track_run(con, job="collect.form4") as run:
        for ticker in syms:
            try:
                filings = Company(ticker).get_filings(form="4").head(filings_per_ticker)
            except Exception as exc:
                log.warning("form4_company_failed", ticker=ticker, error=str(exc))
                continue
            for filing in filings:
                accession = str(getattr(filing, "accession_no", "") or "")
                if not accession:
                    continue
                # Skip filings we've already stored (idempotent fast path).
                seen = con.execute(
                    "SELECT count(*) FROM insider_form4 WHERE accession = ?", [accession]
                ).fetchone()
                if seen and seen[0] > 0:
                    continue
                try:
                    form4 = filing.obj()
                    frame = _extract_frame(form4)
                    filed = getattr(filing, "filing_date", None)
                    filed_dt = (
                        datetime.combine(filed, datetime.min.time())
                        if isinstance(filed, date) and not isinstance(filed, datetime)
                        else filed
                    )
                    insider = getattr(form4, "insider_name", None) or getattr(
                        form4, "reporting_owner", None
                    )
                    rows = normalize_form4_frame(
                        ticker, accession, filed_dt, insider, None, frame
                    )
                    total += upsert_insider(con, rows)
                except Exception as exc:
                    log.warning(
                        "form4_parse_failed", ticker=ticker, accession=accession, error=str(exc)
                    )
            log.info("form4_ticker_done", ticker=ticker)
        run.add_rows(total)
        run.detail = f"tickers={len(syms)}"
    return total
