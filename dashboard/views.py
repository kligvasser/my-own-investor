"""All data pages (everything except Mission control), one function per page."""

from __future__ import annotations

import json

import pandas as pd
import plotly.express as px
import streamlit as st
from common import DBBusy, execute_write, page, q, read_connection

from moi.config import ROOT


# --------------------------------------------------------------------------- #
# Weekly report
# --------------------------------------------------------------------------- #
@page
def weekly_report() -> None:
    st.title("Weekly report")
    reports = sorted((ROOT / "reports").glob("*.md"), reverse=True)
    if not reports:
        st.info("No reports yet — run **Full pipeline** from Mission control.")
        return
    left, right = st.columns([3, 1])
    pick = left.selectbox("Report", [p.name for p in reports], label_visibility="collapsed")
    text = (ROOT / "reports" / pick).read_text()
    right.download_button("Download", text, file_name=pick, width="stretch")
    st.markdown(text)


# --------------------------------------------------------------------------- #
# Approval queue
# --------------------------------------------------------------------------- #
@page
def approval_queue() -> None:
    st.title("Approval queue")
    rows = q(
        """SELECT id, week_end, action, ticker, current_weight, target_weight,
                  score, thesis, bear_case, confidence
           FROM suggestions WHERE status = 'PENDING' ORDER BY created_at DESC"""
    )
    net_liq = None
    snap = q("SELECT net_liquidation FROM portfolio_snapshots ORDER BY taken_at DESC LIMIT 1")
    if not snap.empty and pd.notna(snap.iloc[0, 0]):
        net_liq = float(snap.iloc[0, 0])

    if rows.empty:
        st.success("Queue is empty.")
    else:
        week = rows["week_end"].iloc[0]
        st.caption(f"{len(rows)} pending · week ending {week}")

    for _, r in rows.iterrows():
        with st.container(border=True):
            left, right = st.columns([4, 1])
            with left:
                st.subheader(f"{r['action']} {r['ticker']}")
                size = ""
                if net_liq and pd.notna(r["target_weight"]) and pd.notna(r["current_weight"]):
                    delta_usd = (r["target_weight"] - r["current_weight"]) * net_liq
                    if abs(delta_usd) >= 50:
                        size = f" · ≈ ${abs(delta_usd):,.0f} {'buy' if delta_usd > 0 else 'sell'}"
                cw = f"{r['current_weight']:.1%}" if pd.notna(r["current_weight"]) else "—"
                tw = f"{r['target_weight']:.1%}" if pd.notna(r["target_weight"]) else "—"
                score = f"{r['score']:.3f}" if pd.notna(r["score"]) else "—"
                st.caption(f"{cw} → {tw}{size} · score {score} · {r['confidence']}")
                if r["thesis"]:
                    st.markdown(f"**Thesis:** {r['thesis']}")
                if r["bear_case"]:
                    st.markdown(f"**Bear case:** {r['bear_case']}")
            with right:
                sid = r["id"]
                _decide_button("✅ Approve", f"a{sid}", sid, "APPROVED")
                _decide_button("❌ Reject", f"r{sid}", sid, "REJECTED")
                _decide_button("💤 Snooze", f"s{sid}", sid, "SNOOZED")

    ready = q(
        """SELECT id, action, ticker, target_weight FROM suggestions s
           WHERE s.status = 'APPROVED'
           AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.suggestion_id = s.id
                           AND o.status != 'error')
           ORDER BY decided_at DESC"""
    )
    if not ready.empty:
        st.warning(
            f"{len(ready)} approved suggestion(s) awaiting `moi execute` (run from terminal). "
            "Revocable until an order is sent:"
        )
        for _, r in ready.iterrows():
            c1, c2 = st.columns([4, 1])
            tw = f" → {r['target_weight']:.1%}" if pd.notna(r["target_weight"]) else ""
            c1.markdown(f"**{r['action']} {r['ticker']}**{tw}")
            with c2:
                _decide_button("↩ Revoke", f"rev{r['id']}", r["id"], "REJECTED")


