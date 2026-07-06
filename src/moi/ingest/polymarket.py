"""Polymarket collector (public Gamma + CLOB APIs, no auth).

For each slug in ``config/polymarket.yaml``: resolve market metadata and the YES-outcome
CLOB token via Gamma, then pull the daily price (= probability) history from the CLOB
prices-history endpoint. Stored as ``polymarket_markets`` + ``polymarket_series``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import httpx
import yaml

from moi.config import CONFIG_DIR
from moi.logging import get_logger
from moi.runlog import track_run

log = get_logger(__name__)

GAMMA_URL = "https://gamma-api.polymarket.com/markets"
CLOB_HISTORY_URL = "https://clob.polymarket.com/prices-history"


@dataclass(frozen=True)
class TrackedMarket:
    slug: str
    category: str


def load_tracked_markets(path: Path | None = None) -> list[TrackedMarket]:
    data = yaml.safe_load((path or CONFIG_DIR / "polymarket.yaml").read_text()) or {}
    return [
        TrackedMarket(slug=m["slug"], category=m.get("category", "other"))
        for m in data.get("markets", [])
    ]


def parse_market_metadata(payload: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Extract (question, yes_token_id, closed) from a Gamma /markets?slug= response."""
    if not payload:
        return None
    market = payload[0]
    token_ids_raw = market.get("clobTokenIds")
    outcomes_raw = market.get("outcomes")
    token_ids = (
        json.loads(token_ids_raw) if isinstance(token_ids_raw, str) else (token_ids_raw or [])
    )
    outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else (outcomes_raw or [])

    yes_token: str | None = None
    if token_ids:
        yes_token = token_ids[0]
        for outcome, token in zip(outcomes, token_ids, strict=False):
            if str(outcome).lower() == "yes":
                yes_token = token
                break
    return {
        "question": market.get("question"),
        "token_id": yes_token,
        "closed": bool(market.get("closed", False)),
    }


def parse_history(payload: dict[str, Any]) -> list[tuple[Any, float]]:
    """Convert a CLOB prices-history payload to daily (date, prob) points (last per day)."""
    points: dict[Any, float] = {}
    for item in payload.get("history", []):
        ts, price = item.get("t"), item.get("p")
        if ts is None or price is None:
            continue
        day = datetime.fromtimestamp(int(ts), tz=UTC).date()
        points[day] = float(price)  # later timestamps overwrite → last value of the day
    return sorted(points.items())


def upsert_market(
    con: duckdb.DuckDBPyConnection, market: TrackedMarket, meta: dict[str, Any]
) -> None:
    con.execute(
        """
        INSERT INTO polymarket_markets (slug, question, category, token_id, closed, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (slug) DO UPDATE SET
            question = excluded.question,
            category = excluded.category,
            token_id = excluded.token_id,
            closed = excluded.closed,
            updated_at = excluded.updated_at
        """,
        [
            market.slug, meta.get("question"), market.category,
            meta.get("token_id"), meta.get("closed"), datetime.now(),
        ],
    )


def upsert_series(
    con: duckdb.DuckDBPyConnection, slug: str, points: list[tuple[Any, float]]
) -> int:
    if not points:
        return 0
    con.executemany(
        """
        INSERT INTO polymarket_series (slug, ts, prob) VALUES (?, ?, ?)
        ON CONFLICT (slug, ts) DO UPDATE SET prob = excluded.prob
        """,
        [(slug, day, prob) for day, prob in points],
    )
    return len(points)


def collect_polymarket(
    con: duckdb.DuckDBPyConnection, config_path: Path | None = None
) -> int:
    """Refresh metadata and daily probability series for all tracked markets."""
    markets = load_tracked_markets(config_path)
    total = 0
    with track_run(con, job="collect.polymarket") as run:
        with httpx.Client(timeout=30) as client:
            for market in markets:
                try:
                    resp = client.get(GAMMA_URL, params={"slug": market.slug})
                    resp.raise_for_status()
                    meta = parse_market_metadata(resp.json())
                    if meta is None:
                        log.warning("polymarket_slug_not_found", slug=market.slug)
                        continue
                    upsert_market(con, market, meta)
                    if not meta.get("token_id"):
                        log.warning("polymarket_no_token", slug=market.slug)
                        continue
                    hist = client.get(
                        CLOB_HISTORY_URL,
                        params={
                            "market": meta["token_id"],
                            "interval": "max",
                            "fidelity": 1440,  # daily resolution
                        },
                    )
                    hist.raise_for_status()
                    points = parse_history(hist.json())
                    written = upsert_series(con, market.slug, points)
                    total += written
                    log.info("polymarket_stored", slug=market.slug, points=written)
                except Exception as exc:
                    log.warning("polymarket_failed", slug=market.slug, error=str(exc))
        run.add_rows(total)
        run.detail = f"markets={len(markets)}"
    return total
