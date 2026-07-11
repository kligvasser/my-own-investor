"""Shared dashboard helpers: short-lived DB access, busy handling, formatting.

DuckDB is single-writer: while a pipeline job (subprocess) holds the write lock,
even read-only connections fail. Pages raise :class:`DBBusy` and render a calm
notice instead of a stack trace.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps

import duckdb
import pandas as pd
import streamlit as st

from moi.config import get_settings


class DBBusy(Exception):
    """The database is locked by a running pipeline job."""


class DBMissing(Exception):
    """The database file does not exist yet (fresh install)."""


@contextmanager
def read_connection() -> Iterator[duckdb.DuckDBPyConnection]:
    db_path = get_settings().db_path
    if not db_path.exists():
        raise DBMissing(str(db_path))
    try:
        con = duckdb.connect(str(db_path), read_only=True)
    except duckdb.Error as exc:
        raise DBBusy(str(exc)) from exc
    try:
        yield con
    finally:
        con.close()


def q(sql: str, params: list | None = None) -> pd.DataFrame:
    with read_connection() as con:
        return con.execute(sql, params or []).df()


def execute_write(fn: Callable[[duckdb.DuckDBPyConnection], None]) -> None:
    from moi.db import connect

    try:
        con = connect()
    except duckdb.Error as exc:
        raise DBBusy(str(exc)) from exc
    try:
        fn(con)
    finally:
        con.close()


def busy_note() -> None:
    st.info(
        "⏳ A pipeline job is holding the database — this view loads again when it "
        "finishes. Watch progress on **Mission control**."
    )


def missing_note() -> None:
    st.info("No database yet — run `moi db init`, then **Collect data** on Mission control.")


def page(fn: Callable[[], None]) -> Callable[[], None]:
    """Wrap a page renderer so a locked or missing database degrades to a notice."""

    @wraps(fn)
    def wrapper() -> None:
        try:
            fn()
        except DBBusy:
            busy_note()
        except DBMissing:
            missing_note()

    return wrapper