def _decide_button(label: str, widget_key: str, sid: str, decision: str) -> None:
    """Queue-decision button that survives a locked DB (asks to retry, keeps the click)."""
    from moi.execute.queue import decide

    if st.button(label, key=widget_key, width="stretch"):
        try:
            execute_write(lambda con: decide(con, sid, decision))
        except DBBusy:
            st.warning("Database busy (job running) — try again in a moment.")
            return
        st.rerun()


# --------------------------------------------------------------------------- #
# Portfolio
# --------------------------------------------------------------------------- #
@page
def portfolio() -> None:
    st.title("My holdings")
    from moi.report.performance import PERIODS, holdings_view, normalized_window

    with read_connection() as con:
        view = holdings_view(con)

    if view is None:
        st.info("No account snapshot yet — run the pipeline with TWS running to capture one.")
        return

    t = view.table
    total_pnl = float(t["pnl"].sum())
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Net liquidation", f"${view.net_liquidation:,.0f}")
    c2.metric(
        "Unrealized P&L",
        f"${total_pnl:,.0f}",
        f"{total_pnl / (t['value'].sum() - total_pnl):+.1%}",
    )
    c3.metric("Positions", f"{len(t)}")
    c4.metric("Snapshot", f"{view.taken_at:%Y-%m-%d}")

    st.subheader("Performance by period")
    st.caption(
        "Price return of the current holdings, value-weighted — ignores trades "
        "and cash within the period."
    )
    perf_rows = []
    for label, source in (
        ("Portfolio", view.portfolio_returns),
        ("SPY", view.benchmark_returns),
    ):
        perf_rows.append({"": label, **{p: source.get(p) for p in PERIODS}})
    perf = pd.DataFrame(perf_rows).set_index("")
    st.dataframe(perf.style.format("{:+.1%}", na_rep="—"), width="stretch")

    st.subheader("Holdings")
    show = t[
        [
            "ticker",
            "qty",
            "avg_cost",
            "price",
            "value",
            "weight",
            "pnl",
            "pnl_pct",
            "1W",
            "1M",
            "3M",
            "1Y",
        ]
    ]
    st.dataframe(
        show.style.format(
            {
                "qty": "{:.0f}",
                "avg_cost": "${:.2f}",
                "price": "${:.2f}",
                "value": "${:,.0f}",
                "weight": "{:.1%}",
                "pnl": "${:,.0f}",
                "pnl_pct": "{:+.1%}",
                "1W": "{:+.1%}",
                "1M": "{:+.1%}",
                "3M": "{:+.1%}",
                "1Y": "{:+.1%}",
            },
            na_rep="—",
        ),
        width="stretch",
        hide_index=True,
    )

    st.subheader("Relative performance")
    window_label = st.radio(
        "Window", list(PERIODS), index=1, horizontal=True, label_visibility="collapsed"
    )
    norm = normalized_window(view.closes, PERIODS[window_label]).reset_index()
    long = norm.melt(id_vars="date", var_name="ticker", value_name="indexed")
    fig = px.line(
        long.dropna(),
        x="date",
        y="indexed",
        color="ticker",
        title=f"Indexed to 100 — last {window_label}",
    )
    fig.update_layout(legend={"orientation": "h", "y": -0.25}, yaxis_title=None)
    st.plotly_chart(fig, width="stretch")

    st.subheader("Weights")
    st.plotly_chart(
        px.bar(t.sort_values("value"), x="value", y="ticker", orientation="h").update_layout(
            xaxis_title="market value ($)", yaxis_title=None
        ),
        width="stretch",
    )


