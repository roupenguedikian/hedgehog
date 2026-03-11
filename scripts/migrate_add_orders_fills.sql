-- HedgeHog — Migration: add venue_orders and venue_fills tables
-- Completes the 6 connector data tables:
--   1. venue_accounts     (account)
--   2. venue_positions    (positions)
--   3. venue_orders       (orders)      ← NEW
--   4. venue_fills        (fills)       ← NEW
--   5. funding_rates      (funding)
--   6. venue_funding_income (income)
--
-- Apply:  psql $DATABASE_URL -f scripts/migrate_add_orders_fills.sql

-- ══════════════════════════════════════════════════════════════
-- Venue Orders: snapshot of open orders per venue
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS venue_orders (
    timestamp       TIMESTAMPTZ      NOT NULL,
    venue           TEXT             NOT NULL,
    symbol          TEXT             NOT NULL,
    side            TEXT             NOT NULL,
    order_type      TEXT,
    price           DOUBLE PRECISION,
    size            DOUBLE PRECISION,
    filled          DOUBLE PRECISION DEFAULT 0,
    tif             TEXT,
    status          TEXT,
    order_id        TEXT
);
SELECT create_hypertable('venue_orders', 'timestamp', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_vo_venue_sym ON venue_orders (venue, symbol, timestamp DESC);

-- ══════════════════════════════════════════════════════════════
-- Venue Fills: trade execution history per venue
-- ══════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS venue_fills (
    timestamp       TIMESTAMPTZ      NOT NULL,
    venue           TEXT             NOT NULL,
    symbol          TEXT             NOT NULL,
    side            TEXT             NOT NULL,
    price           DOUBLE PRECISION,
    size            DOUBLE PRECISION,
    value           DOUBLE PRECISION,
    fee             DOUBLE PRECISION,
    role            TEXT
);
SELECT create_hypertable('venue_fills', 'timestamp', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_vf_venue_sym ON venue_fills (venue, symbol, timestamp DESC);
