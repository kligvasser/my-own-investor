"""``moi`` command-line interface (typer)."""

from __future__ import annotations

import typer

from moi.config import get_settings
from moi.logging import configure_logging, get_logger

app = typer.Typer(
    name="moi",
    help="my-own-investor — AI-assisted, human-approved IBKR portfolio copilot.",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(help="Database and migrations.", no_args_is_help=True)
ibkr_app = typer.Typer(help="Interactive Brokers connectivity.", no_args_is_help=True)
collect_app = typer.Typer(help="Data collectors.", no_args_is_help=True)
universe_app = typer.Typer(help="Investment universe.", no_args_is_help=True)
features_app = typer.Typer(help="Feature engineering.", no_args_is_help=True)
ml_app = typer.Typer(help="Model training and evaluation.", no_args_is_help=True)
backtest_app = typer.Typer(help="Backtesting.", no_args_is_help=True)
app.add_typer(db_app, name="db")
app.add_typer(ibkr_app, name="ibkr")
app.add_typer(collect_app, name="collect")
app.add_typer(universe_app, name="universe")
app.add_typer(features_app, name="features")
app.add_typer(ml_app, name="ml")
app.add_typer(backtest_app, name="backtest")

log = get_logger(__name__)


@app.callback()
def _root(verbose: bool = typer.Option(False, "--verbose", "-v", help="Debug logging.")) -> None:
    settings = get_settings()
    configure_logging(level="DEBUG" if verbose else settings.log_level, json=settings.log_json)


# --------------------------------------------------------------------------- #
# db
# --------------------------------------------------------------------------- #
@db_app.command("init")
def db_init() -> None:
    """Create the database and apply all migrations."""
    from moi.db import connect

    con = connect()
    version = con.execute("SELECT max(version) FROM schema_version").fetchone()
    v = version[0] if version else 0
    typer.echo(f"Database ready at {get_settings().db_path} (schema v{v}).")


# --------------------------------------------------------------------------- #
# universe
# --------------------------------------------------------------------------- #
@universe_app.command("sync")
def universe_sync() -> None:
    """Load config/universe.yaml into the database."""
    from moi.db import connect
    from moi.universe import sync_universe

    con = connect()
    n = sync_universe(con)
    typer.echo(f"Synced {n} instruments into the universe table.")


@universe_app.command("list")
def universe_list() -> None:
    """Print the current universe."""
    from moi.db import connect

    con = connect()
    rows = con.execute(
        "SELECT ticker, sub_sector, is_benchmark FROM universe WHERE active "
        "ORDER BY is_benchmark, sub_sector, ticker"
    ).fetchall()
    for ticker, sub, bench in rows:
        tag = "benchmark" if bench else (sub or "-")
        typer.echo(f"  {ticker:6}  {tag}")
    typer.echo(f"\n{len(rows)} active instruments.")


# --------------------------------------------------------------------------- #
# ibkr
# --------------------------------------------------------------------------- #
@ibkr_app.command("ping")
def ibkr_ping() -> None:
    """Connect to IB Gateway/TWS, print account summary, disconnect."""
    from moi.ingest.ibkr import ping

    try:
        info = ping()
    except ConnectionError as exc:
        typer.secho(str(exc), fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    typer.secho(f"Connected. Account: {info.account}", fg=typer.colors.GREEN)
    typer.echo(f"  Net liquidation: {info.net_liquidation}")
    typer.echo(f"  Total cash:      {info.total_cash}")
    typer.echo(f"  Open positions:  {len(info.positions)}")
    for sym, pos, cost in info.positions:
        typer.echo(f"    {sym:6} qty={pos:>10} avg_cost={cost}")


# --------------------------------------------------------------------------- #
# collect
# --------------------------------------------------------------------------- #
@collect_app.command("prices")
def collect_prices_cmd(
    source: str = typer.Option("yfinance", help="Price source: yfinance | ibkr."),
    years: int | None = typer.Option(None, help="History window; defaults to config."),
) -> None:
    """Backfill/refresh daily OHLCV for the universe."""
    from moi.db import connect
    from moi.ingest.prices import collect_prices
    from moi.universe import sync_universe

    settings = get_settings()
    con = connect()
    sync_universe(con)  # ensure universe table is current before collecting
    written = collect_prices(con, years=years or settings.price_history_years, source=source)
    typer.secho(f"Upserted {written} price rows from {source}.", fg=typer.colors.GREEN)


@collect_app.command("13f")
def collect_13f_cmd() -> None:
    """Fetch whale 13F holdings (config/whales.yaml) with QoQ change status."""
    from moi.db import connect
    from moi.ingest.edgar_13f import collect_13f

    written = collect_13f(connect())
    typer.secho(f"Upserted {written} 13F holding rows.", fg=typer.colors.GREEN)


@collect_app.command("form4")
def collect_form4_cmd(
    per_ticker: int = typer.Option(10, help="Recent Form 4 filings per ticker."),
) -> None:
    """Fetch insider Form 4 transactions for universe tickers."""
    from moi.db import connect
    from moi.ingest.edgar_form4 import collect_form4

    written = collect_form4(connect(), filings_per_ticker=per_ticker)
    typer.secho(f"Upserted {written} insider transaction rows.", fg=typer.colors.GREEN)


@collect_app.command("congress")
def collect_congress_cmd() -> None:
    """Fetch congressional trade disclosures (needs a Quiver/Unusual Whales API key)."""
    from moi.db import connect
    from moi.ingest.congress import collect_congress

    written = collect_congress(connect())
    typer.secho(f"Upserted {written} congress trade rows.", fg=typer.colors.GREEN)


@collect_app.command("polymarket")
def collect_polymarket_cmd() -> None:
    """Fetch Polymarket probability series (config/polymarket.yaml)."""
    from moi.db import connect
    from moi.ingest.polymarket import collect_polymarket

    written = collect_polymarket(connect())
    typer.secho(f"Upserted {written} probability points.", fg=typer.colors.GREEN)


@collect_app.command("news")
def collect_news_cmd() -> None:
    """Fetch news headlines (per-ticker + sector RSS feeds)."""
    from moi.db import connect
    from moi.ingest.news import collect_news

    written = collect_news(connect())
    typer.secho(f"Ingested {written} news items.", fg=typer.colors.GREEN)


@collect_app.command("macro")
def collect_macro_cmd() -> None:
    """Fetch FRED macro series (needs MOI_FRED_API_KEY)."""
    from moi.db import connect
    from moi.ingest.macro import collect_macro

    written = collect_macro(connect())
    typer.secho(f"Upserted {written} macro points.", fg=typer.colors.GREEN)


@collect_app.command("all")
def collect_all_cmd() -> None:
    """Run every collector in sequence (nightly job). Failures don't abort the run."""
    from moi.db import connect
    from moi.universe import sync_universe

    settings = get_settings()
    con = connect()
    sync_universe(con)

    def _step(name: str, fn: typing.Callable[[], int]) -> tuple[str, str]:
        try:
            return name, f"ok ({fn()} rows)"
        except Exception as exc:
            log.error("collect_step_failed", step=name, error=str(exc))
            return name, f"ERROR: {exc}"

    import typing

    from moi.ingest.congress import collect_congress
    from moi.ingest.edgar_13f import collect_13f
    from moi.ingest.edgar_form4 import collect_form4
    from moi.ingest.macro import collect_macro
    from moi.ingest.news import collect_news
    from moi.ingest.polymarket import collect_polymarket
    from moi.ingest.prices import collect_prices

    results = [
        _step("prices", lambda: collect_prices(con, years=settings.price_history_years)),
        _step("13f", lambda: collect_13f(con)),
        _step("form4", lambda: collect_form4(con)),
        _step("congress", lambda: collect_congress(con)),
        _step("polymarket", lambda: collect_polymarket(con)),
        _step("news", lambda: collect_news(con)),
        _step("macro", lambda: collect_macro(con)),
    ]
    typer.echo("\ncollect all — summary")
    failed = False
    for name, outcome in results:
        color = typer.colors.GREEN if outcome.startswith("ok") else typer.colors.RED
        if not outcome.startswith("ok"):
            failed = True
        typer.secho(f"  {name:12} {outcome}", fg=color)
    if failed:
        raise typer.Exit(code=1)


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# features / ml / backtest
# --------------------------------------------------------------------------- #
@features_app.command("build")
def features_build() -> None:
    """Compute all weekly features (momentum, whales, market) into the feature store."""
    from moi.db import connect
    from moi.features.store import build_features

    written = build_features(connect())
    typer.secho(f"Upserted {written} feature values.", fg=typer.colors.GREEN)


@ml_app.command("train")
def ml_train() -> None:
    """Evaluate the composite scorer and the LightGBM challenger; print IC metrics."""
    from moi.db import connect
    from moi.ml.composite import evaluate_composite
    from moi.ml.ranker import train_walkforward

    con = connect()
    _, comp_metrics = evaluate_composite(con)
    typer.echo("composite (production scorer):")
    for k, v in comp_metrics.items():
        typer.echo(f"  {k:22} {v:.4f}")

    result = train_walkforward(con)
    typer.echo("lightgbm challenger (walk-forward, out-of-sample):")
    for k, v in result.metrics.items():
        typer.echo(f"  {k:22} {v:.4f}")
    if result.metrics["rank_ic_mean"] > comp_metrics["rank_ic_mean"]:
        typer.secho("challenger beats composite — consider promotion", fg=typer.colors.YELLOW)
    else:
        typer.echo("composite remains the production scorer")


@ml_app.command("scores")
def ml_scores() -> None:
    """Print the latest weekly candidate ranking from the production scorer."""
    from moi.db import connect
    from moi.ml.composite import latest_scores

    ranked = latest_scores(connect())
    week = ranked["week_end"].iloc[0].date() if len(ranked) else "?"
    typer.echo(f"composite ranking for week ending {week}:")
    for i, row in ranked.iterrows():
        typer.echo(f"  {i + 1:>2}. {row.ticker:6} score={row.score:.3f}")


@ml_app.command("portfolio")
def ml_portfolio(
    top_n: int = typer.Option(12, help="Max positions."),
    max_sector_share: float = typer.Option(0.30, help="Max share of book per sub-sector."),
) -> None:
    """Print the current target portfolio (scores + sector caps + regime scaling)."""
    from moi.db import connect
    from moi.ml.portfolio import build_portfolio

    p = build_portfolio(connect(), top_n=top_n, max_sector_share=max_sector_share)
    typer.echo(
        f"target portfolio for week ending {p.week_end.date()} "
        f"(regime: {p.regime.name}, gross {p.regime.gross:.0%}):"
    )
    for pos in p.positions:
        typer.echo(f"  {pos.ticker:6} {pos.weight:6.1%}  {pos.sub_sector:28} score={pos.score:.3f}")
    typer.echo(f"  {'CASH':6} {p.cash_weight:6.1%}")


@backtest_app.command("run")
def backtest_run(
    scorer: str = typer.Option("composite", help="Scorer: composite | lgbm."),
    top_n: int = typer.Option(12, help="Positions held."),
    rebalance_weeks: int = typer.Option(4, help="Weeks between rebalances."),
    cost_bps: float = typer.Option(15.0, help="Per-side cost in basis points."),
) -> None:
    """End-to-end: scores → cost-aware backtest vs baselines → report in docs/backtests/."""
    from datetime import date as _date

    import pandas as pd

    from moi.backtest.engine import BacktestConfig, gate_passed, render_report, run_backtest
    from moi.config import ROOT
    from moi.db import connect

    con = connect()
    if scorer == "lgbm":
        from moi.ml.ranker import train_walkforward

        wf = train_walkforward(con)
        predictions, model_metrics, importances = wf.predictions, wf.metrics, wf.importances
    else:
        from moi.ml.composite import COMPOSITE_SPEC, evaluate_composite

        predictions, model_metrics = evaluate_composite(con)
        predictions = predictions.dropna(subset=["label"])
        importances = pd.Series({feat: abs(sign) for feat, sign in COMPOSITE_SPEC})

    cfg = BacktestConfig(top_n=top_n, rebalance_weeks=rebalance_weeks, cost_bps_per_side=cost_bps)
    result = run_backtest(con, predictions, cfg)

    report = render_report(result, model_metrics, importances)
    out_dir = ROOT / "docs" / "backtests" / _date.today().isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"report-{result.run_id}.md"
    out_path.write_text(report)

    strat, ew = result.metrics["strategy"], result.metrics["equal_weight_universe"]
    typer.echo(
        f"strategy: ann {strat['ann_return']:+.1%}, sharpe {strat['sharpe']:.2f}, "
        f"maxDD {strat['max_drawdown']:.1%}"
    )
    typer.echo(f"eq-weight: ann {ew['ann_return']:+.1%}, sharpe {ew['sharpe']:.2f}")
    if gate_passed(result.metrics):
        typer.secho(f"GATE PASSED — report: {out_path}", fg=typer.colors.GREEN)
    else:
        typer.secho(f"GATE NOT PASSED — report: {out_path}", fg=typer.colors.RED)
        raise typer.Exit(code=2)


# --------------------------------------------------------------------------- #
# approval / execution
# --------------------------------------------------------------------------- #
@app.command("approve")
def approve(suggestion_id: str) -> None:
    """Approve a pending suggestion (makes it executable)."""
    from moi.db import connect
    from moi.execute.queue import decide

    ok = decide(connect(), suggestion_id, "APPROVED")
    typer.secho(
        "Approved." if ok else "Not found or not pending.",
        fg=typer.colors.GREEN if ok else typer.colors.RED,
    )


@app.command("reject")
def reject(suggestion_id: str) -> None:
    """Reject a pending suggestion."""
    from moi.db import connect
    from moi.execute.queue import decide

    ok = decide(connect(), suggestion_id, "REJECTED")
    typer.secho(
        "Rejected." if ok else "Not found or not pending.",
        fg=typer.colors.GREEN if ok else typer.colors.RED,
    )


@app.command("kill")
def kill(state: str = typer.Argument(..., help="on | off")) -> None:
    """Set the kill switch. 'on' blocks all order placement immediately."""
    from moi.db import connect
    from moi.execute.executor import set_kill_switch

    if state not in ("on", "off"):
        typer.secho("state must be 'on' or 'off'", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    set_kill_switch(connect(), state == "on")
    typer.secho(
        f"Kill switch {state.upper()}.",
        fg=typer.colors.RED if state == "on" else typer.colors.GREEN,
    )


@app.command("execute")
def execute() -> None:
    """Place orders for APPROVED suggestions (paper account only unless allow_live)."""
    from moi.db import connect
    from moi.execute.executor import SafetyError, execute_approved

    try:
        results = execute_approved(connect())
    except (SafetyError, ConnectionError) as exc:
        typer.secho(f"BLOCKED: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    for line in results:
        color = typer.colors.GREEN if line.startswith("SUBMITTED") else typer.colors.YELLOW
        typer.secho(f"  {line}", fg=color)


@app.command("dashboard")
def dashboard() -> None:
    """Launch the Streamlit dashboard."""
    import subprocess

    from moi.config import ROOT

    subprocess.run(["streamlit", "run", str(ROOT / "dashboard" / "app.py")], check=False)


# --------------------------------------------------------------------------- #
# weekly / watch
# --------------------------------------------------------------------------- #
@app.command("weekly")
def weekly(
    no_llm: bool = typer.Option(False, "--no-llm", help="Numbers-only report (skip agents)."),
    top_n: int = typer.Option(12, help="Max positions in the target portfolio."),
) -> None:
    """Run the full weekly pipeline: features → portfolio → suggestions → report."""
    from moi.db import connect
    from moi.report.weekly import run_weekly

    path = run_weekly(connect(), with_llm=not no_llm, top_n=top_n)
    typer.secho(f"Weekly report written: {path}", fg=typer.colors.GREEN)


@app.command("watch")
def watch() -> None:
    """Check urgent triggers (big moves, fresh whale filings, data quality)."""
    from moi.db import connect
    from moi.orchestrator.watch import run_watch

    alerts = run_watch(connect())
    if alerts:
        for a in alerts:
            typer.secho(f"  [{a.kind}] {a.message}", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)
    typer.secho("No urgent alerts.", fg=typer.colors.GREEN)


@app.command("queue")
def queue() -> None:
    """Show pending suggestions (the approval queue)."""
    from moi.db import connect

    rows = (
        connect()
        .execute(
            """SELECT id, week_end, action, ticker, current_weight, target_weight, confidence
           FROM suggestions WHERE status = 'PENDING' ORDER BY created_at DESC"""
        )
        .fetchall()
    )
    if not rows:
        typer.echo("Queue is empty.")
        return
    for sid, week, action, ticker, cw, tw, conf in rows:
        typer.echo(
            f"  {sid}  {week}  {action:5} {ticker:6} {cw or 0:.1%} -> {tw or 0:.1%}  ({conf})"
        )
    typer.echo(f"\n{len(rows)} pending. (approval UI arrives in Phase 4)")


@app.command("status")
def status() -> None:
    """Print a data-freshness board (green/red per table) and recent runs."""
    from moi.db import connect, scalar
    from moi.ingest.quality import check_freshness

    con = connect()
    universe_n = scalar(con, "SELECT count(*) FROM universe WHERE active")
    typer.echo("data status")
    typer.echo(f"  universe (active)  : {universe_n}")

    state_colors = {
        "ok": typer.colors.GREEN,
        "stale": typer.colors.RED,
        "empty": typer.colors.RED,
        "skipped": typer.colors.YELLOW,
    }
    for ts in check_freshness(con):
        typer.secho(
            f"  {ts.table:18} : {ts.state:7} {ts.rows:>7} rows, latest {ts.latest}",
            fg=state_colors.get(ts.state, typer.colors.YELLOW),
        )

    last_runs = con.execute(
        "SELECT job, status, rows_written, finished_at FROM run_log "
        "ORDER BY started_at DESC LIMIT 8"
    ).fetchall()
    if last_runs:
        typer.echo("recent runs")
        status_colors = {"ok": typer.colors.GREEN, "error": typer.colors.RED}
        for job, st, rows, fin in last_runs:
            c = status_colors.get(st, typer.colors.YELLOW)
            typer.secho(f"  {job:20} {st:7} rows={rows} at {fin}", fg=c)


if __name__ == "__main__":
    app()
