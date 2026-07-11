"""Holdings X-ray: risk, concentration, contribution, and benchmark-relative analytics.

All computations use a *frozen-weights* portfolio (today's weights applied backwards) —
honest for "what does my current book behave like?", not a statement of realized P&L.
Pure pandas/numpy so everything is unit-testable without a browser.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def portfolio_returns(closes: pd.DataFrame, weights: dict[str, float]) -> pd.Series:
    """Daily frozen-weight portfolio returns from a close panel."""
    cols = [t for t in weights if t in closes.columns]
    if not cols:
        return pd.Series(dtype=float)
    w = pd.Series({t: weights[t] for t in cols})
    w = w / w.sum()
    rets = closes[cols].pct_change(fill_method=None)
    return (rets * w).sum(axis=1, min_count=1).dropna()


def growth_frame(
    closes: pd.DataFrame,
    weights: dict[str, float],
    window_days: int,
    benchmarks: list[str] = ["SPY", "QQQ", "SMH"],  # noqa: B006 - read-only default
) -> pd.DataFrame:
    """Cumulative growth (indexed to 100) of the frozen-weight portfolio vs benchmarks."""
    window = closes.tail(window_days + 1)
    out: dict[str, pd.Series] = {}
    port = portfolio_returns(window, weights)
    if not port.empty:
        out["Portfolio"] = (1 + port).cumprod() * 100
    for bench in benchmarks:
        if bench in window.columns:
            rets = window[bench].pct_change(fill_method=None).dropna()
            out[bench] = (1 + rets).cumprod() * 100
    frame = pd.DataFrame(out)
    # Re-anchor at exactly 100 on the first common day.
    return frame / frame.iloc[0] * 100 if len(frame) else frame


def _stats_for(rets: pd.Series, bench: pd.Series | None) -> dict[str, float | None]:
    rets = rets.dropna()
    if len(rets) < 20:
        return {"beta": None, "ann_vol": None, "sharpe": None, "max_dd": None, "corr": None}
    ann_vol = float(rets.std() * np.sqrt(TRADING_DAYS))
    mean_ann = float(rets.mean() * TRADING_DAYS)
    cum = (1 + rets).cumprod()
    max_dd = float((cum / cum.cummax() - 1).min())
    beta = corr = None
    if bench is not None:
        joined = pd.concat([rets, bench], axis=1, join="inner").dropna()
        if len(joined) >= 20 and joined.iloc[:, 1].var() > 0:
            beta = float(joined.iloc[:, 0].cov(joined.iloc[:, 1]) / joined.iloc[:, 1].var())
            corr = float(joined.iloc[:, 0].corr(joined.iloc[:, 1]))
    return {
        "beta": beta,
        "ann_vol": ann_vol,
        "sharpe": (mean_ann / ann_vol) if ann_vol > 0 else None,
        "max_dd": max_dd,
        "corr": corr,
    }


def risk_table(
    closes: pd.DataFrame,
    weights: dict[str, float],
    window_days: int,
    benchmark: str = "SPY",
) -> pd.DataFrame:
    """Per-holding + portfolio + benchmark risk stats over the trailing window."""
    window = closes.tail(window_days + 1)
    rets = window.pct_change(fill_method=None)
    bench = rets.get(benchmark)

    rows: dict[str, dict[str, float | None]] = {}
    for ticker in weights:
        if ticker in rets.columns:
            rows[ticker] = _stats_for(rets[ticker], bench)
    rows["Portfolio"] = _stats_for(portfolio_returns(window, weights), bench)
    if bench is not None:
        rows[benchmark] = _stats_for(bench, bench)
    return pd.DataFrame(rows).T


def contribution(closes: pd.DataFrame, weights: dict[str, float], window_days: int) -> pd.Series:
    """Approximate contribution to portfolio return: weight * window return, per holding."""
    window = closes.tail(window_days + 1)
    out = {}
    total = sum(weights.values()) or 1.0
    for ticker, w in weights.items():
        series = window.get(ticker)
        if series is None or series.dropna().empty:
            continue
        s = series.dropna()
        out[ticker] = (w / total) * float(s.iloc[-1] / s.iloc[0] - 1)
    return pd.Series(out).sort_values()


def correlation_matrix(closes: pd.DataFrame, tickers: list[str], window_days: int) -> pd.DataFrame:
    """Pairwise daily-return correlations over the trailing window."""
    cols = [t for t in tickers if t in closes.columns]
    rets = closes[cols].tail(window_days + 1).pct_change(fill_method=None)
    return rets.corr()


def effective_positions(weights: dict[str, float]) -> float:
    """1 / Herfindahl index — 'how many positions do I really have?'"""
    w = np.array(list(weights.values()), dtype=float)
    w = w / w.sum()
    return float(1.0 / np.sum(w**2))


def insights(
    weights: dict[str, float],
    risk: pd.DataFrame,
    corr: pd.DataFrame,
    contrib: pd.Series,
    benchmark: str = "SPY",
) -> list[str]:
    """Deterministic plain-language observations — computed, never hallucinated."""
    notes: list[str] = []
    w = pd.Series(weights).sort_values(ascending=False)
    top2 = float(w.head(2).sum() / w.sum())
    notes.append(
        f"Concentration: top 2 positions ({', '.join(w.head(2).index)}) are {top2:.0%} of "
        f"the book; effective positions = {effective_positions(weights):.1f} "
        f"(vs {len(weights)} nominal)."
    )

    pairs = corr.where(~np.eye(len(corr), dtype=bool)).stack()
    if not pairs.empty:
        (a, b), val = pairs.idxmax(), float(pairs.max())
        if val > 0.8:
            notes.append(
                f"Diversification: {a} and {b} move nearly in lockstep "
                f"(correlation {val:.2f}) — closer to one position than two."
            )

    port = risk.loc["Portfolio"] if "Portfolio" in risk.index else None
    if port is not None and port.get("beta") is not None:
        beta = port["beta"]
        direction = "amplifies" if beta > 1.1 else ("dampens" if beta < 0.9 else "tracks")
        notes.append(
            f"Market sensitivity: portfolio beta vs {benchmark} is {beta:.2f} — "
            f"it {direction} broad-market moves"
            + (
                f"; max drawdown over the window {port['max_dd']:.0%}."
                if port.get("max_dd") is not None
                else "."
            )
        )

    if not contrib.empty:
        best, worst = contrib.idxmax(), contrib.idxmin()
        notes.append(
            f"Attribution: {best} contributed {contrib.max():+.1%} of portfolio return "
            f"over the window; {worst} contributed {contrib.min():+.1%}."
        )
        dead = [t for t, v in contrib.items() if abs(v) < 0.002 and weights.get(str(t), 0) > 0.02]
        if dead:
            notes.append(
                f"Dead weight: {', '.join(map(str, dead))} — meaningful capital, "
                "negligible contribution over the window."
            )
    return notes
