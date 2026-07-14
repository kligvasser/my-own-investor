"""Render a weekly report's markdown into a self-contained, phone-friendly HTML page.

No external assets (CSS inlined, no JS, no CDN) so the file works offline, over
Telegram/AirDrop, and inside the dashboard download button.
"""

from __future__ import annotations

import re

import markdown as md

_BADGES = {
    "BUY": "buy",
    "ADD": "buy",
    "SELL": "sell",
    "TRIM": "sell",
    "strong": "ok",
    "moderate": "mid",
    "weak": "bad",
}

_STYLE = """
:root { color-scheme: light dark;
  --bg: #ffffff; --fg: #1a1d21; --muted: #5c6570; --line: #e3e6ea;
  --accent: #2456a6; --buy: #0a7d33; --buy-bg: #e2f4e8; --sell: #b02a2a; --sell-bg: #fbe7e7;
  --ok: #0a7d33; --ok-bg: #e2f4e8; --mid: #8a6d00; --mid-bg: #faf0cd;
  --bad: #b02a2a; --bad-bg: #fbe7e7; --card: #f6f7f9; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #14171a; --fg: #e6e8ea; --muted: #9aa4ae; --line: #2b3138;
    --accent: #7aa5e8; --buy: #5fce8a; --buy-bg: #16341f; --sell: #ef8f8f; --sell-bg: #3b1c1c;
    --ok: #5fce8a; --ok-bg: #16341f; --mid: #e5c65a; --mid-bg: #37300f;
    --bad: #ef8f8f; --bad-bg: #3b1c1c; --card: #1c2126; }
}
* { box-sizing: border-box; }
body { margin: 0 auto; padding: 16px; max-width: 760px; background: var(--bg);
  color: var(--fg); font: 16px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI",
  Roboto, sans-serif; -webkit-text-size-adjust: 100%; }
h1 { font-size: 1.45rem; line-height: 1.25; margin: 0.4em 0; }
h2 { font-size: 1.15rem; margin: 1.6em 0 0.5em; padding-top: 0.9em;
  border-top: 1px solid var(--line); }
h3 { font-size: 1rem; margin: 1.2em 0 0.4em; }
p, ul { margin: 0.5em 0; }
li { margin: 0.25em 0; }
em { color: var(--muted); }
a { color: var(--accent); }
blockquote { margin: 0.8em 0; padding: 0.6em 0.9em; border-left: 3px solid var(--accent);
  background: var(--card); border-radius: 0 8px 8px 0; font-size: 0.92rem; }
blockquote p { margin: 0; }
.tablewrap { overflow-x: auto; -webkit-overflow-scrolling: touch; margin: 0.8em -16px;
  padding: 0 16px; }
table { border-collapse: collapse; font-size: 0.88rem; min-width: 100%; }
th, td { padding: 7px 10px; text-align: left; border-bottom: 1px solid var(--line);
  vertical-align: top; }
th { color: var(--muted); font-weight: 600; white-space: nowrap; }
details { margin: 0.8em 0; padding: 0.5em 0.9em; background: var(--card);
  border-radius: 8px; font-size: 0.9rem; }
summary { cursor: pointer; color: var(--muted); font-weight: 600; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 10px;
  font-size: 0.8rem; font-weight: 700; white-space: nowrap; }
.badge.buy { color: var(--buy); background: var(--buy-bg); }
.badge.sell { color: var(--sell); background: var(--sell-bg); }
.badge.ok { color: var(--ok); background: var(--ok-bg); }
.badge.mid { color: var(--mid); background: var(--mid-bg); }
.badge.bad { color: var(--bad); background: var(--bad-bg); }
"""

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{style}</style>
</head>
<body>
{body}
</body>
</html>
"""


def render_html(md_text: str, *, title: str = "Weekly report") -> str:
    """Markdown report → standalone responsive HTML document."""
    body = md.markdown(md_text, extensions=["tables", "sane_lists"])
    body = body.replace("<table>", '<div class="tablewrap"><table>')
    body = body.replace("</table>", "</table></div>")
    # Badge the action/confidence words in table cells (exact-cell matches only).
    for word, cls in _BADGES.items():
        body = body.replace(f"<td>{word}</td>", f'<td><span class="badge {cls}">{word}</span></td>')
    # Badge the KEEP/WATCH/EXIT verdicts in the holdings review list.
    body = re.sub(
        r"\b(KEEP|WATCH|EXIT)\b(?=\s*[—:-])",
        lambda m: '<span class="badge {}">{}</span>'.format(
            {"KEEP": "ok", "WATCH": "mid", "EXIT": "bad"}[m.group(1)], m.group(1)
        ),
        body,
    )
    return _PAGE.format(title=title, style=_STYLE, body=body)
