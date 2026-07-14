"""The weekly pipeline: features → scores → signals → actions → assessment → report.

``run_weekly`` is what the Saturday job executes. With ``with_llm=False`` it produces a
numbers-only report (used in tests and as a degraded mode).

Ordering contract: every signal digest (holdings, whales, macro, prediction markets,
news) is assembled into one bundle BEFORE any AI assessment runs, and every agent
receives that full bundle — so theses, bear cases, confidence grades and the holdings
exit review are always judgments over the complete picture, never over a single feed.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd

from moi.config import ROOT
from moi.features.store import build_features
from moi.ingest.quality import check_freshness
from moi.logging import get_logger
from moi.ml.composite import latest_scores
from moi.ml.portfolio import TargetPortfolio, build_portfolio
from moi.report.digests import holdings_digest, news_digest, trends_digest, whale_digest
from moi.report.html import render_html
from moi.report.suggestions import (
    Action,
    benchmark_overlap,
    current_universe_weights,
    diff_actions,
    store_suggestions,
)

log = get_logger(__name__)

REPORTS_DIR = ROOT / "reports"

CONFIDENCE_LEVELS = {"strong", "moderate", "weak"}


def run_weekly(
    con: duckdb.DuckDBPyConnection,
    *,
    with_llm: bool = True,
    top_n: int = 12,
    collect: bool = False,
) -> Path:
    """Execute the full weekly pipeline; returns the path of the written report."""
    if collect:
        from moi.ingest.runner import collect_everything

        for name, outcome in collect_everything(con):
            log.info("weekly_collect_step", step=name, outcome=outcome)
    stale = [t.table for t in check_freshness(con) if t.state in ("stale", "empty")]
    build_features(con)
    ranked = latest_scores(con)  # full ranking — the holdings review needs every rank
    portfolio = build_portfolio(con, top_n=top_n, ranked=ranked)
    week = portfolio.week_end

    target = {p.ticker: p.weight for p in portfolio.positions}
    all_scores = {str(t): float(s) for t, s in zip(ranked["ticker"], ranked["score"], strict=True)}
    current = current_universe_weights(con)
    held = current or {}
    positions_known = current is not None

    # ---- 1. Gather EVERY signal before any action assessment -----------------------
    whales = whale_digest(con)
    trends = trends_digest(con)
    news = news_digest(con, sorted(set(target) | set(held)))
    holdings = holdings_digest(con, held, all_scores, target)
    bundle = _signal_bundle(portfolio, holdings, whales, trends, news)

    # ---- 2. Candidate actions (deterministic target-vs-current diff) ---------------
    actions = diff_actions(target, held)
    changed = [a for a in actions if a.action in ("BUY", "SELL", "ADD", "TRIM")]
    for a in changed:
        a.score = all_scores.get(a.ticker)

    # ---- 3. AI assessment — always over the full bundle ----------------------------
    exit_review: dict[str, str] = {}
    if with_llm:
        from moi.orchestrator.agents import ask

        action_lines = "\n".join(
            f"- {a.ticker}: {a.action} {a.current_weight:.1%} -> {a.target_weight:.1%} "
            f"(score {a.score or 0:.3f})"
            for a in changed
        )
        actions_prompt = (
            f"{bundle}\n\n### Proposed actions "
            f"(assess only after weighing ALL signals above)\n{action_lines}"
        )
        if changed:
            thesis_map = _parse_ticker_lines(ask("analyst", actions_prompt))
            bear_map = _parse_ticker_lines(ask("bear", actions_prompt))
            risk_map = _parse_ticker_lines(ask("risk", actions_prompt))
            for a in changed:
                a.thesis = thesis_map.get(a.ticker)
                a.bear_case = bear_map.get(a.ticker)
                level, note = _split_verdict(risk_map.get(a.ticker), CONFIDENCE_LEVELS)
                if level:
                    a.confidence = level
                a.risk_note = note
        if held:
            exit_prompt = (
                f"{bundle}\n\n### Held positions to review "
                f"(judge only after weighing ALL signals above)\n"
                + "\n".join(f"- {t}" for t in sorted(held))
            )
            exit_review = _parse_ticker_lines(ask("exit", exit_prompt))
        macro_txt = ask("macro", bundle)
        whales_txt = ask("whales", bundle)
        summary = ask(
            "pm",
            f"{actions_prompt}\n\n### Holdings exit review\n"
            + ("\n".join(f"- {t}: {v}" for t, v in exit_review.items()) or "none"),
        )
    else:
        macro_txt, whales_txt, summary = "", "", ""

    # A fresh-build diff (positions unknown) must never reach the queue: its BUYs
    # start from 0% and would double-buy positions the account already holds.
    if positions_known:
        store_suggestions(con, week, changed)
    else:
        log.warning("suggestions_not_stored", reason="positions unavailable (TWS down)")

    # ---- 4. Render ------------------------------------------------------------------
    text = _render_markdown(
        week=week,
        stale=stale,
        summary=summary,
        portfolio=portfolio,
        macro_txt=macro_txt,
        changed=changed,
        positions_known=positions_known,
        held=held,
        exit_review=exit_review,
        holdings=holdings,
        etfs=benchmark_overlap(con),
        whales_txt=whales_txt,
        whales=whales,
        trends=trends,
    )
    REPORTS_DIR.mkdir(exist_ok=True)
    iso = week.isocalendar()
    path = REPORTS_DIR / f"{iso.year}-W{iso.week:02d}.md"
    path.write_text(text)
    html_path = path.with_suffix(".html")
    html_path.write_text(render_html(text, title=f"Weekly report {iso.year}-W{iso.week:02d}"))
    log.info("weekly_report_written", path=str(path), html=str(html_path), suggestions=len(changed))
    return path


def _signal_bundle(
    portfolio: TargetPortfolio, holdings: str, whales: str, trends: str, news: str
) -> str:
    """Every signal the pipeline collected, as one prompt block handed to each agent."""
    ranking = "\n".join(
        f"- {p.ticker}: score {p.score:.3f} ({p.sub_sector})" for p in portfolio.positions
    )
    return "\n\n".join(
        [
            f"### Regime\n{portfolio.regime.name} (gross exposure {portfolio.regime.gross:.0%})",
            f"### Target ranking (top {len(portfolio.positions)})\n{ranking}",
            f"### Current holdings\n{holdings}",
            f"### Whales & insiders\n{whales}",
            f"### Macro & prediction markets\n{trends}",
            f"### Recent headlines\n{news}",
        ]
    )


def _render_markdown(
    *,
    week: pd.Timestamp,
    stale: list[str],
    summary: str,
    portfolio: TargetPortfolio,
    macro_txt: str,
    changed: list[Action],
    positions_known: bool,
    held: dict[str, float],
    exit_review: dict[str, str],
    holdings: str,
    etfs: list[tuple[str, float]],
    whales_txt: str,
    whales: str,
    trends: str,
) -> str:
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
            "| action | ticker | now | target | score | confidence |",
            "|---|---|---|---|---|---|",
        ]
        for a in changed:
            score = f"{a.score:.3f}" if a.score is not None else "—"
            lines.append(
                f"| {a.action} | {a.ticker} | {a.current_weight:.1%} | {a.target_weight:.1%} "
                f"| {score} | {a.confidence} |"
            )
        lines.append("")
        for a in changed:
            if not (a.thesis or a.bear_case or a.risk_note):
                continue
            lines += [
                f"### {a.action} {a.ticker} — {a.current_weight:.1%} → {a.target_weight:.1%}",
                "",
            ]
            if a.thesis:
                lines += [f"**Thesis:** {a.thesis}", ""]
            if a.bear_case:
                lines += [f"**Bear case:** {a.bear_case}", ""]
            if a.risk_note:
                lines += [f"**Risk vet:** {a.risk_note}", ""]
        if positions_known:
            lines += [
                f"{len(changed)} suggestions queued as PENDING — approve via dashboard/CLI.",
                "",
            ]
        else:
            lines += [
                "**⚠ NOT queued:** positions were unavailable (TWS down), so these are a "
                "fresh-build sketch — start TWS and rerun `moi weekly` to get real deltas.",
                "",
            ]
    else:
        lines += ["No changes proposed this week.", ""]

    lines += ["## Holdings review — sell side", ""]
    if held:
        lines += [
            "_Every held position judged against the full signal bundle (KEEP / WATCH / EXIT)._",
            "",
        ]
        for ticker, weight in sorted(held.items(), key=lambda kv: kv[1], reverse=True):
            verdict = exit_review.get(ticker)
            if verdict:
                lines.append(f"- **{ticker}** ({weight:.1%}): {verdict}")
            else:
                lines.append(f"- **{ticker}** ({weight:.1%}): _no review available_")
        lines += [
            "",
            "<details><summary>Holdings signal data</summary>",
            "",
            holdings,
            "",
            "</details>",
            "",
        ]
    else:
        lines += [
            "No universe holdings to review"
            + ("" if positions_known else " (positions unavailable — TWS down)")
            + ".",
            "",
        ]

    lines += ["## Target portfolio", ""]
    for p in portfolio.positions:
        lines.append(f"- {p.ticker}: {p.weight:.1%} ({p.sub_sector}, score {p.score:.3f})")
    lines += [f"- CASH: {portfolio.cash_weight:.1%}", ""]

    if etfs:
        held_txt = ", ".join(f"{t} {w:.1%}" for t, w in etfs)
        lines += [
            "> **Outside managed sleeve:** benchmark ETF holdings — "
            + held_txt
            + ". The system never places orders on these; rotating them into the "
            "managed sleeve is a manual decision.",
            "",
        ]
    lines += ["## Whale watch", "", whales_txt or "", "", whales, ""]
    lines += ["## Trends", "", trends, ""]
    return "\n".join(lines)


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


def _split_verdict(text: str | None, levels: set[str]) -> tuple[str | None, str | None]:
    """Extract the leading grade word ('strong — reason' → ('strong', full text))."""
    if not text:
        return None, None
    first = text.split()[0].strip("*_:—-.,").lower()
    return (first if first in levels else None), text
