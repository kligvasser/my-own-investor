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

    # Heavy selling is as informative as buying in this universe — show both extremes.
    insiders = con.execute(
        """
        SELECT ticker, count(*) FILTER (code = 'P') AS buys,
               count(*) FILTER (code = 'S') AS sells
        FROM insider_form4
        WHERE tx_date > current_date - INTERVAL 90 DAY
        GROUP BY ticker HAVING buys + sells > 0
        ORDER BY abs(buys - sells) DESC LIMIT 8
        """
    ).fetchall()
    if insiders:
        lines.append("\nInsider activity, last 90 days (universe, most net activity):")
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
    ]
    for name, val, chg in pairs:
        if val is None:
            continue
        suffix = f" ({chg:+.2f})" if chg is not None else ""
        lines.append(f"- {name}: {val:.3f}{suffix}")

    markets = con.execute(
        """
        SELECT m.category, m.question,
               max_by(s.prob, s.ts) AS latest,
               max_by(s.prob, s.ts) FILTER (
                   WHERE s.ts <= current_date - INTERVAL 7 DAY) AS week_ago
        FROM polymarket_series s
        JOIN polymarket_markets m ON m.slug = s.slug
        WHERE NOT m.closed
        GROUP BY 1, 2 ORDER BY 1, 2
        """
    ).fetchall()
    if markets:
        lines.append("")
        lines.append("Polymarket odds (latest, 7-day change):")
        for category, question, latest, week_ago in markets:
            delta = f", {(latest - week_ago) * 100:+.0f}pp/7d" if week_ago is not None else ""
            lines.append(f"- [{category}] {question}: {latest:.0%}{delta}")
    return "\n".join(lines)


def holdings_digest(
    con: duckdb.DuckDBPyConnection,
    held: dict[str, float],
    scores: dict[str, float],
    target: dict[str, float],
) -> str:
    """Sell-side facts for each held universe position: score rank, insider tape, P&L.

    ``scores`` must be the FULL latest ranking (all scored tickers), not just the
    target picks — a holding that fell out of the top-N still has a rank worth showing.
    """
    if not held:
        return "No current universe holdings (fresh build or positions unavailable)."
    ranking = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    rank_of = {t: i + 1 for i, (t, _) in enumerate(ranking)}
    insider = dict(
        con.execute(
            """
            SELECT ticker, count(*) FILTER (code = 'S') - count(*) FILTER (code = 'P')
            FROM insider_form4
            WHERE tx_date > current_date - INTERVAL 90 DAY
            GROUP BY ticker
            """
        ).fetchall()
    )
    pnl = dict(
        con.execute(
            """
            SELECT ticker, market_value / nullif(quantity * avg_cost, 0) - 1
            FROM portfolio_snapshots
            WHERE taken_at = (SELECT max(taken_at) FROM portfolio_snapshots)
            """
        ).fetchall()
    )
    lines = ["Current holdings (weight, composite rank, 90d insider net sells, unrealized P&L):"]
    for ticker, weight in sorted(held.items(), key=lambda kv: kv[1], reverse=True):
        rank = rank_of.get(ticker)
        rank_txt = f"rank {rank}/{len(ranking)}" if rank else "unranked"
        score = scores.get(ticker)
        score_txt = f" score {score:.3f}" if score is not None else ""
        in_target = "in target" if ticker in target else "DROPPED from target"
        net = insider.get(ticker, 0)
        gain = pnl.get(ticker)
        gain_txt = f", P&L {gain:+.0%}" if gain is not None else ""
        lines.append(
            f"- {ticker}: {weight:.1%}, {rank_txt}{score_txt}, {in_target}, "
            f"insider net sells {net}{gain_txt}"
        )
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
