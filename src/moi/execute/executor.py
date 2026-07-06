"""Order executor — the ONLY code path that can place a trade.

Hard rails, enforced in code and tested in tests/test_executor.py:
    1. Only suggestions with status = 'APPROVED' are considered (queue.approved_unexecuted).
    2. Ticker must be an active, non-benchmark universe member (whitelist).
    3. No shorting: sells are capped at the currently held quantity.
    4. Per-order and per-day dollar caps from settings.
    5. Kill switch ('moi kill on') blocks everything.
    6. Only paper accounts (DU...) unless settings.allow_live is explicitly true.
Every order is journaled to ``orders`` before submission and updated after.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

import duckdb

from moi.config import get_settings
from moi.logging import get_logger
from moi.runlog import new_run_id

log = get_logger(__name__)

SLIPPAGE_GUARD = 0.01  # limit price: last close ±1%


class SafetyError(RuntimeError):
    """Raised when a planned order violates a hard rail. Never bypassed."""


# --------------------------------------------------------------------------- #
# Controls
# --------------------------------------------------------------------------- #
def kill_switch_on(con: duckdb.DuckDBPyConnection) -> bool:
    row = con.execute("SELECT value FROM controls WHERE key = 'kill_switch'").fetchone()
    return row is not None and row[0] == "on"


def set_kill_switch(con: duckdb.DuckDBPyConnection, on: bool) -> None:
    con.execute(
        "INSERT OR REPLACE INTO controls (key, value, updated_at) VALUES ('kill_switch', ?, ?)",
        ["on" if on else "off", datetime.now()],
    )
    log.warning("kill_switch_set", on=on)


# --------------------------------------------------------------------------- #
# Planning (pure decision logic — fully unit-tested, no broker needed)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PlannedOrder:
    suggestion_id: str
    ticker: str
    side: str  # BUY | SELL
    quantity: int
    limit_price: float
    est_value: float


def plan_order(
    con: duckdb.DuckDBPyConnection,
    *,
    suggestion_id: str,
    ticker: str,
    action: str,
    current_weight: float,
    target_weight: float,
    net_liquidation: float,
    held_quantity: float,
) -> PlannedOrder:
    """Validate a single approved suggestion against every rail and size the order."""
    settings = get_settings()

    if kill_switch_on(con):
        raise SafetyError("kill switch is ON — all trading blocked")

    status = con.execute("SELECT status FROM suggestions WHERE id = ?", [suggestion_id]).fetchone()
    if status is None or status[0] != "APPROVED":
        raise SafetyError(f"suggestion {suggestion_id} is not APPROVED")

    whitelisted = con.execute(
        "SELECT 1 FROM universe WHERE ticker = ? AND active AND NOT is_benchmark",
        [ticker],
    ).fetchone()
    if not whitelisted:
        raise SafetyError(f"{ticker} is not in the tradable universe whitelist")

    price_row = con.execute(
        "SELECT close FROM prices_daily WHERE ticker = ? ORDER BY date DESC LIMIT 1",
        [ticker],
    ).fetchone()
    if not price_row or not price_row[0]:
        raise SafetyError(f"no price on file for {ticker}")
    price = float(price_row[0])

    delta_value = (target_weight - current_weight) * net_liquidation
    side = "BUY" if action in ("BUY", "ADD") else "SELL"
    if side == "SELL":
        delta_value = -abs(delta_value) if delta_value == 0 else delta_value
    quantity = math.floor(abs(delta_value) / price)
    if quantity < 1:
        raise SafetyError(f"{ticker}: computed quantity is zero (delta ${delta_value:.0f})")

    if side == "SELL":
        if held_quantity <= 0:
            raise SafetyError(f"{ticker}: nothing held — shorting is not allowed")
        quantity = min(quantity, math.floor(held_quantity))  # never below zero position

    limit = price * (1 + SLIPPAGE_GUARD) if side == "BUY" else price * (1 - SLIPPAGE_GUARD)
    est_value = quantity * price
    if est_value > settings.max_order_usd:
        raise SafetyError(
            f"{ticker}: order ${est_value:,.0f} exceeds max_order_usd "
            f"${settings.max_order_usd:,.0f}"
        )

    from moi.db import scalar

    today_total = float(
        scalar(
            con,
            """SELECT coalesce(sum(est_value), 0) FROM orders
               WHERE created_at >= current_date AND status != 'error'""",
        )
    )
    if today_total + est_value > settings.max_daily_usd:
        raise SafetyError(
            f"daily total ${today_total + est_value:,.0f} would exceed "
            f"max_daily_usd ${settings.max_daily_usd:,.0f}"
        )

    return PlannedOrder(
        suggestion_id=suggestion_id,
        ticker=ticker,
        side=side,
        quantity=quantity,
        limit_price=round(limit, 2),
        est_value=est_value,
    )


def journal_order(
    con: duckdb.DuckDBPyConnection, plan: PlannedOrder, account: str, status: str, detail: str = ""
) -> str:
    order_id = new_run_id()
    con.execute(
        """INSERT INTO orders (order_id, suggestion_id, created_at, account, ticker, side,
                               quantity, limit_price, est_value, status, detail)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            order_id,
            plan.suggestion_id,
            datetime.now(),
            account,
            plan.ticker,
            plan.side,
            plan.quantity,
            plan.limit_price,
            plan.est_value,
            status,
            detail,
        ],
    )
    return order_id


