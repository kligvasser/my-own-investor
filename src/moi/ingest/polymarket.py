"""Polymarket collector (public Gamma + CLOB APIs, no auth).

For each slug in ``config/polymarket.yaml``: resolve market metadata and the YES-outcome
CLOB token via Gamma, then pull the daily price (= probability) history from the CLOB
prices-history endpoint. Stored as ``polymarket_markets`` + ``polymarket_series``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import httpx
import yaml

from moi.config import CONFIG_DIR
from moi.ingest import http
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


def parse_end_date(raw: object) -> datetime | None:
    """Gamma endDate (ISO, usually Z-suffixed) → naive UTC timestamp, or None."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def parse_market_metadata(payload: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Extract (question, yes_token_id, closed, end_date) from a Gamma /markets?slug=."""
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
        "end_date": parse_end_date(market.get("endDate")),
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
        INSERT INTO polymarket_markets
            (slug, question, category, token_id, closed, end_date, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (slug) DO UPDATE SET
            question = excluded.question,
            category = excluded.category,
            token_id = excluded.token_id,
            closed = excluded.closed,
            end_date = excluded.end_date,
            updated_at = excluded.updated_at
        """,
        [
            market.slug,
            meta.get("question"),
            market.category,
            meta.get("token_id"),
            meta.get("closed"),
            meta.get("end_date"),
            datetime.now(),
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


def _collect_market(
    con: duckdb.DuckDBPyConnection, client: httpx.Client, market: TrackedMarket
) -> int:
    """Refresh one market's metadata + probability series. Returns points written."""
    resp = client.get(GAMMA_URL, params={"slug": market.slug})
    resp.raise_for_status()
    meta = parse_market_metadata(resp.json())
    if meta is None:
        log.warning("polymarket_slug_not_found", slug=market.slug)
        return 0
    upsert_market(con, market, meta)
    if not meta.get("token_id"):
        log.warning("polymarket_no_token", slug=market.slug)
        return 0
    hist = client.get(
        CLOB_HISTORY_URL,
        params={
            "market": meta["token_id"],
            "interval": "max",
            "fidelity": 1440,  # daily resolution
        },
    )
    hist.raise_for_status()
    written = upsert_series(con, market.slug, parse_history(hist.json()))
    log.info("polymarket_stored", slug=market.slug, points=written)
    return written


def collect_polymarket(con: duckdb.DuckDBPyConnection, config_path: Path | None = None) -> int:
    """Refresh metadata and daily probability series for all tracked markets."""
    markets = load_tracked_markets(config_path)
    total = 0
    with track_run(con, job="collect.polymarket") as run:
        with http.client(timeout=30) as client:
            for market in markets:
                try:
                    total += _collect_market(con, client, market)
                except Exception as exc:
                    run.add_failures()
                    log.warning("polymarket_failed", slug=market.slug, error=str(exc))
        run.add_rows(total)
        run.detail = f"markets={len(markets)}"
    return total


def collect_single_market(con: duckdb.DuckDBPyConnection, slug: str, category: str) -> int:
    """Fetch one market immediately (dashboard 'Add market' path). Raises on failure."""
    with http.client(timeout=30) as client:
        return _collect_market(con, client, TrackedMarket(slug=slug, category=category))


# --------------------------------------------------------------------------- #
# Market discovery + config management (dashboard "Manage tracked markets")
# --------------------------------------------------------------------------- #
SEARCH_URL = "https://gamma-api.polymarket.com/public-search"

_SLUG_RE = re.compile(r"^[a-z0-9-]+$")
_CATEGORY_RE = re.compile(r"^[a-z0-9_-]{1,24}$")


def search_markets(query: str, *, limit: int = 12) -> list[dict[str, Any]]:
    """Search Gamma for OPEN markets; returns dicts sorted by volume (desc).

    Note: search sometimes returns a canonical slug that does not resolve on
    /markets?slug= — validation happens at add time (`slug_resolves`).
    """
    headers = {"Accept-Encoding": "gzip", "User-Agent": "moi/0.1"}  # brotli workaround
    with http.client(timeout=20, headers=headers) as client:
        resp = client.get(SEARCH_URL, params={"q": query, "limit_per_type": 8})
        resp.raise_for_status()
        events = resp.json().get("events") or []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ev in events:
        for m in ev.get("markets") or []:
            slug = m.get("slug")
            if not slug or slug in seen or m.get("closed"):
                continue
            seen.add(slug)
            out.append(
                {
                    "slug": slug,
                    "question": m.get("question"),
                    "volume": float(m.get("volumeNum") or 0),
                    "end_date": parse_end_date(m.get("endDate")),
                }
            )
    out.sort(key=lambda r: -r["volume"])
    return out[:limit]


def slug_resolves(slug: str) -> bool:
    """Does /markets?slug= return this market? (search slugs are not always canonical)."""
    headers = {"Accept-Encoding": "gzip", "User-Agent": "moi/0.1"}
    with http.client(timeout=20, headers=headers) as client:
        resp = client.get(GAMMA_URL, params={"slug": slug})
        resp.raise_for_status()
        return bool(resp.json())


def config_slugs(path: Path | None = None) -> set[str]:
    return {m.slug for m in load_tracked_markets(path)}


def add_market_to_config(
    slug: str,
    category: str,
    path: Path | None = None,
    validate: Callable[[str], bool] | None = None,
) -> None:
    """Append a market to config/polymarket.yaml (text-level edit preserves comments).

    Validates the slug against Gamma first — a search result's canonical slug does not
    always resolve; date-suffixed slugs do. Raises ValueError on any problem.
    """
    path = path or CONFIG_DIR / "polymarket.yaml"
    if not _SLUG_RE.match(slug):
        raise ValueError(f"invalid slug: {slug!r}")
    if not _CATEGORY_RE.match(category):
        raise ValueError(f"invalid category: {category!r} (lowercase letters/digits/-/_)")
    if slug in config_slugs(path):
        raise ValueError(f"{slug} is already tracked")
    if not (validate or slug_resolves)(slug):
        raise ValueError(
            f"{slug} does not resolve on the Gamma markets endpoint — open the market "
            "on polymarket.com and copy the full slug from the URL (often date-suffixed)"
        )
    text = path.read_text().rstrip("\n")
    # `markets:` is the last top-level key, so appending keeps valid YAML.
    path.write_text(f'{text}\n  - {{slug: "{slug}", category: {category}}}\n')
    log.info("polymarket_market_added", slug=slug, category=category)


def remove_market_from_config(slug: str, path: Path | None = None) -> bool:
    """Drop a market's line from the config. Collected history stays in the DB."""
    path = path or CONFIG_DIR / "polymarket.yaml"
    lines = path.read_text().splitlines(keepends=True)
    kept = [ln for ln in lines if f'"{slug}"' not in ln]
    if len(kept) == len(lines):
        return False
    path.write_text("".join(kept))
    log.info("polymarket_market_removed", slug=slug)
    return True
