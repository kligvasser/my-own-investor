"""Report rendering: markdown layout, verdict parsing, and the phone-friendly HTML."""

from __future__ import annotations

import pandas as pd

from moi.ml.portfolio import Position, TargetPortfolio
from moi.ml.regime import Regime
from moi.report.html import render_html
from moi.report.suggestions import Action
from moi.report.weekly import CONFIDENCE_LEVELS, _render_markdown, _split_verdict


def _portfolio() -> TargetPortfolio:
    return TargetPortfolio(
        week_end=pd.Timestamp("2026-07-17"),
        regime=Regime(pd.Timestamp("2026-07-17"), "risk_on", 1.0, {}),
        positions=[Position("ICHR", 0.083, "semicap", 0.655)],
        cash_weight=0.0,
    )


def _markdown() -> str:
    actions = [
        Action(
            "ICHR",
            "BUY",
            0.0,
            0.083,
            score=0.655,
            thesis="entry case",
            bear_case="objection",
            confidence="strong",
            risk_note="strong — aligned",
        ),
        Action(
            "OLD",
            "SELL",
            0.05,
            0.0,
            score=0.1,
            thesis="exit case",
            confidence="weak",
            risk_note="weak — one source",
        ),
    ]
    return _render_markdown(
        week=pd.Timestamp("2026-07-17"),
        stale=[],
        summary="the summary",
        portfolio=_portfolio(),
        macro_txt="macro prose",
        changed=actions,
        positions_known=True,
        held={"OLD": 0.05, "ALAB": 0.08},
        exit_review={"ALAB": "WATCH — insider selling", "OLD": "EXIT — dropped from target"},
        holdings="- OLD: facts",
        etfs=[("SMH", 0.34)],
        whales_txt="",
        whales="whale facts",
        trends="trend facts",
    )


def test_markdown_has_sell_side_sections() -> None:
    text = _markdown()
    assert "| SELL | OLD |" in text  # sell action in the compact table
    assert "### SELL OLD — 5.0% → 0.0%" in text  # per-action detail block
    assert "**Risk vet:** weak — one source" in text
    assert "## Holdings review — sell side" in text
    assert "**ALAB** (8.0%): WATCH — insider selling" in text


def test_markdown_holdings_review_without_positions() -> None:
    text = _render_markdown(
        week=pd.Timestamp("2026-07-17"),
        stale=[],
        summary="",
        portfolio=_portfolio(),
        macro_txt="",
        changed=[],
        positions_known=False,
        held={},
        exit_review={},
        holdings="",
        etfs=[],
        whales_txt="",
        whales="",
        trends="",
    )
    assert "No changes proposed this week." in text
    assert "positions unavailable" in text


def test_split_verdict() -> None:
    assert _split_verdict("strong — three signals agree", CONFIDENCE_LEVELS) == (
        "strong",
        "strong — three signals agree",
    )
    assert _split_verdict("**Weak** — thin", CONFIDENCE_LEVELS) == ("weak", "**Weak** — thin")
    assert _split_verdict("meh — unknown grade", CONFIDENCE_LEVELS)[0] is None
    assert _split_verdict(None, CONFIDENCE_LEVELS) == (None, None)


def test_render_html_is_selfcontained_and_mobile() -> None:
    html = render_html(_markdown(), title="2026-W29")
    assert '<meta name="viewport"' in html
    assert "<title>2026-W29</title>" in html
    assert '<div class="tablewrap"><table>' in html  # tables scroll, page doesn't
    assert '<span class="badge sell">SELL</span>' in html
    assert '<span class="badge mid">WATCH</span>' in html
    assert "http" not in html.split("</title>")[1].split("<h1")[0]  # no external assets in head