# --------------------------------------------------------------------------- #
# Execution (broker side)
# --------------------------------------------------------------------------- #
def execute_approved(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Plan and submit every approved-unexecuted suggestion. Returns summary lines."""
    from ib_async import LimitOrder, Stock

    from moi.execute.queue import approved_unexecuted
    from moi.ingest.ibkr import ib_connection

    settings = get_settings()
    work = approved_unexecuted(con)
    if not work:
        return ["nothing approved to execute"]
    if kill_switch_on(con):
        raise SafetyError("kill switch is ON — all trading blocked")

    results: list[str] = []
    with ib_connection() as ib:
        accounts = ib.managedAccounts()
        account = settings.ibkr.account or (accounts[0] if accounts else "")
        if not account.startswith("DU") and not settings.allow_live:
            raise SafetyError(
                f"account {account!r} is not a paper account (DU...) and allow_live is false"
            )
        summary = {r.tag: r.value for r in ib.accountSummary()}
        net_liq = float(summary.get("NetLiquidation", 0) or 0)
        held = {p.contract.symbol: float(p.position) for p in ib.positions()}

        for suggestion_id, ticker, action, cw, tw in work:
            try:
                plan = plan_order(
                    con,
                    suggestion_id=suggestion_id,
                    ticker=ticker,
                    action=action,
                    current_weight=cw or 0.0,
                    target_weight=tw or 0.0,
                    net_liquidation=net_liq,
                    held_quantity=held.get(ticker, 0.0),
                )
            except SafetyError as exc:
                log.warning("order_refused", ticker=ticker, reason=str(exc))
                results.append(f"REFUSED {ticker}: {exc}")
                continue

            order_id = journal_order(con, plan, account, "submitted")
            try:
                contract = Stock(plan.ticker, "SMART", "USD")
                order = LimitOrder(plan.side, plan.quantity, plan.limit_price, tif="GTC")
                trade = ib.placeOrder(contract, order)
                ib.sleep(2)
                con.execute(
                    "UPDATE orders SET ib_order_id = ?, detail = ? WHERE order_id = ?",
                    [trade.order.orderId, trade.orderStatus.status, order_id],
                )
                con.execute(
                    "UPDATE suggestions SET status = 'EXECUTED' WHERE id = ?", [suggestion_id]
                )
                results.append(
                    f"SUBMITTED {plan.side} {plan.quantity} {plan.ticker} @ {plan.limit_price}"
                )
                log.info("order_submitted", ticker=plan.ticker, qty=plan.quantity)
            except Exception as exc:
                con.execute(
                    "UPDATE orders SET status = 'error', detail = ? WHERE order_id = ?",
                    [str(exc)[:300], order_id],
                )
                results.append(f"ERROR {plan.ticker}: {exc}")
    return results
