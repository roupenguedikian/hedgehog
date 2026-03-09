-- HedgeHog — TimescaleDB Schema
-- Stores funding rates, opportunities, positions, and agent audit logs

-- Enable TimescaleDB
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ══════════════════════════════════════════════════════════════
-- Funding Rates (hypertable — time-series optimized)
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS funding_rates (
    timestamp       TIMESTAMPTZ     NOT NULL,
    venue           TEXT            NOT NULL,
    symbol          TEXT            NOT NULL,
    rate            DOUBLE PRECISION NOT NULL,
    annualized      DOUBLE PRECISION NOT NULL,
    cycle_hours     INT             NOT NULL DEFAULT 8,
    mark_price      DOUBLE PRECISION,
    index_price     DOUBLE PRECISION,
    open_interest   DOUBLE PRECISION,
    predicted_rate  DOUBLE PRECISION
);

SELECT create_hypertable('funding_rates', 'timestamp', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_fr_venue_symbol ON funding_rates (venue, symbol, timestamp DESC);

-- ══════════════════════════════════════════════════════════════
-- Funding Opportunities
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS funding_opportunities (
    timestamp       TIMESTAMPTZ     NOT NULL,
    symbol          TEXT            NOT NULL,
    short_venue     TEXT            NOT NULL,
    long_venue      TEXT            NOT NULL,
    spread_annual   DOUBLE PRECISION NOT NULL,
    net_yield       DOUBLE PRECISION NOT NULL,
    confidence      DOUBLE PRECISION NOT NULL
);

SELECT create_hypertable('funding_opportunities', 'timestamp', if_not_exists => TRUE);

-- ══════════════════════════════════════════════════════════════
-- Venue Scores
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS venue_scores (
    timestamp           TIMESTAMPTZ     NOT NULL,
    venue               TEXT            NOT NULL,
    symbol              TEXT            NOT NULL,
    composite_score     DOUBLE PRECISION NOT NULL,
    avg_funding_30d     DOUBLE PRECISION,
    liquidity_depth     DOUBLE PRECISION,
    fee_score           DOUBLE PRECISION,
    consistency_score   DOUBLE PRECISION
);

SELECT create_hypertable('venue_scores', 'timestamp', if_not_exists => TRUE);

-- ══════════════════════════════════════════════════════════════
-- Hedge Positions
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS hedge_positions (
    position_id     TEXT            PRIMARY KEY,
    symbol          TEXT            NOT NULL,
    short_venue     TEXT            NOT NULL,
    long_venue      TEXT            NOT NULL,
    short_size      DOUBLE PRECISION NOT NULL,
    long_size       DOUBLE PRECISION NOT NULL,
    short_entry     DOUBLE PRECISION NOT NULL,
    long_entry      DOUBLE PRECISION NOT NULL,
    entry_basis     DOUBLE PRECISION NOT NULL,
    funding_accrued DOUBLE PRECISION NOT NULL DEFAULT 0,
    fees_paid       DOUBLE PRECISION NOT NULL DEFAULT 0,
    gas_paid        DOUBLE PRECISION NOT NULL DEFAULT 0,
    net_pnl         DOUBLE PRECISION NOT NULL DEFAULT 0,
    status          TEXT            NOT NULL DEFAULT 'open',
    opened_at       TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    closed_at       TIMESTAMPTZ
);

-- ══════════════════════════════════════════════════════════════
-- Execution Log
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS execution_log (
    timestamp       TIMESTAMPTZ     NOT NULL,
    position_id     TEXT,
    venue           TEXT            NOT NULL,
    symbol          TEXT            NOT NULL,
    side            TEXT            NOT NULL,
    order_id        TEXT,
    status          TEXT            NOT NULL,
    filled_qty      DOUBLE PRECISION,
    avg_price       DOUBLE PRECISION,
    fees_paid       DOUBLE PRECISION,
    gas_cost        DOUBLE PRECISION,
    slippage_bps    DOUBLE PRECISION,
    tx_hash         TEXT
);

SELECT create_hypertable('execution_log', 'timestamp', if_not_exists => TRUE);

-- ══════════════════════════════════════════════════════════════
-- Agent Reasoning Log (audit trail)
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS agent_log (
    timestamp       TIMESTAMPTZ     NOT NULL,
    cycle_number    INT             NOT NULL,
    agent           TEXT            NOT NULL,
    reasoning       TEXT,
    confidence      DOUBLE PRECISION,
    actions         JSONB,
    decisions       JSONB
);

SELECT create_hypertable('agent_log', 'timestamp', if_not_exists => TRUE);

-- ══════════════════════════════════════════════════════════════
-- Risk Snapshots
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS risk_snapshots (
    timestamp           TIMESTAMPTZ     NOT NULL,
    nav                 DOUBLE PRECISION NOT NULL,
    drawdown_pct        DOUBLE PRECISION NOT NULL,
    max_venue_exposure  DOUBLE PRECISION,
    max_chain_exposure  DOUBLE PRECISION,
    warnings            TEXT[],
    halt_triggered      BOOLEAN DEFAULT FALSE
);

SELECT create_hypertable('risk_snapshots', 'timestamp', if_not_exists => TRUE);

-- ══════════════════════════════════════════════════════════════
-- Continuous Aggregates for Dashboard
-- ══════════════════════════════════════════════════════════════
-- Hourly funding rate averages per venue/symbol
CREATE MATERIALIZED VIEW IF NOT EXISTS funding_rates_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', timestamp) AS hour,
    venue,
    symbol,
    AVG(rate) as avg_rate,
    AVG(annualized) as avg_annualized,
    AVG(open_interest) as avg_oi
FROM funding_rates
GROUP BY hour, venue, symbol
WITH NO DATA;

-- Refresh every 5 minutes
SELECT add_continuous_aggregate_policy('funding_rates_hourly',
    start_offset => INTERVAL '2 hours',
    end_offset => INTERVAL '5 minutes',
    schedule_interval => INTERVAL '5 minutes',
    if_not_exists => TRUE
);
