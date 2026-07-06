"""FRED observation parsing and upsert."""

from __future__ import annotations

from datetime import date

from moi.ingest.macro import parse_observations, upsert_macro

PAYLOAD = {
    "observations": [
        {"date": "2026-06-01", "value": "4.25"},
        {"date": "2026-06-02", "value": "."},  # FRED gap marker
        {"date": "2026-06-03", "value": "4.30"},
    ]
}


def test_parse_observations_skips_gaps() -> None:
    points = parse_observations(PAYLOAD)
    assert points == [(date(2026, 6, 1), 4.25), (date(2026, 6, 3), 4.30)]


def test_upsert_macro_idempotent(db) -> None:
    points = parse_observations(PAYLOAD)
    upsert_macro(db, "FEDFUNDS", points)
    upsert_macro(db, "FEDFUNDS", points)
    assert db.execute("SELECT count(*) FROM macro_series").fetchone()[0] == 2
