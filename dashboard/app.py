"""my-own-investor dashboard (Streamlit).

Read-mostly views over the DuckDB store; the only writes are approval-queue decisions
and the kill switch. Connections are short-lived per operation because DuckDB is
single-writer (see docs/SETUP.md).

Run: `moi dashboard`  (or `streamlit run dashboard/app.py`)
"""

from __future__ import annotations

import duckdb
import pandas as pd
import streamlit as st

from moi.config import ROOT, get_settings

st.set_page_config(page_title="my-own-investor", page_icon="📈", layout="wide")


def q(sql: str, params: list | None = None) -> pd.DataFrame:
    con = duckdb.connect(str(get_settings().db_path), read_only=True)
    try:
        return con.execute(sql, params or []).df()
    finally:
        con.close()


def execute_write(fn) -> None:
    from moi.db import connect

    con = connect()
    try:
        fn(con)
    finally:
        con.close()


PAGES = [
    "Weekly report",
    "Approval queue",
    "Portfolio",
    "Holdings X-ray",
    "Candidates",
    "Whales",
    "Trends",
    "Model health",
    "Journal",
]
page = st.sidebar.radio("moi", PAGES)
st.sidebar.caption("Model output — not financial advice.")

kill = q("SELECT value FROM controls WHERE key = 'kill_switch'")
kill_on = not kill.empty and kill.iloc[0, 0] == "on"
if kill_on:
    st.sidebar.error("KILL SWITCH ON — trading blocked")
if st.sidebar.button("Kill switch " + ("OFF" if kill_on else "ON")):
    from moi.execute.executor import set_kill_switch

    execute_write(lambda con: set_kill_switch(con, not kill_on))
    st.rerun()


# --------------------------------------------------------------------------- #
if page == "Weekly report":
    reports = sorted((ROOT / "reports").glob("*.md"), reverse=True)
    if not reports:
        st.info("No reports yet — run `moi weekly`.")
    else:
        pick = st.selectbox("Report", [p.name for p in reports])
        st.markdown((ROOT / "reports" / pick).read_text())

elif page == "Approval queue":
    st.header("Approval queue")
    rows = q(
        """SELECT id, week_end, action, ticker, current_weight, target_weight,
                  score, thesis, bear_case, confidence
           FROM suggestions WHERE status = 'PENDING' ORDER BY created_at DESC"""
    )
    if rows.empty:
        st.success("Queue is empty.")
    for _, r in rows.iterrows():
        with st.container(border=True):
            left, right = st.columns([4, 1])
            with left:
                st.subheader(f"{r['action']} {r['ticker']}")
                st.caption(
                    f"{r['current_weight']:.1%} → {r['target_weight']:.1%} · "
                    f"score {r['score'] if pd.notna(r['score']) else '—'} · {r['confidence']}"
                )
                if r["thesis"]:
                    st.markdown(f"**Thesis:** {r['thesis']}")
                if r["bear_case"]:
                    st.markdown(f"**Bear case:** {r['bear_case']}")
            with right:
                from moi.execute.queue import decide

                sid = r["id"]
                if st.button("✅ Approve", key=f"a{sid}"):
                    execute_write(lambda con, s=sid: decide(con, s, "APPROVED"))
                    st.rerun()
                if st.button("❌ Reject", key=f"r{sid}"):
                    execute_write(lambda con, s=sid: decide(con, s, "REJECTED"))
                    st.rerun()
                if st.button("💤 Snooze", key=f"s{sid}"):
                    execute_write(lambda con, s=sid: decide(con, s, "SNOOZED"))
                    st.rerun()
    approved = q(
        """SELECT count(*) AS n FROM suggestions s WHERE s.status = 'APPROVED'
           AND NOT EXISTS (SELECT 1 FROM orders o WHERE o.suggestion_id = s.id
                           AND o.status != 'error')"""
    )
    n_ready = int(approved["n"].iloc[0])
    if n_ready:
        st.warning(f"{n_ready} approved suggestion(s) awaiting `moi execute` (run from terminal).")

elif page == "Portfolio":
    st.header("My holdings")
    import duckdb as _duckdb

    from moi.report.performance import PERIODS, holdings_view, normalized_window

    _con = _duckdb.connect(str(get_settings().db_path), read_only=True)
    try:
        view = holdings_view(_con)
    finally:
        _con.close()

    if view is None:
        st.info("No account snapshot yet — run `moi weekly` (with TWS running) to capture one.")
    else:
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
        import plotly.express as px

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

elif page == "Holdings X-ray":
    st.header("Holdings X-ray")
    st.caption(
        "How does the current book behave as a whole? Frozen-weights analysis: "
        "today's weights applied backwards — behavior of the book, not realized P&L."
    )
    import duckdb as _duckdb
    import plotly.express as px

    from moi.report.performance import holdings_view
    from moi.report.xray import (
        contribution,
        correlation_matrix,
        growth_frame,
        insights,
        risk_table,
    )

    _con = _duckdb.connect(str(get_settings().db_path), read_only=True)
    try:
        view = holdings_view(_con)
    finally:
        _con.close()

    if view is None:
        st.info("No account snapshot yet — run `moi weekly` (with TWS running) first.")
    else:
        weights = dict(zip(view.table["ticker"], view.table["weight"], strict=True))
        WINDOWS = {"3M": 63, "6M": 126, "1Y": 252, "3Y": 756}
        label = st.radio(
            "Window", list(WINDOWS), index=2, horizontal=True, label_visibility="collapsed"
        )
        days = WINDOWS[label]

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
        st.caption("weight * return over the window — who actually moved the book.")
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
                corr,
                zmin=-1,
                zmax=1,
                color_continuous_scale="RdBu_r",
                text_auto=".2f",
                aspect="auto",
            ),
            width="stretch",
        )

