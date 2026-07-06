"""Deterministic data digests — the context handed to the LLM agents.

Everything here is plain SQL/pandas; the agents narrate these facts, they never
compute them. That keeps every number in the report reproducible.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import duckdb


def whale_digest(con: duckdb.DuckDBPyConnection) -> str:
    """Latest-quarter whale moves: universe overlap first, then largest actions."""
    lines: list[str] = []
    overlap = con.execute(
        """
        SELECT f.manager_name, f.ticker, f.change_status, round(f.value_usd / 1e6) AS mm
        FROM filings_13f f
        JOIN universe u ON u.ticker = f.ticker AND u.active AND NOT u.is_benchmark
        WHERE f.period = (SELECT max(period) FROM filings_13f)
        ORDER BY f.value_usd DESC
        """
    ).fetchall()
    if overlap:
        lines.append("Whale positions overlapping our universe (latest quarter):")
        lines += [f"- {m}: {t} {c} (${mm:.0f}M)" for m, t, c, mm in overlap]
    else:
        lines.append("No whale 13F positions currently overlap the universe.")

    moves = con.execute(
        """
        SELECT manager_name, ticker, issuer, change_status, round(value_usd / 1e6) AS mm
        FROM filings_13f
        WHERE period = (SELECT max(period) FROM filings_13f)
          AND change_status IN ('NEW', 'DECREASED', 'INCREASED')
          AND value_usd > 200e6
        ORDER BY value_usd DESC LIMIT 12
        """
    ).fetchall()
    if moves:
        lines.append("\nLargest whale moves this quarter (any sector):")
        lines += [f"- {m}: {c} {t or issuer} (${mm:.0f}M)" for m, t, issuer, c, mm in moves]

    insiders = con.execute(
        """
        SELECT ticker, count(*) FILTER (code = 'P') AS buys,
               count(*) FILTER (code = 'S') AS sells
        FROM insider_form4
        WHERE tx_date > current_date - INTERVAL 90 DAY
        GROUP BY ticker HAVING buys > 0
        ORDER BY buys - sells DESC LIMIT 8
        """
    ).fetchall()
    if insiders:
        lines.append("\nInsider activity, last 90 days (universe):")
        lines += [f"- {t}: {b} buys / {s} sells" for t, b, s in insiders]
    return "\n".join(lines)


def trends_digest(con: duckdb.DuckDBPyConnection) -> str:
    """Macro + Polymarket snapshot with 13-week deltas."""
    lines = ["Market / macro snapshot (latest values, 13w change in parens):"]
    rows = con.execute(
        """
        SELECT feature, value FROM features_weekly
        WHERE ticker = '_MARKET_'
          AND week_end = (SELECT max(week_end) FROM features_weekly WHERE ticker = '_MARKET_')
        ORDER BY feature
        """
    ).fetchall()
    values = dict(rows)
    pairs = [
        ("SMH 13w return", values.get("mkt_smh_ret_13w"), None),
        ("SPY 13w return", values.get("mkt_spy_ret_13w"), None),
        ("10y yield", values.get("mkt_10y_yield"), values.get("mkt_10y_yield_13w_chg")),
        ("HY credit spread", values.get("mkt_hy_spread"), values.get("mkt_hy_spread_13w_chg")),
        ("10y-2y spread", values.get("mkt_t10y2y"), values.get("mkt_t10y2y_13w_chg")),
        ("Polymarket: fed category avg prob", values.get("mkt_pm_fed"), None),
        ("Polymarket: recession prob", values.get("mkt_pm_macro"), None),
        ("Polymarket: ai category avg prob", values.get("mkt_pm_ai"), None),
    ]
    for name, val, chg in pairs:
        if val is None:
            continue
        suffix = f" ({chg:+.2f})" if chg is not None else ""
        lines.append(f"- {name}: {val:.3f}{suffix}")
    return "\n".join(lines)


def news_digest(con: duckdb.DuckDBPyConnection, tickers: list[str], days: int = 10) -> str:
    """Recent headlines for the given tickers plus sector feeds."""
    since = datetime.now() - timedelta(days=days)
    placeholders = ", ".join("?" for _ in tickers)
    rows = con.execute(
        f"""
        SELECT coalesce(ticker, 'SECTOR') AS tag, title
        FROM news_items
        WHERE published_at > ? AND (ticker IN ({placeholders}) OR ticker IS NULL)
        ORDER BY published_at DESC LIMIT 60
        """,
        [since, *tickers],
    ).fetchall()
    if not rows:
        return "No recent headlines collected."
    return "\n".join(f"- [{tag}] {title}" for tag, title in rows)