# --------------------------------------------------------------------------- #
# Holdings X-ray
# --------------------------------------------------------------------------- #
@page
def xray() -> None:
    st.title("Holdings X-ray")
    st.caption(
        "How does the current book behave as a whole? Frozen-weights analysis: "
        "today's weights applied backwards — behavior of the book, not realized P&L."
    )
    from moi.report.performance import holdings_view
    from moi.report.xray import (
        contribution,
        correlation_matrix,
        growth_frame,
        insights,
        risk_table,
    )

    with read_connection() as con:
        view = holdings_view(con)

    if view is None:
        st.info("No account snapshot yet — run the pipeline with TWS running first.")
        return

    weights = dict(zip(view.table["ticker"], view.table["weight"], strict=True))
    windows = {"3M": 63, "6M": 126, "1Y": 252, "3Y": 756}
    label = st.radio(
        "Window", list(windows), index=2, horizontal=True, label_visibility="collapsed"
    )
    days = windows[label]

    risk = risk_table(view.closes, weights, days)
    corr = correlation_matrix(view.closes, list(weights), days)
    contrib = contribution(view.closes, weights, days)

    st.subheader("What the numbers say")
    for note in insights(weights, risk, corr, contrib):
        st.markdown(f"- {note}")

    st.subheader(f"Growth of 100 — last {label}")
    growth = growth_frame(view.closes, weights, days).reset_index()
    long = growth.melt(id_vars="date", var_name="series", value_name="value")
    fig = px.line(long.dropna(), x="date", y="value", color="series")
    fig.update_layout(legend={"orientation": "h", "y": -0.25}, yaxis_title=None)
    st.plotly_chart(fig, width="stretch")

    st.subheader("Risk profile")
    st.caption("Daily returns over the window, annualized where applicable.")
    st.dataframe(
        risk.style.format(
            {
                "beta": "{:.2f}",
                "ann_vol": "{:.0%}",
                "sharpe": "{:.2f}",
                "max_dd": "{:.0%}",
                "corr": "{:.2f}",
            },
            na_rep="—",
        ),
        width="stretch",
    )

    st.subheader("Contribution to portfolio return")
    st.caption("weight times return over the window — who actually moved the book.")
    st.plotly_chart(
        px.bar(
            contrib.reset_index().rename(columns={"index": "ticker", 0: "contribution"}),
            x="contribution",
            y="ticker",
            orientation="h",
        ).update_layout(xaxis_tickformat="+.1%", yaxis_title=None),
        width="stretch",
    )

    st.subheader("Correlation between holdings")
    st.plotly_chart(
        px.imshow(
            corr, zmin=-1, zmax=1, color_continuous_scale="RdBu_r", text_auto=".2f", aspect="auto"
        ),
        width="stretch",
    )


# --------------------------------------------------------------------------- #
# Candidates
# --------------------------------------------------------------------------- #
@page
def candidates() -> None:
    st.title("Candidate ranking")
    feats = q(
        """SELECT f.ticker, f.feature, f.value FROM features_weekly f
           WHERE f.week_end = (SELECT max(week_end) FROM features_weekly)
             AND f.ticker != '_MARKET_'
             AND f.feature IN ('ret_13w', 'ret_26w', 'ret_52w', 'dist_52w_high',
                               'adv_dollar_13w_log')"""
    )
    if feats.empty:
        st.info("No features yet — run **Collect data** then a report from Mission control.")
        return

    week = q("SELECT max(week_end) AS w FROM features_weekly")["w"][0]
    st.caption(f"Composite scorer inputs, week ending {pd.Timestamp(week):%Y-%m-%d}")

    wide = feats.pivot_table(index="ticker", columns="feature", values="value")
    sug = q(
        """SELECT ticker, score FROM suggestions
           WHERE week_end = (SELECT max(week_end) FROM suggestions) AND score IS NOT NULL"""
    )
    if not sug.empty:
        wide = wide.join(sug.groupby("ticker")["score"].max())

    held = set(
        q(
            """SELECT ticker FROM portfolio_snapshots
               WHERE taken_at = (SELECT max(taken_at) FROM portfolio_snapshots)"""
        )["ticker"]
    )
    wide = wide.sort_values("score" if "score" in wide.columns else "ret_26w", ascending=False)
    wide.insert(0, "held", ["★" if t in held else "" for t in wide.index])

    fmt = {c: "{:+.1%}" for c in wide.columns if c.startswith(("ret_", "dist_"))}
    fmt.update({"adv_dollar_13w_log": "{:.1f}", "score": "{:.3f}"})
    st.dataframe(wide.style.format(fmt, na_rep="—"), width="stretch")

    if "score" in wide.columns:
        scored = wide.dropna(subset=["score"]).reset_index()
        fig = px.bar(scored, x="score", y="ticker", orientation="h", color="held")
        fig.update_layout(
            showlegend=False, yaxis={"categoryorder": "total ascending"}, yaxis_title=None
        )
        st.plotly_chart(fig, width="stretch")


