"""Polymarket metadata/history parsing and end-to-end collection with mocked HTTP."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx

from moi.ingest.polymarket import (
    CLOB_HISTORY_URL,
    GAMMA_URL,
    collect_polymarket,
    parse_history,
    parse_market_metadata,
)

GAMMA_PAYLOAD = [
    {
        "question": "Fed cuts rates in September 2026?",
        "slug": "fed-sept-2026",
        "outcomes": json.dumps(["Yes", "No"]),
        "clobTokenIds": json.dumps(["tok-yes", "tok-no"]),
        "closed": False,
    }
]

HISTORY_PAYLOAD = {
    "history": [
        {"t": 1751155200, "p": 0.40},  # 2025-06-29 00:00 UTC
        {"t": 1751190000, "p": 0.42},  # 2025-06-29 09:40 UTC — later same day → wins
        {"t": 1751241600, "p": 0.45},  # 2025-06-30 00:00 UTC
    ]
}


def test_parse_market_metadata_picks_yes_token() -> None:
    meta = parse_market_metadata(GAMMA_PAYLOAD)
    assert meta is not None
    assert meta["token_id"] == "tok-yes"
    assert meta["closed"] is False
    assert parse_market_metadata([]) is None


def test_parse_history_daily_last_value() -> None:
    points = parse_history(HISTORY_PAYLOAD)
    assert len(points) == 2
    assert points[0][1] == 0.42  # last value of the first day
    assert points[1][1] == 0.45


@respx.mock
def test_collect_polymarket(db, tmp_path: Path) -> None:
    cfg = tmp_path / "polymarket.yaml"
    cfg.write_text("markets:\n  - {slug: fed-sept-2026, category: fed}\n")
    respx.get(GAMMA_URL).mock(return_value=httpx.Response(200, json=GAMMA_PAYLOAD))
    respx.get(CLOB_HISTORY_URL).mock(return_value=httpx.Response(200, json=HISTORY_PAYLOAD))

    written = collect_polymarket(db, config_path=cfg)
    assert written == 2
    # Idempotent re-run.
    collect_polymarket(db, config_path=cfg)
    assert db.execute("SELECT count(*) FROM polymarket_series").fetchone()[0] == 2
    q = db.execute("SELECT question FROM polymarket_markets WHERE slug='fed-sept-2026'").fetchone()
    assert "Fed" in q[0]
