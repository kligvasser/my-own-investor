"""Mission control: health at a glance, one-click pipeline commands, connections,
data-source board, and run history."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from common import DBBusy, DBMissing, busy_note, missing_note, q

from moi import ops
from moi.config import ROOT

_DOT = {"ok": ":green[●]", "warn": ":orange[●]", "off": ":gray[●]", "error": ":red[●]"}
_FRESH_ICON = {"ok": "🟢", "stale": "🔴", "empty": "🔴", "skipped": "⚪"}


def render() -> None:
    st.title("Mission control")
    _health_row()
    st.divider()
    _command_deck()
    st.divider()
    left, right = st.columns([2, 3], gap="large")
    with left:
        _connections()
    with right:
        _source_board()
        _run_history()


# --------------------------------------------------------------------------- #
# Health strip
# --------------------------------------------------------------------------- #
def _latest_report() -> Path | None:
    reports = sorted((ROOT / "reports").glob("*.md"), reverse=True)
    return reports[0] if reports else None


def _health_row() -> None:
    c1, c2, c3, c4, c5 = st.columns(5)

    report = _latest_report()
    if report:
        age = (datetime.now() - datetime.fromtimestamp(report.stat().st_mtime)).days
        c1.metric("Latest report", report.stem, f"{age}d old", delta_color="off")
    else:
        c1.metric("Latest report", "none")

    try:
        pending = int(q("SELECT count(*) AS n FROM suggestions WHERE status = 'PENDING'")["n"][0])
        ready = int(
            q(
                """SELECT count(*) AS n FROM suggestions s WHERE s.status = 'APPROVED'
                   AND NOT EXISTS (SELECT 1 FROM orders o
                                   WHERE o.suggestion_id = s.id AND o.status != 'error')"""
            )["n"][0]
        )
        snap = q("SELECT max(taken_at) AS t FROM portfolio_snapshots")["t"][0]
        fresh = q_freshness_counts()
        c2.metric("Pending suggestions", pending)
        c3.metric("Approved, unexecuted", ready)
        if pd.notna(snap):
            c4.metric("Account snapshot", f"{(datetime.now() - snap).days}d ago")
        else:
            c4.metric("Account snapshot", "never")
        c5.metric("Data sources fresh", f"{fresh[0]}/{fresh[1]}")
    except DBBusy:
        c2.metric("Database", "busy")
    except DBMissing:
        c2.metric("Database", "not created")


def q_freshness_counts() -> tuple[int, int]:
    from common import read_connection

    from moi.ingest.quality import check_freshness

    with read_connection() as con:
        states = [t.state for t in check_freshness(con)]
    return sum(s == "ok" for s in states), len(states)


# --------------------------------------------------------------------------- #
# Command deck
# --------------------------------------------------------------------------- #
def _command_deck() -> None:
    st.subheader("Commands")
    info = ops.current_job()

    if info and info["running"]:
        spec = ops.JOBS[info["key"]]
        st.info(
            f"**{spec.label}** running (`moi {' '.join(spec.args)}`, started {info['started']})"
        )
        _live_log(Path(info["log"]))
        c1, c2 = st.columns([1, 4])
        if c1.button("Cancel job"):
            ops.cancel_job()
            st.rerun()
        with c2.expander("Job stuck? (e.g. stale after a reboot)"):
            if st.button("Force clear tracker", help="Clears tracking only — kills nothing."):
                ops.clear_job()
                st.rerun()
        return

    if info:  # finished, awaiting dismissal
        spec = ops.JOBS[info["key"]]
        tail = ops.tail_log(Path(info["log"]))
        rc = ops.job_exit_code(info)
        if rc == 0:
            st.success(f"**{spec.label}** finished OK.")
        elif info["key"] == "watch" and rc == 1:
            st.warning(f"**{spec.label}** finished — urgent alerts found (see log).")
        elif rc is None:
            st.warning(f"**{spec.label}** finished — exit status unknown, check the log.")
        else:
            st.error(f"**{spec.label}** failed (exit {rc}) — check the log.")
        with st.expander("Job log", expanded=rc != 0):
            st.code(tail or "(empty)", language="text")
        if st.button("Dismiss", type="primary"):
            ops.clear_job()
            st.rerun()
        return

    def _launch(key: str) -> None:
        try:
            ops.start_job(key)
        except ops.JobBlocked as exc:
            st.session_state["job_blocked_msg"] = str(exc)
        st.rerun()

    if msg := st.session_state.pop("job_blocked_msg", None):
        st.warning(f"Not started: {msg}")

    order = ["run", "collect", "report", "report-fast"]
    cols = st.columns(4)
    for col, key in zip(cols, order, strict=True):
        spec = ops.JOBS[key]
        with col:
            if st.button(
                spec.label, type="primary" if key == "run" else "secondary", width="stretch"
            ):
                _launch(key)
            st.caption(spec.blurb)

    utility = ["watch", "orders-sync", "ibkr-ping", "ml-train"]
    cols = st.columns(4)
    for col, key in zip(cols, utility, strict=True):
        spec = ops.JOBS[key]
        with col:
            if st.button(spec.label, width="stretch"):
                _launch(key)
            st.caption(spec.blurb)

    st.caption(
        "One job at a time — the database is single-writer. Jobs keep running even if "
        "you close this page; logs land in `data/joblogs/` (last 30 kept)."
    )


@st.fragment(run_every="2.5s")
def _live_log(log_path: Path) -> None:
    st.code(ops.tail_log(log_path) or "starting…", language="text")
    cur = ops.current_job()
    if not (cur and cur["running"]):
        st.rerun(scope="app")


# --------------------------------------------------------------------------- #
# Connections / sources / history
# --------------------------------------------------------------------------- #
def _connections() -> None:
    st.subheader("Connections")
    for check in ops.connection_checks():
        dot = _DOT.get(check.state, ":gray[●]")
        st.markdown(f"{dot} **{check.name}** — {check.detail}")


def _source_board() -> None:
    st.subheader("Data sources")
    try:
        from common import read_connection

        with read_connection() as con:
            rows = ops.source_board(con)
    except DBBusy:
        busy_note()
        return
    except DBMissing:
        missing_note()
        return
    board = pd.DataFrame(rows)
    board["freshness"] = board["freshness"].map(lambda s: f"{_FRESH_ICON.get(s, '⚪')} {s}")
    st.dataframe(board, width="stretch", hide_index=True)


def _run_history() -> None:
    st.subheader("Recent runs")
    try:
        runs = q(
            """SELECT started_at, job, status, rows_written, detail
               FROM run_log ORDER BY started_at DESC LIMIT 12"""
        )
    except (DBBusy, DBMissing):
        return
    if runs.empty:
        st.caption("No pipeline runs recorded yet.")
        return
    runs["status"] = runs["status"].map(
        lambda s: {
            "ok": "✅ ok",
            "partial": "🔶 partial",
            "error": "❌ error",
            "running": "🔄 running",
        }.get(s, s)
    )
    st.dataframe(runs, width="stretch", hide_index=True)
