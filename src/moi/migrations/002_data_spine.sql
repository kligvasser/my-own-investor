-- Phase 1 schema: whale 13F holdings, insider Form 4, congress trades,
-- Polymarket series, news items, and macro series.

-- Quarterly 13F holdings per tracked manager. `change_status` is computed against the
-- previous stored quarter at collect time (NEW | INCREASED | DECREASED | UNCHANGED).
CREATE TABLE IF NOT EXISTS filings_13f (
    manager_cik   VARCHAR NOT NULL,
    manager_name  VARCHAR,
    period        DATE    NOT NULL,   -- report period end (quarter)
    cusip         VARCHAR NOT NULL,
    ticker        VARCHAR,
    issuer        VARCHAR,
    value_usd     DOUBLE,
    shares        DOUBLE,
    change_status VARCHAR,
    filed_at      DATE,
    PRIMARY KEY (manager_cik, period, cusip)
);

-- Insider (officer/director/10% owner) transactions for universe tickers.
CREATE TABLE IF NOT EXISTS insider_form4 (
    accession  VARCHAR NOT NULL,
    seq        INTEGER NOT NULL,      -- row index within the filing
    ticker     VARCHAR NOT NULL,
    insider    VARCHAR,
    role       VARCHAR,
    tx_date    DATE,
    code       VARCHAR,               -- SEC transaction code: P=purchase, S=sale, ...
    shares     DOUBLE,
    price      DOUBLE,
    value_usd  DOUBLE,
    filed_at   TIMESTAMP,
    PRIMARY KEY (accession, seq)
);

-- STOCK Act congressional trade disclosures. `tx_id` is a stable hash of the natural
-- fields so re-collection from either provider stays idempotent.
CREATE TABLE IF NOT EXISTS congress_trades (
    tx_id           VARCHAR NOT NULL PRIMARY KEY,
    politician      VARCHAR NOT NULL,
    chamber         VARCHAR,          -- house | senate
    ticker          VARCHAR,
    direction       VARCHAR,          -- buy | sell
    amount_range    VARCHAR,          -- disclosed band, e.g. "$1,001 - $15,000"
    tx_date         DATE,
    disclosure_date DATE,             -- keep both: disclosure lag is itself a feature
    source          VARCHAR NOT NULL  -- quiver | unusualwhales
);

-- Tracked Polymarket markets (metadata) and their daily probability history.
CREATE TABLE IF NOT EXISTS polymarket_markets (
    slug       VARCHAR NOT NULL PRIMARY KEY,
    question   VARCHAR,
    category   VARCHAR,               -- from config: fed | tariffs | ai | election | ...
    token_id   VARCHAR,               -- CLOB token id of the YES outcome
    closed     BOOLEAN,
    updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS polymarket_series (
    slug VARCHAR NOT NULL,
    ts   DATE    NOT NULL,
    prob DOUBLE,
    PRIMARY KEY (slug, ts)
);

-- Deduplicated news headlines per ticker/feed. `id` = sha1(url).
CREATE TABLE IF NOT EXISTS news_items (
    id           VARCHAR NOT NULL PRIMARY KEY,
    ticker       VARCHAR,             -- NULL for sector-wide feeds
    title        VARCHAR,
    url          VARCHAR,
    published_at TIMESTAMP,
    feed         VARCHAR,
    summary      VARCHAR
);

-- FRED macro series.
CREATE TABLE IF NOT EXISTS macro_series (
    series_id VARCHAR NOT NULL,
    date      DATE    NOT NULL,
    value     DOUBLE,
    PRIMARY KEY (series_id, date)
);
