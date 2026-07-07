"""Daily OHLCV collector.

Sources: yfinance (free, reliable for backfill) and IBKR historical bars (when a gateway
is connected). Both normalize to the same :class:`PriceRow` shape and upsert idempotently
into ``prices_daily`` keyed by ``(ticker, date)`` — re-running never duplicates rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import duckdb

from moi.logging import get_logger
from moi.runlog import track_run
from moi.universe import all_tickers

log = get_logger(__name__)


@dataclass(frozen=True)
class PriceRow:
    ticker: str
    date: date
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    adj_close: float | None
    volume: int | None
    source: str


# --------------------------------------------------------------------------- #
# Normalization (pure, unit-testable without network)
# --------------------------------------------------------------------------- #
def normalize_yf_frame(ticker: str, frame: object) -> list[PriceRow]:
    """Convert a single-ticker yfinance/pandas DataFrame into PriceRows.

    Expects columns Open/High/Low/Close and optionally Adj Close/Volume, indexed by date.
    """
    import math

    import pandas as pd  # local import keeps module import light

    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return []

    def cell(row: pd.Series, *names: str) -> float | None:
        for n in names:
            if n in row and not (isinstance(row[n], float) and math.isnan(row[n])):
                return float(row[n])
        return None

    rows: list[PriceRow] = []
    for idx, r in frame.iterrows():
        d = idx.date() if hasattr(idx, "date") else pd.Timestamp(idx).date()
        close = cell(r, "Close")
        vol = cell(r, "Volume")
        rows.append(
            PriceRow(
                ticker=ticker,
                date=d,
                open=cell(r, "Open"),
                high=cell(r, "High"),
                low=cell(r, "Low"),
                close=close,
                adj_close=cell(r, "Adj Close") or close,
                volume=int(vol) if vol is not None else None,
                source="yfinance",
            )
        )
    return rows


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #
def fetch_yfinance(tickers: list[str], start: date, end: date) -> list[PriceRow]:
    """Fetch daily bars for many tickers via yfinance."""
    import yfinance as yf

    rows: list[PriceRow] = []
    # per-ticker loop: robust column handling and isolates a bad symbol
    for ticker in tickers:
        try:
            frame = yf.download(
                ticker,
                start=start.isoformat(),
                end=(end + timedelta(days=1)).isoformat(),
                auto_adjust=False,
                progress=False,
                threads=False,
            )
            # yf may return MultiIndex columns for a single ticker; flatten them.
            if hasattr(frame, "columns") and getattr(frame.columns, "nlevels", 1) > 1:
                frame = frame.droplevel(1, axis=1)
            fetched = normalize_yf_frame(ticker, frame)
            rows.extend(fetched)
            log.info("yf_fetched", ticker=ticker, rows=len(fetched))
        except Exception as exc:
            log.warning("yf_fetch_failed", ticker=ticker, error=str(exc))
    return rows


def fetch_ibkr(ib: object, tickers: list[str], years: int) -> list[PriceRow]:
    """Fetch daily bars from IBKR historical data for the given tickers."""
    from ib_async import Stock

    rows: list[PriceRow] = []
    for ticker in tickers:
        try:
            contract = Stock(ticker, "SMART", "USD")
            bars = ib.reqHistoricalData(  # type: ignore[attr-defined]
                contract,
                endDateTime="",
                durationStr=f"{years} Y",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
            for b in bars:
                d = (
                    b.date
                    if isinstance(b.date, date)
                    else datetime.strptime(str(b.date), "%Y%m%d").date()
                )
                rows.append(
                    PriceRow(
                        ticker=ticker,
                        date=d,
                        open=float(b.open),
                        high=float(b.high),
                        low=float(b.low),
                        close=float(b.close),
                        adj_close=float(b.close),
                        volume=int(b.volume) if b.volume and b.volume > 0 else None,
                        source="ibkr",
                    )
                )
            log.info("ibkr_fetched", ticker=ticker)
        except Exception as exc:
            log.warning("ibkr_fetch_failed", ticker=ticker, error=str(exc))
    return rows


# --------------------------------------------------------------------------- #
# Persistence (idempotent)
# --------------------------------------------------------------------------- #
def upsert_prices(con: duckdb.DuckDBPyConnection, rows: list[PriceRow]) -> int:
    """Idempotently upsert price rows by (ticker, date). Returns rows processed."""
    if not rows:
        return 0
    con.executemany(
        """
        INSERT INTO prices_daily
            (ticker, date, open, high, low, close, adj_close, volume, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (ticker, date) DO UPDATE SET
            open = excluded.open,
            high = excluded.high,
            low = excluded.low,
            close = excluded.close,
            adj_close = excluded.adj_close,
            volume = excluded.volume,
            source = excluded.source
        """,
        [
            (r.ticker, r.date, r.open, r.high, r.low, r.close, r.adj_close, r.volume, r.source)
            for r in rows
        ],
    )
    return len(rows)


def tracked_tickers(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Universe + benchmarks + anything actually held in the account.

    Held non-universe tickers (from portfolio snapshots) get price history too so the
    dashboard can chart the real portfolio's performance.
    """
    held = [r[0] for r in con.execute("SELECT DISTINCT ticker FROM portfolio_snapshots").fetchall()]
    return sorted(set(all_tickers()) | set(held))


def incremental_start(
    con: duckdb.DuckDBPyConnection, years: int, today: date | None = None
) -> date:
    """Start date for a refresh: 7 days before the latest stored bar (idempotent overlap),
    or the full `years` window when the table is empty / `full` refreshes are wanted."""
    today = today or date.today()
    full_start = today - timedelta(days=int(years * 365.25) + 5)
    row = con.execute("SELECT max(date) FROM prices_daily").fetchone()
    if row and row[0]:
        latest: date = row[0]
        return max(full_start, latest - timedelta(days=7))
    return full_start


def collect_prices(
    con: duckdb.DuckDBPyConnection,
    *,
    years: int,
    tickers: list[str] | None = None,
    source: str = "yfinance",
    full: bool = False,
) -> int:
    """Fetch and upsert daily prices for the universe. Returns rows written.

    Incremental by default (last stored date minus a week); ``full=True`` refetches the
    whole window — use after adding tickers to the universe.

    Args:
        source: "yfinance" (default, no gateway needed) or "ibkr" (requires connection).
    """
    syms = tickers if tickers is not None else tracked_tickers(con)
    end = date.today()
    if full:
        start = end - timedelta(days=int(years * 365.25) + 5)
    else:
        start = incremental_start(con, years, end)

    with track_run(con, job="collect.prices") as run:
        if source == "ibkr":
            from moi.ingest.ibkr import ib_connection

            with ib_connection() as ib:
                rows = fetch_ibkr(ib, syms, years)
        else:
            rows = fetch_yfinance(syms, start, end)

        written = upsert_prices(con, rows)
        run.add_rows(written)
        run.detail = f"source={source} tickers={len(syms)} start={start}"
    log.info("collect_prices_done", rows=written, source=source, tickers=len(syms))
    return written
