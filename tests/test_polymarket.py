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


def test_parse_end_date_variants() -> None:
    from datetime import datetime

    from moi.ingest.polymarket import parse_end_date

    assert parse_end_date("2026-07-31T12:00:00Z") == datetime(2026, 7, 31, 12, 0)
    assert parse_end_date(None) is None
    assert parse_end_date("not-a-date") is None


def test_add_and_remove_market_preserves_config(tmp_path) -> None:
    from moi.ingest.polymarket import (
        add_market_to_config,
        config_slugs,
        load_tracked_markets,
        remove_market_from_config,
    )

    cfg = tmp_path / "polymarket.yaml"
    cfg.write_text(
        '# my comment must survive\nmarkets:\n  - {slug: "existing-market", category: fed}\n'
    )
    add_market_to_config("new-market-aug-31", "energy", cfg, validate=lambda s: True)
    markets = {m.slug: m.category for m in load_tracked_markets(cfg)}
    assert markets == {"existing-market": "fed", "new-market-aug-31": "energy"}
    assert "# my comment must survive" in cfg.read_text()

    # Duplicate, bad slug, bad category, unresolvable slug → all refused.
    import pytest as _pytest

    with _pytest.raises(ValueError, match="already tracked"):
        add_market_to_config("new-market-aug-31", "energy", cfg, validate=lambda s: True)
    with _pytest.raises(ValueError, match="invalid slug"):
        add_market_to_config('bad"slug', "energy", cfg, validate=lambda s: True)
    with _pytest.raises(ValueError, match="invalid category"):
        add_market_to_config("ok-slug", 'x: "y"', cfg, validate=lambda s: True)
    with _pytest.raises(ValueError, match="does not resolve"):
        add_market_to_config("ghost-slug", "energy", cfg, validate=lambda s: False)

    assert remove_market_from_config("new-market-aug-31", cfg)
    assert config_slugs(cfg) == {"existing-market"}
    assert not remove_market_from_config("never-there", cfg)


def test_market_rotation_alerts(db) -> None:
    from datetime import datetime, timedelta

    from moi.orchestrator.watch import market_rotation_alerts

    db.execute(
        """INSERT INTO polymarket_markets (slug, question, category, closed, end_date, updated_at)
           VALUES ('dead-market', 'Old question?', 'fed', TRUE, NULL, now()),
                  ('expiring-market', 'Ends soon?', 'compute', FALSE, ?, now()),
                  ('healthy-market', 'Fine?', 'macro', FALSE, ?, now()),
                  ('untracked-market', 'Not ours', 'ai', TRUE, NULL, now())""",
        [datetime.now() + timedelta(days=1), datetime.now() + timedelta(days=60)],
    )
    tracked = {"dead-market", "expiring-market", "healthy-market"}
    alerts = market_rotation_alerts(db, tracked=tracked)
    keys = {a.key for a in alerts}
    assert keys == {"closed:dead-market", "expiring:expiring-market"}
    assert all(a.kind == "market_rotation" for a in alerts)
