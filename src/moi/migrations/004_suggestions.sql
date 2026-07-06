-- Phase 3 schema: actionable suggestions (the approval queue) and account snapshots.

-- Every proposed action. Nothing is ever executed unless status = 'APPROVED'
-- (and execution itself arrives only in Phase 4).
CREATE TABLE IF NOT EXISTS suggestions (
    id            VARCHAR NOT NULL PRIMARY KEY,
    created_at    TIMESTAMP NOT NULL,
    week_end      DATE NOT NULL,
    ticker        VARCHAR NOT NULL,
    action        VARCHAR NOT NULL,   -- BUY | ADD | TRIM | SELL | HOLD
    current_weight DOUBLE,
    target_weight  DOUBLE,
    score         DOUBLE,
    thesis        VARCHAR,            -- written by the analyst agent
    bear_case     VARCHAR,            -- written by the bear (red team) agent
    confidence    VARCHAR,            -- strong | moderate | weak
    status        VARCHAR NOT NULL DEFAULT 'PENDING',
                                      -- PENDING | APPROVED | REJECTED | SNOOZED | EXECUTED
    decided_at    TIMESTAMP
);

-- Point-in-time snapshots of the real IBKR account (taken when TWS is reachable).
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    taken_at     TIMESTAMP NOT NULL,
    account      VARCHAR NOT NULL,
    ticker       VARCHAR NOT NULL,
    quantity     DOUBLE,
    avg_cost     DOUBLE,
    market_value DOUBLE,              -- estimated from latest stored close when possible
    net_liquidation DOUBLE,           -- account-level, repeated per row for convenience
    PRIMARY KEY (taken_at, account, ticker)
);
