"""Holdings performance analytics for the dashboard.

Works off the latest account snapshot (positions, avg cost) and stored daily prices.
Portfolio-level returns are the current-holdings price return (weighted by latest market
value) — an honest approximation that ignores intra-period trades and cash drag, labeled
as such in the UI.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb
import pandas as pd

# Trading-day lookbacks.
PERIODS: dict[str, int] = {"1W": 5, "1M": 21, "3M": 63, "6M": 126, "1Y": 252}


@dataclass
class HoldingsView:
    taken_at: pd.Timestamp
    net_liquidation: float
    table: pd.DataFrame  # per holding: qty, avg_cost, price, value, weight, pnl, returns
    portfolio_returns: dict[str, float]  # period -> weighted return
    benchmark_returns: dict[str, float]  # period -> SPY return
    closes: pd.DataFrame  # daily closes for held tickers + SPY (for charts)


def latest_snapshot(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    return con.execute(
        """SELECT ticker, quantity, avg_cost, net_liquidation, taken_at
           FROM portfolio_snapshots
           WHERE taken_at = (SELECT max(taken_at) FROM portfolio_snapshots)
           ORDER BY ticker"""
    ).df()


def daily_closes(con: duckdb.DuckDBPyConnection, tickers: list[str]) -> pd.DataFrame:
    placeholders = ", ".join("?" for _ in tickers)
    df = con.execute(
        f"SELECT ticker, date, coalesce(adj_close, close) AS px "
        f"FROM prices_daily WHERE ticker IN ({placeholders}) ORDER BY date",
        tickers,
    ).df()
    if df.empty:
        return pd.DataFrame()
    df["date"] = pd.to_datetime(df["date"])
    return df.pivot_table(index="date", columns="ticker", values="px").ffill()


def period_return(series: pd.Series, days: int) -> float | None:
    """Return over the trailing `days` trading days; None if history is too short."""
    s = series.dropna()
    if len(s) <= days:
        return None
    return float(s.iloc[-1] / s.iloc[-1 - days] - 1)


def holdings_view(con: duckdb.DuckDBPyConnection) -> HoldingsView | None:
    """Assemble the full holdings analytics view; None if no snapshot exists."""
    snap = latest_snapshot(con)
    if snap.empty:
        return None
    tickers = snap["ticker"].tolist()
    closes = daily_closes(con, [*tickers, "SPY"])
    if closes.empty:
        return None

    rows = []
    for r in snap.itertuples(index=False):
        series = closes.get(r.ticker)
        price = (
            float(series.dropna().iloc[-1])
            if series is not None and series.notna().any()
            else float(r.avg_cost)
        )
        value = float(r.quantity) * price
        cost = float(r.quantity) * float(r.avg_cost)
        row: dict[str, object] = {
            "ticker": r.ticker,
            "qty": float(r.quantity),
            "avg_cost": float(r.avg_cost),
            "price": price,
            "value": value,
            "pnl": value - cost,
            "pnl_pct": (value / cost - 1) if cost else None,
        }
        for name, days in PERIODS.items():
            row[name] = period_return(series, days) if series is not None else None
        rows.append(row)
    table = pd.DataFrame(rows)
    total_value = float(table["value"].sum())
    table["weight"] = table["value"] / total_value if total_value else 0.0

    portfolio_returns: dict[str, float] = {}
    benchmark_returns: dict[str, float] = {}
    spy = closes.get("SPY")
    for name in PERIODS:
        rets = table[["weight", name]].dropna()
        if not rets.empty:
            portfolio_returns[name] = float(
                (rets["weight"] * rets[name]).sum() / rets["weight"].sum()
            )
        if spy is not None:
            spy_ret = period_return(spy, PERIODS[name])
            if spy_ret is not None:
                benchmark_returns[name] = spy_ret

    net_liq = float(snap["net_liquidation"].iloc[0] or total_value)
    return HoldingsView(
        taken_at=pd.Timestamp(snap["taken_at"].iloc[0]),
        net_liquidation=net_liq,
        table=table,
        portfolio_returns=portfolio_returns,
        benchmark_returns=benchmark_returns,
        closes=closes,
    )


def normalized_window(closes: pd.DataFrame, days: int) -> pd.DataFrame:
    """Last `days` trading days of closes, each series indexed to 100 at window start."""
    window = closes.tail(days).dropna(axis=1, how="all")
    first = window.apply(lambda s: s.dropna().iloc[0] if s.notna().any() else None)
    return window / first * 100.0
