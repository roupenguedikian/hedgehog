"""Hedgehog Dashboard — FastAPI server."""
import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx
import uvicorn
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

# ── Load .env ────────────────────────────────────────────────
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# ── Add scripts/exchanges to path for opportunity scanner imports ──
EXCHANGES_DIR = PROJECT_ROOT / "scripts" / "exchanges"
sys.path.insert(0, str(EXCHANGES_DIR))

from .venues import fetch_all_portfolios, build_hedge_groups

# ── Cache ─────────────────────────────────────────────────────

_cache: dict[str, tuple[float, Any]] = {}
_start_time = time.time()


async def get_cached(key: str, fetcher: Callable, ttl: float = 25.0) -> Any:
    now = time.time()
    if key in _cache and (now - _cache[key][0]) < ttl:
        return _cache[key][1]
    data = await fetcher()
    _cache[key] = (now, data)
    return data


# ── App ───────────────────────────────────────────────────────

app = FastAPI(title="Hedgehog Dashboard")

# Serve static frontend
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/dashboard", StaticFiles(directory=str(STATIC_DIR), html=True), name="dashboard_static")


@app.get("/")
async def root():
    return RedirectResponse("/dashboard/index.html")


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "uptime_s": round(time.time() - _start_time, 1),
        "cache_keys": list(_cache.keys()),
    }


@app.get("/api/portfolio")
async def get_portfolio():
    data = await get_cached("portfolio", _fetch_portfolio, ttl=25)
    return data


@app.get("/api/opportunities")
async def get_opportunities():
    data = await get_cached("opportunities", _fetch_opportunities, ttl=50)
    return data


# ── Portfolio fetcher ─────────────────────────────────────────

async def _fetch_portfolio() -> dict:
    venues = await fetch_all_portfolios()

    total_equity = 0.0
    total_margin_used = 0.0
    total_margin_free = 0.0
    total_upnl = 0.0
    total_positions = 0

    for v in venues:
        if v.get("status") != "ok":
            continue
        bal = v.get("balance", {})
        total_equity += bal.get("equity", 0)
        total_margin_used += bal.get("margin_used", 0)
        total_margin_free += bal.get("margin_free", 0)
        total_upnl += bal.get("unrealized_pnl", 0)
        total_positions += len(v.get("positions", []))

    hedge_groups = build_hedge_groups(venues)

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_equity": total_equity,
        "total_margin_used": total_margin_used,
        "total_margin_free": total_margin_free,
        "total_margin_util_pct": (total_margin_used / total_equity * 100) if total_equity else 0,
        "total_upnl": total_upnl,
        "total_positions": total_positions,
        "venues": venues,
        "hedge_groups": hedge_groups,
    }


# ── Opportunities fetcher ────────────────────────────────────

async def _fetch_opportunities() -> dict:
    try:
        from opportunities_query import (
            fetch_hl as opp_fetch_hl,
            fetch_aster as opp_fetch_aster,
            fetch_lighter as opp_fetch_lighter,
            fetch_apex as opp_fetch_apex,
            fetch_dydx as opp_fetch_dydx,
            fetch_drift as opp_fetch_drift,
            fetch_edgex as opp_fetch_edgex,
            fetch_paradex as opp_fetch_paradex,
            fetch_ethereal as opp_fetch_ethereal,
            fetch_tradexyz as opp_fetch_tradexyz,
            build_hedge_pairs,
            load_ema,
            update_ema,
            save_ema,
            ema_values,
            MIN_VOLUME,
        )
    except ImportError as e:
        return {"error": f"Could not import opportunities_query: {e}", "pairs": [], "venue_status": {}}

    venues = [
        ("HL", opp_fetch_hl),
        ("Aster", opp_fetch_aster),
        ("Lighter", opp_fetch_lighter),
        ("Apex", opp_fetch_apex),
        ("dYdX", opp_fetch_dydx),
        ("Drift", opp_fetch_drift),
        ("EdgeX", opp_fetch_edgex),
        ("Paradex", opp_fetch_paradex),
        ("Ethereal", opp_fetch_ethereal),
        ("XYZ", opp_fetch_tradexyz),
    ]

    now = time.time()
    async with httpx.AsyncClient(timeout=25.0) as client:
        results = await asyncio.gather(
            *[fn(client) for _, fn in venues],
            return_exceptions=True,
        )

    venue_data: dict[str, dict] = {}
    venue_status: dict[str, str] = {}
    for (name, _), result in zip(venues, results):
        if isinstance(result, Exception):
            venue_data[name] = {}
            venue_status[name] = f"error: {result}"
        else:
            status = result.pop("__status__", None) if isinstance(result, dict) else None
            venue_data[name] = result
            if status == "blocked":
                venue_status[name] = "blocked"
            elif status == "degraded":
                venue_status[name] = "degraded"
            else:
                count = sum(1 for v in result.values() if v.get("volume", 0) >= MIN_VOLUME)
                venue_status[name] = f"ok ({count} symbols)"

    # Build EMA
    current_apy: dict[str, float] = {}
    for venue, symbols in venue_data.items():
        for symbol, info in symbols.items():
            if info.get("volume", 0) >= MIN_VOLUME:
                current_apy[f"{venue}:{symbol}"] = info["apy"]

    prev_ts, prev_state = load_ema()
    dt = now - prev_ts if prev_ts > 0 else 0
    state = update_ema(current_apy, prev_state, dt, now)
    save_ema(now, state)
    ema = ema_values(state)

    pairs = build_hedge_pairs(venue_data, MIN_VOLUME, ema)

    # Also build a funding rate matrix for the frontend
    funding_matrix: dict[str, dict[str, float]] = {}
    for venue, symbols in venue_data.items():
        for symbol, info in symbols.items():
            if symbol not in funding_matrix:
                funding_matrix[symbol] = {}
            funding_matrix[symbol][venue] = info["apy"]

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "pairs": pairs,
        "venue_status": venue_status,
        "funding_matrix": funding_matrix,
        "pair_count": len(pairs),
    }


# ── Entry point ──────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "dashboard.server:app",
        host="0.0.0.0",
        port=8080,
        reload=True,
        reload_dirs=[str(PROJECT_ROOT / "dashboard")],
    )