# --------------------------------------------------------------------------- #
# Whales
# --------------------------------------------------------------------------- #
@page
def whales() -> None:
    st.title("Whale watch")
    period = q("SELECT max(period) AS p FROM filings_13f")["p"][0]
    if pd.isna(period):
        st.info("No 13F data yet — run **Collect data** from Mission control.")
        return
    st.caption(f"Latest 13F quarter: {pd.Timestamp(period):%Y-%m-%d} (filings lag ~45 days)")

    managers = sorted(q("SELECT DISTINCT manager_name FROM filings_13f")["manager_name"])
    pick = st.multiselect("Managers", managers, default=managers)

    st.subheader("Universe overlap")
    overlap = q(
        """SELECT f.manager_name, f.ticker, f.change_status,
                  round(f.value_usd / 1e6, 1) AS value_mm
           FROM filings_13f f JOIN universe u ON u.ticker = f.ticker AND u.active
           WHERE f.period = (SELECT max(period) FROM filings_13f)
           ORDER BY f.value_usd DESC"""
    )
    overlap = overlap[overlap["manager_name"].isin(pick)]
    if overlap.empty:
        st.caption("No overlap between tracked managers and the universe this quarter.")
    else:
        st.dataframe(overlap, width="stretch", hide_index=True)

    st.subheader("All tracked-manager moves")
    moves = q(
        """SELECT manager_name, coalesce(ticker, issuer) AS name, change_status,
                  round(value_usd / 1e6, 1) AS value_mm
           FROM filings_13f WHERE period = (SELECT max(period) FROM filings_13f)
           ORDER BY value_usd DESC LIMIT 100"""
    )
    st.dataframe(moves[moves["manager_name"].isin(pick)], width="stretch", hide_index=True)

    st.subheader("Insider activity (90 days)")
    ins = q(
        """SELECT ticker, count(*) FILTER (code='P') AS buys,
                  count(*) FILTER (code='S') AS sells
           FROM insider_form4 WHERE tx_date > current_date - INTERVAL 90 DAY
           GROUP BY ticker ORDER BY buys DESC, sells DESC"""
    )
    st.dataframe(ins, width="stretch", hide_index=True)


