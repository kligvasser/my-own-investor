"""Operational status checks and background-job control for the dashboard.

Everything here is UI-agnostic so it can be unit-tested: the Streamlit Mission
Control page is a thin renderer over these functions.

Jobs run as detached ``python -m moi …`` subprocesses (DuckDB is single-writer,
so the dashboard never runs pipeline code in-process). The current job is
tracked in a small JSON file so a dashboard restart can re-attach to it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from moi.config import DATA_DIR, ROOT, get_settings
from moi.ingest.quality import check_freshness

JOB_LOG_DIR = DATA_DIR / "joblogs"
CURRENT_JOB_FILE = JOB_LOG_DIR / "current-job.json"

LAUNCHD_PLISTS = {
    "nightly collect 22:00": Path.home() / "Library/LaunchAgents/com.moi.collect.plist",
    "weekly report Sat 09:00": Path.home() / "Library/LaunchAgents/com.moi.weekly.plist",
}

# Which collector job (run_log.job) feeds which freshness-checked table.
TABLE_TO_JOB = {
    "prices_daily": "collect.prices",
    "filings_13f": "collect.13f",
    "insider_form4": "collect.form4",
    "congress_trades": "collect.congress",
    "polymarket_series": "collect.polymarket",
    "news_items": "collect.news",
    "macro_series": "collect.macro",
}


# --------------------------------------------------------------------------- #
# Runnable jobs
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class JobSpec:
    key: str
    label: str
    args: tuple[str, ...]  # `moi` CLI arguments
    blurb: str


JOBS: dict[str, JobSpec] = {
    spec.key: spec
    for spec in [
        JobSpec(
            "run",
            "Full pipeline",
            ("run",),
            "Collect all data → report + suggestions → urgent triggers.",
        ),
        JobSpec(
            "collect",
            "Collect data",
            ("collect", "all"),
            "Refresh every source: prices, 13F, insiders, congress, Polymarket, news, macro.",
        ),
        JobSpec(
            "report",
            "Report (AI)",
            ("weekly",),
            "Report + suggestions from existing data, with agent narration.",
        ),
        JobSpec(
            "report-fast",
            "Report (fast)",
            ("weekly", "--no-llm"),
            "Numbers-only report in ~30s — no agent narration.",
        ),
        JobSpec(
            "watch",
            "Urgent triggers",
            ("watch",),
            "Big moves, fresh whale filings, data-quality alarms.",
        ),
        JobSpec(
            "orders-sync",
            "Sync fills",
            ("orders", "--sync"),
            "Reconcile order status with the broker (needs TWS).",
        ),
        JobSpec(
            "ibkr-ping",
            "Ping IBKR",
            ("ibkr", "ping"),
            "Connect to TWS and print the account summary.",
        ),
        JobSpec(
            "ml-train",
            "Re-evaluate model",
            ("ml", "train"),
            "Composite vs LightGBM challenger, out-of-sample rank-IC.",
        ),
    ]
}


# Popen handles for jobs started by this process. poll() reaps the child on exit;
# without it a finished job stays a zombie and os.kill(pid, 0) reports it alive.
_PROCS: dict[int, subprocess.Popen[bytes]] = {}

EXIT_MARKER = "[moi exit "  # appended to the log by the wrapper; parsed for status
KEEP_JOB_LOGS = 30


class JobBlocked(RuntimeError):
    """start_job refused: another job is running or the DB is locked externally."""


def db_lock_free() -> bool:
    """Can we open the database read-only right now? (False → a writer holds it.)"""
    s = get_settings()
    if not s.db_path.exists():
        return True  # nothing to lock yet — the job itself will create it
    try:
        duckdb.connect(str(s.db_path), read_only=True).close()
    except duckdb.Error:
        return False
    return True


def start_job(key: str) -> dict[str, Any]:
    """Launch a `moi` CLI job as a detached subprocess; record it for re-attach.

    Guarded: refuses when a tracked job is still alive (e.g. a second browser tab)
    or when an untracked process — the nightly launchd collect, a terminal `moi run`
    — holds the single-writer database.
    """
    spec = JOBS[key]
    running = current_job()
    if running and running["running"]:
        raise JobBlocked(f"a job is already running ({running['key']})")
    if not db_lock_free():
        raise JobBlocked(
            "another process holds the database (nightly collect or a terminal run?) — "
            "try again when it finishes"
        )
    JOB_LOG_DIR.mkdir(parents=True, exist_ok=True)
    _prune_logs()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = JOB_LOG_DIR / f"{stamp}-{key}.log"
    # Shell wrapper so the exit code lands in the log even if the dashboard restarts
    # and loses the Popen handle. argv is constant and shell-quoted — never user input.
    cmd = shlex.join([sys.executable, "-m", "moi", *spec.args])
    script = f'{cmd}; rc=$?; echo; echo "{EXIT_MARKER}$rc]"; exit $rc'
    with open(log_path, "wb") as fh:
        proc = subprocess.Popen(
            script,
            shell=True,
            stdout=fh,
            stderr=subprocess.STDOUT,
            cwd=str(ROOT),
            start_new_session=True,  # survives a dashboard restart
        )
    _PROCS[proc.pid] = proc
    info: dict[str, Any] = {
        "pid": proc.pid,
        "key": key,
        "log": str(log_path),
        "started": datetime.now().isoformat(timespec="seconds"),
    }
    # Atomic replace: a concurrent reader never sees a half-written file.
    tmp = CURRENT_JOB_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(info))
    tmp.replace(CURRENT_JOB_FILE)
    return info


def current_job() -> dict[str, Any] | None:
    """The tracked job (running or awaiting dismissal), or None."""
    if not CURRENT_JOB_FILE.exists():
        return None
    try:
        info: dict[str, Any] = json.loads(CURRENT_JOB_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    info["running"] = _pid_alive(int(info.get("pid", -1)))
    if info["running"] and _finished_by_log(info):
        # pid was reused by an unrelated process (e.g. after a reboot) — the log's
        # exit marker is the ground truth that the job actually finished.
        info["running"] = False
    return info


def cancel_job() -> bool:
    """SIGTERM the tracked job's process group. Returns True if a signal was sent."""
    info = current_job()
    if not (info and info["running"]):
        return False
    try:
        os.killpg(int(info["pid"]), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def clear_job() -> None:
    """Dismiss the tracked job (the log file and run_log rows remain)."""
    CURRENT_JOB_FILE.unlink(missing_ok=True)


def job_exit_code(info: dict[str, Any]) -> int | None:
    """Exit code of a finished job: from our Popen handle, else the log marker."""
    proc = _PROCS.get(int(info.get("pid", -1)))
    if proc is not None and proc.poll() is not None:
        return proc.returncode
    tail = tail_log(Path(info["log"]))
    marker = tail.rfind(EXIT_MARKER)
    if marker != -1:
        try:
            return int(tail[marker + len(EXIT_MARKER) :].split("]")[0])
        except ValueError:
            return None
    return None


def tail_log(path: Path, max_bytes: int = 8_000) -> str:
    """Last chunk of a job log, decoded leniently (seek — never read the whole file)."""
    if not path.exists():
        return ""
    with open(path, "rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        fh.seek(max(0, size - max_bytes))
        return fh.read().decode("utf-8", errors="replace")


def _finished_by_log(info: dict[str, Any]) -> bool:
    return EXIT_MARKER in tail_log(Path(info["log"]), max_bytes=200)


def _prune_logs() -> None:
    logs = sorted(JOB_LOG_DIR.glob("*.log"))
    for old in logs[: max(0, len(logs) - KEEP_JOB_LOGS)]:
        old.unlink(missing_ok=True)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    proc = _PROCS.get(pid)
    if proc is not None:
        return proc.poll() is None
    # Re-attach case (dashboard restarted): the detached child was reparented to
    # init, which reaps it on exit, so a plain signal-0 probe is reliable here.
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --------------------------------------------------------------------------- #
# Connection / configuration checks
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Check:
    name: str
    state: str  # "ok" | "warn" | "off" | "error"
    detail: str


def tws_reachable(timeout: float = 0.5) -> bool:
    """Fast TCP probe of the configured TWS/Gateway port (no IB handshake)."""
    cfg = get_settings().ibkr
    try:
        with socket.create_connection((cfg.host, cfg.port), timeout=timeout):
            return True
    except OSError:
        return False


def connection_checks() -> list[Check]:
    """Every external dependency: is it configured, and is it reachable?"""
    s = get_settings()
    checks: list[Check] = []

    if s.db_path.exists():
        size_mb = s.db_path.stat().st_size / 1e6
        try:
            con = duckdb.connect(str(s.db_path), read_only=True)
            try:
                version = con.execute("SELECT max(version) FROM schema_version").fetchone()
                v = version[0] if version else "?"
                checks.append(Check("Database", "ok", f"{size_mb:.0f} MB · schema v{v}"))
            except duckdb.Error:  # connects but no schema_version — never migrated
                checks.append(
                    Check("Database", "error", f"{size_mb:.0f} MB · no schema — run `moi db init`")
                )
            finally:
                con.close()
        except duckdb.Error:  # cannot even connect read-only → a writer holds the lock
            checks.append(
                Check("Database", "warn", f"{size_mb:.0f} MB · busy (a job holds the write lock)")
            )
    else:
        checks.append(Check("Database", "error", "missing — run `moi db init`"))

    port = s.ibkr.port
    kind = "LIVE" if port in (7496, 4001) else "paper"
    if tws_reachable():
        mode = "read-only" if s.ibkr.readonly else "trading enabled"
        checks.append(Check("IBKR TWS", "ok", f"port {port} ({kind}, {mode})"))
    else:
        checks.append(
            Check("IBKR TWS", "error", f"port {port} not reachable — start TWS, enable the API")
        )

    if s.edgar_identity:
        checks.append(Check("SEC EDGAR", "ok", f"identity: {s.edgar_identity}"))
    else:
        checks.append(
            Check("SEC EDGAR", "error", "MOI_EDGAR_IDENTITY missing — 13F/Form 4 blocked")
        )

    if s.fred_api_key:
        checks.append(Check("FRED macro", "ok", "API key configured"))
    else:
        checks.append(Check("FRED macro", "warn", "no key — macro collection skipped"))

    if s.quiver_api_key or s.unusualwhales_api_key:
        provider = "Quiver" if s.quiver_api_key else "Unusual Whales"
        checks.append(Check("Congress trades", "ok", f"{provider} key configured"))
    else:
        checks.append(Check("Congress trades", "off", "optional — no API key"))

    checks.append(Check("Polymarket", "ok", "public API — no key needed"))

    if importlib.util.find_spec("claude_agent_sdk") is not None:
        checks.append(Check("Claude agents", "ok", "Agent SDK installed — narrated reports"))
    else:
        checks.append(
            Check("Claude agents", "warn", "SDK missing — reports fall back to numbers-only")
        )

    if s.telegram_bot_token and s.telegram_chat_id:
        checks.append(Check("Telegram", "ok", "notifications configured"))
    else:
        checks.append(Check("Telegram", "off", "optional — no bot token"))

    missing = [name for name, path in LAUNCHD_PLISTS.items() if not path.exists()]
    if not missing:
        checks.append(Check("Scheduler", "ok", " · ".join(LAUNCHD_PLISTS)))
    else:
        checks.append(Check("Scheduler", "warn", f"launchd job(s) missing: {', '.join(missing)}"))

    if s.allow_live:
        checks.append(
            Check(
                "Trading mode",
                "warn",
                f"LIVE enabled — rails: ${s.max_order_usd:,.0f}/order, "
                f"${s.max_daily_usd:,.0f}/day, kill switch",
            )
        )
    else:
        checks.append(Check("Trading mode", "ok", "paper-only (allow_live is off)"))
    return checks


# --------------------------------------------------------------------------- #
# Data-source board (freshness + last collector run)
# --------------------------------------------------------------------------- #
def source_board(con: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """Per source: table freshness merged with the latest run_log outcome."""
    last_runs = {
        job: (status, finished, detail)
        for job, status, finished, detail in con.execute(
            """SELECT job, status, finished_at, detail FROM (
                   SELECT *, row_number() OVER (PARTITION BY job ORDER BY started_at DESC) AS rn
                   FROM run_log) WHERE rn = 1"""
        ).fetchall()
    }
    rows: list[dict[str, Any]] = []
    for ts in check_freshness(con):
        job = TABLE_TO_JOB.get(ts.table, "")
        status, finished, detail = last_runs.get(job, (None, None, None))
        rows.append(
            {
                "source": ts.table.removesuffix("_daily")
                .removesuffix("_series")
                .removesuffix("_items")
                .replace("_", " "),
                "freshness": ts.state,
                "rows": ts.rows,
                "latest data": (ts.latest or "—")[:10],
                "last run": status or "never",
                "ran at": f"{finished:%Y-%m-%d %H:%M}" if finished else "—",
                "note": (detail or "")[:80],
            }
        )
    return rows
