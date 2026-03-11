"""
connectors/db.py
Shared DB-write layer for venue connectors.
Every connector imports this and calls the insert helpers after fetching data.
If the DB is unreachable, writes are silently skipped (connectors still print).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

_pool = None


async def _get_pool():
    global _pool
    if _pool is not None:
        return _pool
    try:
        import asyncpg
    except ImportError:
        return None
    url = _build_url()
    if not url:
        return None
    try:
        _pool = await asyncpg.create_pool(url, min_size=1, max_size=3, command_timeout=10)
        return _pool
    except Exception:
        return None


def _build_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if url:
        for h in ("timescaledb", "aegis-db", "postgres"):
            url = url.replace(f"@{h}:", "@localhost:")
        return url
    host = os.environ.get("DB_HOST", "localhost")
    port = os.environ.get("DB_PORT", "5432")
    user = os.environ.get("DB_USER", "aegis")
    pw = os.environ.get("DB_PASSWORD", "aegis_dev")
    name = os.environ.get("DB_NAME", "aegis")
    return f"postgresql://{user}:{pw}@{host}:{port}/{name}"


async def insert_account(venue: str, *, nav: float, wallet_balance: float,
                         margin_used: float, free_margin: float, maint_margin: float,
                         margin_util_pct: float, unrealized_pnl: float,
                         withdrawable: float, position_count: int):
    pool = await _get_pool()
    if not pool:
        return
    now = datetime.now(timezone.utc)
    try:
        await pool.execute(
            """INSERT INTO venue_accounts
               (timestamp, venue, nav, wallet_balance, margin_used, free_margin,
                maint_margin, margin_util_pct, unrealized_pnl, withdrawable, position_count)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
            now, venue, nav, wallet_balance, margin_used, free_margin,
            maint_margin, margin_util_pct, unrealized_pnl, withdrawable, position_count,
        )
    except Exception:
        pass


async def insert_positions(venue: str, rows: list[dict]):
    pool = await _get_pool()
    if not pool or not rows:
        return
    now = datetime.now(timezone.utc)
    try:
        await pool.executemany(
            """INSERT INTO venue_positions
               (timestamp, venue, symbol, side, size, notional, entry_price,
                mark_price, unrealized_pnl, leverage, liquidation_price)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
            [(now, venue, r["symbol"], r["side"], r["size"], r["notional"],
              r["entry_price"], r["mark_price"], r["unrealized_pnl"],
              r["leverage"], r["liquidation_price"]) for r in rows],
        )
    except Exception:
        pass


async def insert_funding_rates(venue: str, rows: list[dict]):
    pool = await _get_pool()
    if not pool or not rows:
        return
    now = datetime.now(timezone.utc)
    try:
        await pool.executemany(
            """INSERT INTO funding_rates
               (timestamp, venue, symbol, rate, annualized, cycle_hours,
                mark_price, index_price, open_interest, predicted_rate)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)""",
            [(now, venue, r["symbol"], r["rate"], r["annualized"],
              r["cycle_hours"], r.get("mark_price"), r.get("index_price"),
              r.get("open_interest"), r.get("predicted_rate")) for r in rows],
        )
    except Exception:
        pass


async def insert_orders(venue: str, rows: list[dict]):
    pool = await _get_pool()
    if not pool or not rows:
        return
    now = datetime.now(timezone.utc)
    try:
        await pool.executemany(
            """INSERT INTO venue_orders
               (timestamp, venue, symbol, side, order_type, price, size,
                filled, tif, status, order_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
            [(now, venue, r["symbol"], r["side"], r.get("order_type"),
              r.get("price"), r.get("size"), r.get("filled", 0),
              r.get("tif"), r.get("status"), r.get("order_id"))
             for r in rows],
        )
    except Exception:
        pass


async def insert_fills(venue: str, rows: list[dict]):
    pool = await _get_pool()
    if not pool or not rows:
        return
    try:
        await pool.executemany(
            """INSERT INTO venue_fills
               (timestamp, venue, symbol, side, price, size, value, fee, role)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
            [(r["timestamp"], venue, r["symbol"], r["side"],
              r.get("price"), r.get("size"), r.get("value"),
              r.get("fee"), r.get("role"))
             for r in rows],
        )
    except Exception:
        pass


async def insert_income(venue: str, rows: list[dict]):
    pool = await _get_pool()
    if not pool or not rows:
        return
    try:
        await pool.executemany(
            """INSERT INTO venue_funding_income
               (timestamp, venue, symbol, rate, payment)
               VALUES ($1,$2,$3,$4,$5)""",
            [(r["timestamp"], venue, r["symbol"], r["rate"], r["payment"])
             for r in rows],
        )
    except Exception:
        pass


async def close():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
