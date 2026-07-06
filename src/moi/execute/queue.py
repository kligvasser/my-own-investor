"""Approval-queue actions: the only way a suggestion can become executable."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import duckdb

from moi.logging import get_logger

log = get_logger(__name__)

DECISIONS = {"APPROVED", "REJECTED", "SNOOZED"}


def decide(con: duckdb.DuckDBPyConnection, suggestion_id: str, decision: str) -> bool:
    """Transition a PENDING suggestion. Returns False if not found or not pending."""
    decision = decision.upper()
    if decision not in DECISIONS:
        raise ValueError(f"Invalid decision {decision!r}; must be one of {sorted(DECISIONS)}")
    row = con.execute("SELECT status FROM suggestions WHERE id = ?", [suggestion_id]).fetchone()
    if row is None or row[0] not in ("PENDING", "SNOOZED"):
        return False
    con.execute(
        "UPDATE suggestions SET status = ?, decided_at = ? WHERE id = ?",
        [decision, datetime.now(), suggestion_id],
    )
    log.info("suggestion_decided", id=suggestion_id, decision=decision)
    return True


def pending(con: duckdb.DuckDBPyConnection) -> list[tuple[Any, ...]]:
    return con.execute(
        """SELECT id, week_end, action, ticker, current_weight, target_weight,
                  score, thesis, bear_case, confidence
           FROM suggestions WHERE status = 'PENDING' ORDER BY created_at DESC"""
    ).fetchall()


def approved_unexecuted(con: duckdb.DuckDBPyConnection) -> list[tuple[Any, ...]]:
    """Approved suggestions with no non-error order yet — the executor's work list."""
    return con.execute(
        """SELECT s.id, s.ticker, s.action, s.current_weight, s.target_weight
           FROM suggestions s
           WHERE s.status = 'APPROVED'
             AND NOT EXISTS (
                 SELECT 1 FROM orders o
                 WHERE o.suggestion_id = s.id AND o.status != 'error'
             )
           ORDER BY s.created_at"""
    ).fetchall()
