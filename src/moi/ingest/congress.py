"""Congressional trade collector (STOCK Act disclosures).

Two provider adapters — Quiver Quantitative and Unusual Whales — behind a common
interface; the one with an API key configured is used (Quiver preferred if both).
With no key configured the collector logs and skips gracefully.

Both transaction date and disclosure date are stored: the 30-45 day disclosure lag is
itself a feature (PLAN §5), never to be hidden.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

import duckdb
import httpx

from moi.config import get_settings
from moi.logging import get_logger
from moi.runlog import track_run

log = get_logger(__name__)


@dataclass(frozen=True)
class CongressTrade:
    politician: str
    chamber: str | None
    ticker: str | None
    direction: str | None  # buy | sell
    amount_range: str | None
    tx_date: date | None
    disclosure_date: date | None
    source: str

    @property
    def tx_id(self) -> str:
        raw = "|".join(
            str(x)
            for x in (
                self.politician, self.ticker, self.direction,
                self.amount_range, self.tx_date, self.source,
            )
        )
        return hashlib.sha1(raw.encode()).hexdigest()[:16]


def _to_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _direction(value: Any) -> str | None:
    s = str(value or "").lower()
    if "purchase" in s or s == "buy":
        return "buy"
    if "sale" in s or s == "sell":
        return "sell"
    return s or None


class CongressProvider(Protocol):
    name: str

    def fetch(self, client: httpx.Client) -> list[CongressTrade]: ...


class QuiverProvider:
    """Quiver Quantitative bulk congress-trading endpoint."""

    name = "quiver"
    url = "https://api.quiverquant.com/beta/bulk/congresstrading"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def fetch(self, client: httpx.Client) -> list[CongressTrade]:
        resp = client.get(
            self.url,
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
        )
        resp.raise_for_status()
        return [self.parse_row(row) for row in resp.json()]

    @staticmethod
    def parse_row(row: dict[str, Any]) -> CongressTrade:
        return CongressTrade(
            politician=str(row.get("Representative") or row.get("Name") or "unknown"),
            chamber=(str(row["House"]).lower() if row.get("House") else None),
            ticker=(str(row["Ticker"]).upper() if row.get("Ticker") else None),
            direction=_direction(row.get("Transaction")),
            amount_range=row.get("Range") or row.get("Amount"),
            tx_date=_to_date(row.get("TransactionDate")),
            disclosure_date=_to_date(row.get("ReportDate") or row.get("Date")),
            source="quiver",
        )


class UnusualWhalesProvider:
    """Unusual Whales congress trades endpoint."""

    name = "unusualwhales"
    url = "https://api.unusualwhales.com/api/congress/congress-trader"

    def __init__(self, api_key: str) -> None:
        self.api_key = api_key

    def fetch(self, client: httpx.Client) -> list[CongressTrade]:
        resp = client.get(
            self.url,
            headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
        )
        resp.raise_for_status()
        payload = resp.json()
        rows = payload.get("data", payload) if isinstance(payload, dict) else payload
        return [self.parse_row(row) for row in rows]

    @staticmethod
    def parse_row(row: dict[str, Any]) -> CongressTrade:
        return CongressTrade(
            politician=str(row.get("reporter") or row.get("name") or "unknown"),
            chamber=(str(row["chamber"]).lower() if row.get("chamber") else None),
            ticker=(str(row["ticker"]).upper() if row.get("ticker") else None),
            direction=_direction(row.get("txn_type") or row.get("transaction_type")),
            amount_range=row.get("amounts") or row.get("amount_range"),
            tx_date=_to_date(row.get("transaction_date") or row.get("txn_date")),
            disclosure_date=_to_date(row.get("disclosure_date") or row.get("filed_at")),
            source="unusualwhales",
        )


def make_provider() -> CongressProvider | None:
    settings = get_settings()
    if settings.quiver_api_key:
        return QuiverProvider(settings.quiver_api_key)
    if settings.unusualwhales_api_key:
        return UnusualWhalesProvider(settings.unusualwhales_api_key)
    return None


def upsert_trades(con: duckdb.DuckDBPyConnection, trades: list[CongressTrade]) -> int:
    if not trades:
        return 0
    con.executemany(
        """
        INSERT INTO congress_trades
            (tx_id, politician, chamber, ticker, direction, amount_range,
             tx_date, disclosure_date, source)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (tx_id) DO UPDATE SET
            disclosure_date = excluded.disclosure_date
        """,
        [
            (
                t.tx_id, t.politician, t.chamber, t.ticker, t.direction,
                t.amount_range, t.tx_date, t.disclosure_date, t.source,
            )
            for t in trades
        ],
    )
    return len(trades)


def collect_congress(con: duckdb.DuckDBPyConnection) -> int:
    """Fetch congress trades from the configured provider. Skips if no API key is set."""
    provider = make_provider()
    if provider is None:
        log.warning("congress_skipped", reason="no API key configured (MOI_QUIVER_API_KEY)")
        return 0
    with track_run(con, job="collect.congress") as run:
        with httpx.Client(timeout=60) as client:
            trades = provider.fetch(client)
        written = upsert_trades(con, trades)
        run.add_rows(written)
        run.detail = f"provider={provider.name}"
    log.info("congress_done", provider=provider.name, rows=written)
    return written