# --------------------------------------------------------------------------- #
# Trends
# --------------------------------------------------------------------------- #
@page
def trends() -> None:
    st.title("Trends")
    pm = q(
        """SELECT s.ts, s.prob, m.question, m.category FROM polymarket_series s
           JOIN polymarket_markets m ON m.slug = s.slug
           WHERE NOT m.closed ORDER BY s.ts"""
    )
    if pm.empty:
        st.caption("No Polymarket data yet.")
    else:
        st.subheader("Polymarket odds")
        st.caption("Latest probability per market; delta = change over the last 7 days.")
        latest = pm.groupby(["category", "question"], as_index=False).last()
        cutoff = pm["ts"].max() - pd.Timedelta(days=7)
        week_ago = pm[pm["ts"] <= cutoff].groupby("question")["prob"].last()
        for category, group in latest.groupby("category"):
            st.markdown(f"**{category}**")
            cols = st.columns(4)
            for i, (_, row) in enumerate(group.iterrows()):
                prev = week_ago.get(row["question"])
                delta = f"{(row['prob'] - prev) * 100:+.0f} pp" if prev is not None else None
                cols[i % 4].metric(str(row["question"])[:70], f"{row['prob']:.0%}", delta)

        st.subheader("History")
        categories = sorted(pm["category"].unique())
        pick = st.multiselect("Categories", categories, default=categories)
        sub = pm[pm["category"].isin(pick)]
        fig = px.line(sub, x="ts", y="prob", color="question", line_dash="category")
        fig.update_layout(legend={"orientation": "h", "y": -0.35}, yaxis_tickformat=".0%")
        st.plotly_chart(fig, width="stretch")

    macro = q("SELECT series_id, date, value FROM macro_series ORDER BY date")
    if not macro.empty:
        st.subheader("Macro (FRED)")
        options = sorted(macro["series_id"].unique())
        wanted = [s for s in ("T10Y2Y", "BAMLH0A0HYM2") if s in options]
        pick = st.multiselect("FRED series", options, default=wanted or options[:2])
        sub = macro[macro["series_id"].isin(pick)]
        st.plotly_chart(px.line(sub, x="date", y="value", color="series_id"), width="stretch")


# --------------------------------------------------------------------------- #
# Model health
# --------------------------------------------------------------------------- #
@page
def model_health() -> None:
    st.title("Model health")
    runs = q("SELECT created_at, kind, metrics FROM model_runs ORDER BY created_at DESC LIMIT 20")
    if runs.empty:
        st.info("No model runs yet — use **Re-evaluate model** on Mission control.")
    else:
        parsed = pd.json_normalize(runs["metrics"].map(_loads))
        table = pd.concat([runs[["created_at", "kind"]], parsed], axis=1)
        num_cols = [c for c in table.columns if c not in ("created_at", "kind")]
        st.subheader("Recent evaluations")
        st.dataframe(
            table.style.format({c: "{:.4f}" for c in num_cols}, na_rep="—"),
            width="stretch",
            hide_index=True,
        )
        if "rank_ic_mean" in table.columns and table["rank_ic_mean"].notna().sum() > 1:
            st.subheader("Out-of-sample rank-IC over time")
            fig = px.line(
                table.dropna(subset=["rank_ic_mean"]),
                x="created_at",
                y="rank_ic_mean",
                color="kind",
                markers=True,
            )
            st.plotly_chart(fig, width="stretch")

    bts = q(
        "SELECT created_at, config, metrics FROM backtest_runs ORDER BY created_at DESC LIMIT 10"
    )
    st.subheader("Recent backtests")
    st.dataframe(bts, width="stretch", hide_index=True)
    st.caption("Gate: strategy Sharpe must beat equal-weight universe after costs.")


def _loads(raw: object) -> dict:
    try:
        out = json.loads(str(raw))
        return out if isinstance(out, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


# --------------------------------------------------------------------------- #
# Journal
# --------------------------------------------------------------------------- #
@page
def journal() -> None:
    st.title("Journal")
    st.subheader("Suggestions")
    statuses = ["PENDING", "APPROVED", "REJECTED", "SNOOZED", "SUPERSEDED", "EXECUTED"]
    pick = st.multiselect("Status", statuses, default=statuses)
    sug = q(
        """SELECT created_at, week_end, action, ticker, status, decided_at
           FROM suggestions ORDER BY created_at DESC LIMIT 500"""
    )
    st.dataframe(sug[sug["status"].isin(pick)].head(200), width="stretch", hide_index=True)

    st.subheader("Orders")
    st.dataframe(
        q("SELECT * FROM orders ORDER BY created_at DESC LIMIT 200"),
        width="stretch",
        hide_index=True,
    )