elif page == "Candidates":
    st.header("Candidate ranking (latest week)")
    feats = q(
        """SELECT f.ticker, f.feature, f.value FROM features_weekly f
           WHERE f.week_end = (SELECT max(week_end) FROM features_weekly)
             AND f.ticker != '_MARKET_'
             AND f.feature IN ('ret_13w', 'ret_26w', 'ret_52w', 'dist_52w_high',
                               'adv_dollar_13w_log')"""
    )
    if feats.empty:
        st.info("Run `moi features build` first.")
    else:
        wide = feats.pivot_table(index="ticker", columns="feature", values="value")
        sug = q(
            """SELECT ticker, score FROM suggestions
               WHERE week_end = (SELECT max(week_end) FROM suggestions) AND score IS NOT NULL"""
        )
        if not sug.empty:
            wide = wide.join(sug.set_index("ticker")["score"])
        st.dataframe(
            wide.sort_values("score" if "score" in wide.columns else "ret_26w", ascending=False),
            width="stretch",
        )

elif page == "Whales":
    st.header("Whale watch")
    st.subheader("Universe overlap (latest quarter)")
    overlap = q(
        """SELECT f.manager_name, f.ticker, f.change_status, f.value_usd
           FROM filings_13f f JOIN universe u ON u.ticker = f.ticker AND u.active
           WHERE f.period = (SELECT max(period) FROM filings_13f)
           ORDER BY f.value_usd DESC"""
    )
    st.dataframe(overlap, width="stretch")
    st.subheader("All tracked-manager moves (latest quarter)")
    moves = q(
        """SELECT manager_name, coalesce(ticker, issuer) AS name, change_status,
                  round(value_usd / 1e6, 1) AS value_mm
           FROM filings_13f WHERE period = (SELECT max(period) FROM filings_13f)
           ORDER BY value_usd DESC LIMIT 50"""
    )
    st.dataframe(moves, width="stretch")
    st.subheader("Insider activity (90 days)")
    ins = q(
        """SELECT ticker, count(*) FILTER (code='P') AS buys,
                  count(*) FILTER (code='S') AS sells
           FROM insider_form4 WHERE tx_date > current_date - INTERVAL 90 DAY
           GROUP BY ticker ORDER BY buys DESC, sells DESC"""
    )
    st.dataframe(ins, width="stretch")

elif page == "Trends":
    st.header("Trends")
    pm = q(
        """SELECT s.ts, s.prob, m.question FROM polymarket_series s
           JOIN polymarket_markets m ON m.slug = s.slug ORDER BY s.ts"""
    )
    if not pm.empty:
        import plotly.express as px

        fig = px.line(pm, x="ts", y="prob", color="question", title="Polymarket probabilities")
        fig.update_layout(legend={"orientation": "h", "y": -0.2})
        st.plotly_chart(fig, width="stretch")
    macro = q("SELECT series_id, date, value FROM macro_series ORDER BY date")
    if not macro.empty:
        import plotly.express as px

        pick = st.multiselect(
            "FRED series", sorted(macro["series_id"].unique()), default=["T10Y2Y", "BAMLH0A0HYM2"]
        )
        sub = macro[macro["series_id"].isin(pick)]
        st.plotly_chart(px.line(sub, x="date", y="value", color="series_id"), width="stretch")

elif page == "Model health":
    st.header("Model health")
    runs = q("SELECT created_at, kind, metrics FROM model_runs ORDER BY created_at DESC LIMIT 10")
    st.subheader("Recent model runs")
    st.dataframe(runs, width="stretch")
    bts = q(
        "SELECT created_at, config, metrics FROM backtest_runs ORDER BY created_at DESC LIMIT 10"
    )
    st.subheader("Recent backtests")
    st.dataframe(bts, width="stretch")
    st.caption("Gate: strategy Sharpe must beat equal-weight universe after costs.")

elif page == "Journal":
    st.header("Journal")
    st.subheader("Suggestions")
    st.dataframe(
        q(
            """SELECT created_at, week_end, action, ticker, status, decided_at
               FROM suggestions ORDER BY created_at DESC LIMIT 200"""
        ),
        width="stretch",
    )
    st.subheader("Orders")
    st.dataframe(q("SELECT * FROM orders ORDER BY created_at DESC LIMIT 200"), width="stretch")
    st.subheader("Pipeline runs")
    st.dataframe(
        q(
            """SELECT started_at, job, status, rows_written, detail
               FROM run_log ORDER BY started_at DESC LIMIT 100"""
        ),
        width="stretch",
    )
