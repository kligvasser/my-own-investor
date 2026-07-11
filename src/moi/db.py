"""DuckDB access layer with ordered SQL migrations.

Usage::

    from moi.db import connect, migrate
    con = connect()          # opens data/moi.duckdb, applies pending migrations
    con.execute("SELECT ...")

Migrations are plain ``.sql`` files in ``moi/migrations/`` named ``NNN_name.sql``.
They are applied in numeric order and recorded in ``schema_version`` so each runs once.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import duckdb

from moi.config import get_settings
from moi.logging import get_logger

log = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_MIGRATION_RE = re.compile(r"^(\d+)_.+\.sql$")


def _discover_migrations() -> list[tuple[int, Path]]:
    found: list[tuple[int, Path]] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        m = _MIGRATION_RE.match(path.name)
        if not m:
            continue
        found.append((int(m.group(1)), path))
    found.sort(key=lambda t: t[0])
    return found


def _ensure_version_table(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version    INTEGER NOT NULL PRIMARY KEY,
            name       VARCHAR NOT NULL,
            applied_at TIMESTAMP NOT NULL DEFAULT now()
        )
        """
    )


def _applied_versions(con: duckdb.DuckDBPyConnection) -> set[int]:
    rows = con.execute("SELECT version FROM schema_version").fetchall()
    return {int(r[0]) for r in rows}


def migrate(con: duckdb.DuckDBPyConnection) -> int:
    """Apply all pending migrations. Returns the number applied."""
    _ensure_version_table(con)
    applied = _applied_versions(con)
    count = 0
    for version, path in _discover_migrations():
        if version in applied:
            continue
        log.info("applying_migration", version=version, file=path.name)
        con.execute("BEGIN")
        try:
            con.execute(path.read_text())
            con.execute(
                "INSERT INTO schema_version (version, name) VALUES (?, ?)",
                [version, path.stem],
            )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        count += 1
    return count


def scalar(con: duckdb.DuckDBPyConnection, sql: str, params: list[object] | None = None) -> Any:
    """Execute a query and return the first column of the first row (or None)."""
    row = con.execute(sql, params or []).fetchone()
    return row[0] if row else None


def connect(
    db_path: Path | None = None,
    *,
    read_only: bool = False,
    wait_lock_seconds: float = 0,
) -> duckdb.DuckDBPyConnection:
    """Open (creating if needed) the DuckDB database and apply pending migrations.

    Args:
        db_path: Override the configured DB path (used by tests).
        read_only: Open read-only (skips migration; DB must already exist).
        wait_lock_seconds: DuckDB is single-writer; scheduled jobs pass a budget here
            to wait for another process (e.g. a dashboard-launched run) to release the
            lock instead of dying instantly. 0 = fail fast (interactive commands).
    """
    import time

    path = db_path or get_settings().db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait_lock_seconds
    while True:
        try:
            con = duckdb.connect(str(path), read_only=read_only)
            break
        except duckdb.IOException:
            if time.monotonic() >= deadline:
                raise
            log.warning("db_locked_waiting", path=str(path))
            time.sleep(15)
    if not read_only:
        migrate(con)
    return con
