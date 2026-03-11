-- HedgeHog — Portfolio TimescaleDB Schema
-- Extends init_db.sql with venue account snapshots, position tracking,
-- and funding income history.
-- Apply after init_db.sql:
--   psql $DATABASE_URL -f scripts/init_portfolio_db.sql

-- ══════════════════════════════════════════════════════════════
-- Venue Accounts: periodic snapshots of account state per venue
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS venue_accounts (
    timestamp       TIMESTAMPTZ      NOT NULL,
    venue           TEXT             NOT NULL,
    nav             DOUBLE PRECISION,
    wallet_balance  DOUBLE PRECISION,
    margin_used     DOUBLE PRECISION,
    free_margin     DOUBLE PRECISION,
    maint_margin    DOUBLE PRECISION,
    margin_util_pct DOUBLE PRECISION,
    unrealized_pnl  DOUBLE PRECISION,
    withdrawable    DOUBLE PRECISION,
    position_count  INT DEFAULT 0
);
SELECT create_hypertable('venue_accounts', 'timestamp', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_va_venue ON venue_accounts (venue, timestamp DESC);

-- ══════════════════════════════════════════════════════════════
-- Venue Positions: snapshot of each open position per venue
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS venue_positions (
    timestamp       TIMESTAMPTZ      NOT NULL,
    venue           TEXT             NOT NULL,
    symbol          TEXT             NOT NULL,
    side            TEXT             NOT NULL,
    size            DOUBLE PRECISION,
    notional        DOUBLE PRECISION,
    entry_price     DOUBLE PRECISION,
    mark_price      DOUBLE PRECISION,
    unrealized_pnl  DOUBLE PRECISION,
    leverage        DOUBLE PRECISION,
    liquidation_price DOUBLE PRECISION
);
SELECT create_hypertable('venue_positions', 'timestamp', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_vp_venue_sym ON venue_positions (venue, symbol, timestamp DESC);

-- ══════════════════════════════════════════════════════════════
-- Venue Funding Income: funding payments received per venue per symbol
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS venue_funding_income (
    timestamp       TIMESTAMPTZ      NOT NULL,
    venue           TEXT             NOT NULL,
    symbol          TEXT             NOT NULL,
    rate            DOUBLE PRECISION,
    payment         DOUBLE PRECISION
);
SELECT create_hypertable('venue_funding_income', 'timestamp', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_vfi_venue_sym ON venue_funding_income (venue, symbol, timestamp DESC);

-- ══════════════════════════════════════════════════════════════
-- Continuous Aggregates
-- ══════════════════════════════════════════════════════════════

-- Hourly account snapshots
CREATE MATERIALIZED VIEW IF NOT EXISTS venue_accounts_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', timestamp) AS hour,
    venue,
    AVG(nav) as avg_nav,
    AVG(margin_util_pct) as avg_margin_util,
    AVG(unrealized_pnl) as avg_upnl,
    MAX(position_count) as max_positions
FROM venue_accounts
GROUP BY hour, venue
WITH NO DATA;

SELECT add_continuous_aggregate_policy('venue_accounts_hourly',
    start_offset => INTERVAL '2 hours',
    end_offset => INTERVAL '5 minutes',
    schedule_interval => INTERVAL '5 minutes',
    if_not_exists => TRUE
);

-- Daily funding income rollup
CREATE MATERIALIZED VIEW IF NOT EXISTS venue_funding_income_daily
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', timestamp) AS day,
    venue,
    symbol,
    SUM(payment) as total_payment,
    AVG(rate) as avg_rate,
    COUNT(*) as payment_count
FROM venue_funding_income
GROUP BY day, venue, symbol
WITH NO DATA;

SELECT add_continuous_aggregate_policy('venue_funding_income_daily',
    start_offset => INTERVAL '2 days',
    end_offset => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE
);
