"""The weekly pipeline: features → scores → portfolio → suggestions → report.

``run_weekly`` is what the Saturday job executes. With ``with_llm=False`` it produces a
numbers-only report (used in tests and as a degraded mode).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb

from moi.config import ROOT
from moi.features.store import build_features
from moi.ingest.quality import check_freshness
from moi.logging import get_logger
from moi.ml.portfolio import build_portfolio
from moi.report.digests import news_digest, trends_digest, whale_digest
from moi.report.suggestions import (
    current_universe_weights,
    diff_actions,
    store_suggestions,
)

log = get_logger(__name__)

REPORTS_DIR = ROOT / "reports"


def run_weekly(con: duckdb.DuckDBPyConnection, *, with_llm: bool = True, top_n: int = 12) -> Path:
    """Execute the full weekly pipeline; returns the path of the written report."""
    stale = [t.table for t in check_freshness(con) if t.state in ("stale", "empty")]
    build_features(con)
    portfolio = build_portfolio(con, top_n=top_n)
    week = portfolio.week_end

    target = {p.ticker: p.weight for p in portfolio.positions}
    scores = {p.ticker: p.score for p in portfolio.positions}
    current = current_universe_weights(con)
    actions = diff_actions(target, current or {})
    positions_known = current is not None

    whales = whale_digest(con)
    trends = trends_digest(con)
    news = news_digest(con, list(target))

    changed = [a for a in actions if a.action in ("BUY", "SELL", "ADD", "TRIM")]
    if with_llm and changed:
        from moi.orchestrator.agents import ask

        action_lines = "\n".join(
            f"- {a.ticker}: {a.action} {a.current_weight:.1%} -> {a.target_weight:.1%} "
            f"(score {scores.get(a.ticker, 0) or 0:.3f})"
            for a in changed
        )
        theses = ask("analyst", f"Proposed actions:\n{action_lines}\n\nRecent headlines:\n{news}")
        bears = ask("bear", f"Proposed actions:\n{action_lines}\n\nRecent headlines:\n{news}")
        thesis_map = _parse_ticker_lines(theses)
        bear_map = _parse_ticker_lines(bears)
        for a in changed:
            a.thesis = thesis_map.get(a.ticker)
            a.bear_case = bear_map.get(a.ticker)
        macro_txt = ask(
            "macro",
            f"Regime: {portfolio.regime.name} (gross {portfolio.regime.gross:.0%}).\n{trends}",
        )
        whales_txt = ask("whales", whales)
        summary = ask(
            "pm",
            f"Regime: {portfolio.regime.name}. Actions:\n{action_lines}\n\n"
            f"Bear objections:\n{bears}\n\nMacro:\n{trends}",
        )
    else:
        macro_txt, whales_txt, summary = "", "", ""

    store_suggestions(con, week, changed)

    lines = [
        f"# Weekly report — week ending {week.date()}",
        f"_generated {datetime.now():%Y-%m-%d %H:%M}_",
        "",
        "> Model output for personal review — not financial advice.",
        "",
    ]
    if stale:
        lines += [f"**⚠ data warning:** stale/empty tables: {', '.join(stale)}", ""]
    if summary:
        lines += ["## Summary", "", summary, ""]
    lines += [
        "## Regime",
        "",
        f"**{portfolio.regime.name}** — gross exposure {portfolio.regime.gross:.0%}",
        "",
        macro_txt,
        "",
        "## Proposed actions"
        + ("" if positions_known else " (positions unavailable — fresh-build proposal)"),
        "",
    ]
    if changed:
        lines += [
            "| action | ticker | now | target | thesis | bear case |",
            "|---|---|---|---|---|---|",
        ]
        for a in changed:
            lines.append(
                f"| {a.action} | {a.ticker} | {a.current_weight:.1%} | {a.target_weight:.1%} "
                f"| {a.thesis or '-'} | {a.bear_case or '-'} |"
            )
        lines += [
            "",
            f"{len(changed)} suggestions queued as PENDING — approve via dashboard/CLI.",
            "",
        ]
    else:
        lines += ["No changes proposed this week.", ""]

    lines += ["## Target portfolio", ""]
    for p in portfolio.positions:
        lines.append(f"- {p.ticker}: {p.weight:.1%} ({p.sub_sector}, score {p.score:.3f})")
    lines += [f"- CASH: {portfolio.cash_weight:.1%}", ""]
    lines += ["## Whale watch", "", whales_txt or "", "", whales, ""]
    lines += ["## Trends", "", trends, ""]

    REPORTS_DIR.mkdir(exist_ok=True)
    iso = week.isocalendar()
    path = REPORTS_DIR / f"{iso.year}-W{iso.week:02d}.md"
    path.write_text("\n".join(lines))
    log.info("weekly_report_written", path=str(path), suggestions=len(changed))
    return path


def _parse_ticker_lines(text: str) -> dict[str, str]:
    """Parse '- TICKER: prose' lines from agent output."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip().lstrip("-*").strip()
        if ":" in line:
            head, _, body = line.partition(":")
            ticker = head.strip().upper().strip("*_`")
            if ticker.isalpha() and 1 <= len(ticker) <= 6 and body.strip():
                out[ticker] = body.strip()
    return out
