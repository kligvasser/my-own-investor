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
    + " Role: equity analyst. For EACH ticker listed, write a 2-3 sentence thesis grounded "
    "in its score components and recent headlines. Format strictly as '- TICKER: thesis'.",
    "bear": _BASE
    + " Role: red-team bear. For EACH proposed action listed, give the strongest concrete "
    "objection (valuation, cyclicality, customer concentration, dilution...). Format "
    "strictly as '- TICKER: objection'. Be specific, not generic.",
    "pm": _BASE + " Role: portfolio manager. Write the executive summary: regime, top actions with "
    "one-line rationale, biggest risk this month. 150 words max.",
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
