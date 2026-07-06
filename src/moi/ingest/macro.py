"""FRED macro series collector (plain REST via httpx; needs a free FRED API key).

Series are configured in ``config/macro.yaml``. Skips gracefully without a key.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import duckdb
import httpx
import yaml

from moi.config import CONFIG_DIR, get_settings
from moi.logging import get_logger
from moi.runlog import track_run

log = get_logger(__name__)

FRED_URL = "https://api.stlouisfed.org/fred/series/observations"


def parse_observations(payload: dict[str, Any]) -> list[tuple[date, float]]:
    """Convert a FRED observations payload to (date, value) points, skipping '.' gaps."""
    points: list[tuple[date, float]] = []
    for obs in payload.get("observations", []):
        raw = obs.get("value")
        if raw in (None, "", "."):
            continue
        try:
            points.append((date.fromisoformat(obs["date"]), float(raw)))
        except (KeyError, ValueError):
            continue
    return points


def upsert_macro(
    con: duckdb.DuckDBPyConnection, series_id: str, points: list[tuple[date, float]]
) -> int:
    if not points:
        return 0
    con.executemany(
        """
        INSERT INTO macro_series (series_id, date, value) VALUES (?, ?, ?)
        ON CONFLICT (series_id, date) DO UPDATE SET value = excluded.value
        """,
        [(series_id, d, v) for d, v in points],
    )
    return len(points)


def collect_macro(con: duckdb.DuckDBPyConnection, config_path: Path | None = None) -> int:
    """Fetch all configured FRED series. Skips if MOI_FRED_API_KEY is unset."""
    api_key = get_settings().fred_api_key
    if not api_key:
        log.warning("macro_skipped", reason="no MOI_FRED_API_KEY configured")
        return 0

    cfg = yaml.safe_load((config_path or CONFIG_DIR / "macro.yaml").read_text()) or {}
    series = cfg.get("series", [])
    total = 0
    with track_run(con, job="collect.macro") as run:
        with httpx.Client(timeout=30) as client:
            for entry in series:
                sid = entry["id"]
                try:
                    resp = client.get(
                        FRED_URL,
                        params={
                            "series_id": sid,
                            "api_key": api_key,
                            "file_type": "json",
                            "observation_start": "2018-01-01",
                        },
                    )
                    resp.raise_for_status()
                    points = parse_observations(resp.json())
                    written = upsert_macro(con, sid, points)
                    total += written
                    log.info("macro_stored", series=sid, points=written)
                except Exception as exc:
                    log.warning("macro_failed", series=sid, error=str(exc))
        run.add_rows(total)
        run.detail = f"series={len(series)}"
    return total
