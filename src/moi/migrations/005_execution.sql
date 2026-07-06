-- Phase 4 schema: order journal and runtime controls (kill switch).

CREATE TABLE IF NOT EXISTS orders (
    order_id      VARCHAR NOT NULL PRIMARY KEY,
    suggestion_id VARCHAR NOT NULL,   -- every order traces back to an approved suggestion
    created_at    TIMESTAMP NOT NULL,
    account       VARCHAR,
    ticker        VARCHAR NOT NULL,
    side          VARCHAR NOT NULL,   -- BUY | SELL
    quantity      DOUBLE  NOT NULL,
    limit_price   DOUBLE,
    est_value     DOUBLE,
    status        VARCHAR NOT NULL,   -- planned | submitted | filled | cancelled | error
    ib_order_id   INTEGER,
    filled_at     TIMESTAMP,
    fill_price    DOUBLE,
    detail        VARCHAR
);

-- Runtime flags. kill_switch = 'on' blocks ALL order placement, checked before every order.
CREATE TABLE IF NOT EXISTS controls (
    key        VARCHAR NOT NULL PRIMARY KEY,
    value      VARCHAR NOT NULL,
    updated_at TIMESTAMP NOT NULL
);
