"""LLM agent roles (Claude Agent SDK).

Design: the quant pipeline computes every number; agents only *narrate and critique*
the digests they're handed. No agent has tools, database access, or the ability to
place orders — they turn validated data into readable judgment. One call per section
keeps weekly LLM cost to a few cents.

If the Claude CLI is unavailable, each call degrades to a stub note so the weekly
report still ships (numbers intact, narrative missing).
"""

from __future__ import annotations

import anyio

from moi.logging import get_logger

log = get_logger(__name__)

_BASE = (
    "You are part of 'my-own-investor', a personal portfolio copilot for a small/mid-cap "
    "hardware universe (computing, data centers, connectivity). Mid-horizon (months-years) "
    "discipline. Write tight, concrete prose for the weekly report — no hedging boilerplate, "
    "no invented numbers: use ONLY figures present in the provided data. Output plain "
    "markdown body text (no top-level heading)."
)

ROLES = {
    "macro": _BASE
    + " Role: macro/trends analyst. From the market snapshot, explain the regime call and "
    "what changed in the last quarter that matters for hardware/data-center names. 120 words max.",
    "whales": _BASE
    + " Role: whale watcher. Summarize what the tracked 13F managers and insiders did and "
    "whether it confirms or contradicts our candidate ranking. Note the 45-day 13F lag. "
    "120 words max.",
    "analyst": _BASE
    + " Role: equity analyst. You receive the FULL weekly signal bundle (scores, holdings, "
    "whales, insiders, macro, prediction markets, headlines) plus the proposed actions. "
    "For EACH action, write a 2-3 sentence direction-aware thesis: for BUY/ADD explain the "
    "entry case; for TRIM/SELL explain the exit case (score decay, deteriorating signals, "
    "better uses of the weight). Ground every claim in the bundle. "
    "Format strictly as '- TICKER: thesis'.",
    "bear": _BASE
    + " Role: red-team devil's advocate. For EACH proposed action, argue the OTHER side: "
    "against a BUY/ADD give the strongest concrete objection (valuation, cyclicality, "
    "customer concentration, dilution...); against a TRIM/SELL give the strongest reason "
    "to keep the position (what upside is being given up). Use the full signal bundle. "
    "Format strictly as '- TICKER: objection'. Be specific, not generic.",
    "risk": _BASE
    + " Role: risk officer. You receive the FULL weekly signal bundle and the proposed "
    "actions LAST, so judge each action only after weighing every signal: score strength, "
    "insider tape, whale positioning, macro regime, prediction-market odds, headlines. "
    "For EACH action output exactly one line "
    "'- TICKER: strong|moderate|weak — one-sentence reason', where strong = multiple "
    "independent signals agree, moderate = signals mixed or thin, weak = signals "
    "contradict the action or rest on a single source.",
    "exit": _BASE
    + " Role: exit reviewer for currently-held positions. For EACH held ticker listed, "
    "weigh the sell side of the ledger against the full bundle: score rank and trend, "
    "insider selling, whale trims, macro/odds shifts, adverse headlines. Output exactly "
    "one line '- TICKER: KEEP|WATCH|EXIT — one-sentence reason'. Use EXIT only when "
    "several signals independently deteriorated; WATCH when one meaningful signal turned.",
    "pm": _BASE + " Role: portfolio manager. Write the executive summary: regime, top actions "
    "(both entries and exits) with one-line rationale, biggest risk this month. 150 words max.",
}


async def _ask_async(role: str, prompt: str) -> str:
    from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

    options = ClaudeAgentOptions(
        system_prompt=ROLES[role],
        max_turns=1,
        allowed_tools=[],
    )
    parts: list[str] = []
    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            parts += [b.text for b in message.content if isinstance(b, TextBlock)]
    return "".join(parts).strip()


def ask(role: str, prompt: str) -> str:
    """Synchronous single-shot agent call; degrades to a stub on any failure."""
    try:
        text = anyio.run(_ask_async, role, prompt)
        if text:
            log.info("agent_done", role=role, chars=len(text))
            return text
        raise RuntimeError("empty response")
    except Exception as exc:
        log.warning("agent_unavailable", role=role, error=str(exc)[:200])
        return f"_({role} narrative unavailable: {type(exc).__name__})_"
