"""my-own-investor dashboard (Streamlit) — entry point and navigation.

Read-mostly views over the DuckDB store; the only writes are approval-queue
decisions and the kill switch. Pipeline work is never run in-process: Mission
control launches `moi` CLI subprocesses (DuckDB is single-writer).

Run: `moi dashboard`  (or `streamlit run dashboard/app.py`)
"""

from __future__ import annotations

import contextlib

import streamlit as st

st.set_page_config(page_title="my-own-investor", page_icon="📈", layout="wide")

import mission  # noqa: E402
import views  # noqa: E402
from common import DBBusy, DBMissing, execute_write, q  # noqa: E402


def _sidebar() -> None:
    from moi.execute.executor import KILL_FILE, set_kill_file, set_kill_switch

    kill_on = KILL_FILE.exists()
    db_readable = True
    try:
        kill = q("SELECT value FROM controls WHERE key = 'kill_switch'")
        kill_on = kill_on or (not kill.empty and kill.iloc[0, 0] == "on")
    except DBBusy:
        db_readable = False
        st.sidebar.caption("⏳ database busy — job running")
    except DBMissing:
        db_readable = False

    if kill_on:
        st.sidebar.error("KILL SWITCH ON — trading blocked")
    if st.sidebar.button("Kill switch " + ("OFF" if kill_on else "ON")):
        # The file sentinel always works; the DB flag follows when unlocked.
        set_kill_file(not kill_on)
        if db_readable:
            with contextlib.suppress(DBBusy):
                execute_write(lambda con: set_kill_switch(con, not kill_on))
        st.rerun()

    _trading_lock_widget()
    st.sidebar.caption("Model output — not financial advice.")


def _trading_lock_widget() -> None:
    """Arming window for live execution: unlock with the key from .env, auto-relocks."""
    from datetime import datetime

    from moi.config import get_settings
    from moi.execute.executor import (
        SafetyError,
        lock_trading,
        trading_unlocked_until,
        unlock_trading,
    )

    if not get_settings().trading_unlock_key:
        return  # feature not configured — executor behaves as before
    until = trading_unlocked_until()
    if until:
        minutes = max(0, int((until - datetime.now()).total_seconds() // 60))
        st.sidebar.warning(f"🔓 Trading UNLOCKED — {minutes} min left")
        if st.sidebar.button("Relock now"):
            lock_trading()
            st.rerun()
    else:
        with st.sidebar.expander("🔒 Trading locked — unlock"):
            key = st.text_input("Unlock key", type="password", key="unlock_key")
            if st.button("Unlock", key="unlock_btn") and key:
                try:
                    unlock_trading(key)
                    st.rerun()
                except SafetyError:
                    st.error("Invalid key.")


nav = st.navigation(
    {
        "Operate": [
            st.Page(
                mission.render, title="Mission control", icon="🎛️", url_path="mission", default=True
            ),
            st.Page(views.weekly_report, title="Weekly report", icon="📄", url_path="report"),
            st.Page(views.approval_queue, title="Approval queue", icon="✅", url_path="queue"),
        ],
        "My money": [
            st.Page(views.portfolio, title="Portfolio", icon="💼", url_path="portfolio"),
            st.Page(views.xray, title="Holdings X-ray", icon="🔬", url_path="xray"),
            st.Page(views.journal, title="Journal", icon="📓", url_path="journal"),
        ],
        "Research": [
            st.Page(views.candidates, title="Candidates", icon="🎯", url_path="candidates"),
            st.Page(views.whales, title="Whales", icon="🐋", url_path="whales"),
            st.Page(views.trends, title="Trends", icon="📊", url_path="trends"),
            st.Page(views.model_health, title="Model health", icon="🧠", url_path="model"),
        ],
    }
)
_sidebar()
nav.run()
