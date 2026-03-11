"""
Hedgehog Web UI — FastAPI backend.

Reads from TimescaleDB and provides REST API + dashboard.
Controls Docker containers for bot lifecycle management.

Usage:
    uvicorn web.app:app --host 0.0.0.0 --port 8000 --reload
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

# Add scripts/ to path so we can import hedge_scanner
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from hedge_scanner import scan_json  # noqa: E402


# ── Database URL ─────────────────────────────────────────────────────────────

def get_database_url() -> str:
    url = os.getenv("DATABASE_URL", "")
    if url:
        # Inside Docker, DATABASE_URL uses 'timescaledb' hostname.
        # For local dev, fall back to localhost if DB_HOST isn't set.
        return url
    host = os.getenv("DB_HOST", "localhost")
    port = os.getenv("DB_PORT", "5432")
    user = os.getenv("DB_USER", "aegis")
    password = os.getenv("DB_PASSWORD", "aegis_dev")
    dbname = os.getenv("DB_NAME", "aegis")
    return f"postgresql://{user}:{password}@{host}:{port}/{dbname}"


def get_database_urls() -> list[str]:
    """Return candidate DB URLs — primary first, localhost fallback second."""
    primary = get_database_url()
    urls = [primary]
    # If primary uses a Docker-internal hostname, add localhost fallback
    if "timescaledb" in primary or "aegis-db" in primary:
        fallback = primary.replace("timescaledb", "localhost").replace("aegis-db", "localhost")
        if fallback != primary:
            urls.append(fallback)
    return urls


# ── Docker helpers ───────────────────────────────────────────────────────────

ALLOWED_CONTAINERS = {"hedgehog-bot", "hedgehog-redis", "aegis-db", "hedgehog-web"}
ALLOWED_ACTIONS = {"start", "stop", "restart", "pause", "unpause"}
COMPOSE_DIR = Path(__file__).resolve().parent.parent


async def docker_cmd(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "docker", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


async def docker_compose_cmd(*args: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "docker", "compose", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(COMPOSE_DIR),
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode().strip(), stderr.decode().strip()


# ── App lifecycle ────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = None
    for url in get_database_urls():
        try:
            app.state.pool = await asyncpg.create_pool(
                url, min_size=2, max_size=10, command_timeout=10,
            )
            break
        except Exception:
            continue
    yield
    if app.state.pool:
        await app.state.pool.close()


app = FastAPI(title="Hedgehog Dashboard", lifespan=lifespan)


# ── Helper ───────────────────────────────────────────────────────────────────

def pool(request: Request):
    p = request.app.state.pool
    if not p:
        raise HTTPException(503, "Database not connected")
    return p


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


# ── Portfolio ────────────────────────────────────────────────────────────────

@app.get("/api/portfolio/summary")
async def portfolio_summary(request: Request):
    p = pool(request)
    async with p.acquire() as conn:
        # Latest account per venue
        accounts = await conn.fetch("""
            SELECT DISTINCT ON (venue) venue, nav, wallet_balance, margin_used,
                   free_margin, unrealized_pnl, margin_util_pct, position_count
            FROM venue_accounts ORDER BY venue, timestamp DESC
        """)
        total_nav = sum(r["nav"] or 0 for r in accounts)
        total_upnl = sum(r["unrealized_pnl"] or 0 for r in accounts)
        total_positions = sum(r["position_count"] or 0 for r in accounts)

        # Total funding collected
        funding_row = await conn.fetchrow(
            "SELECT COALESCE(SUM(payment), 0) as total FROM venue_funding_income"
        )
        total_funding = float(funding_row["total"])

        # Today's funding
        today_row = await conn.fetchrow("""
            SELECT COALESCE(SUM(payment), 0) as today
            FROM venue_funding_income
            WHERE timestamp >= CURRENT_DATE
        """)
        today_funding = float(today_row["today"])

        # Latest risk
        risk = await conn.fetchrow(
            "SELECT * FROM risk_snapshots ORDER BY timestamp DESC LIMIT 1"
        )

        # Open hedges
        hedges = await conn.fetchval(
            "SELECT COUNT(*) FROM hedge_positions WHERE status = 'open'"
        )

    return {
        "total_nav": total_nav,
        "total_unrealized_pnl": total_upnl,
        "total_funding_collected": total_funding,
        "today_funding": today_funding,
        "active_positions": total_positions,
        "open_hedges": hedges or 0,
        "drawdown_pct": float(risk["drawdown_pct"]) if risk else 0,
        "halt_triggered": risk["halt_triggered"] if risk else False,
    }


@app.get("/api/portfolio/nav-history")
async def nav_history(request: Request, hours: int = 72):
    p = pool(request)
    async with p.acquire() as conn:
        rows = await conn.fetch("""
            SELECT time_bucket('1 hour', timestamp) AS ts,
                   SUM(nav) as total_nav
            FROM venue_accounts
            WHERE timestamp > NOW() - make_interval(hours => $1)
            GROUP BY ts ORDER BY ts
        """, hours)
    return [{"ts": str(r["ts"]), "nav": float(r["total_nav"] or 0)} for r in rows]


# ── Venues ───────────────────────────────────────────────────────────────────

@app.get("/api/venues/accounts")
async def venue_accounts(request: Request):
    p = pool(request)
    async with p.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT ON (venue) venue, nav, wallet_balance, margin_used,
                   free_margin, maint_margin, margin_util_pct, unrealized_pnl,
                   withdrawable, position_count, timestamp
            FROM venue_accounts ORDER BY venue, timestamp DESC
        """)
    return rows_to_dicts(rows)


@app.get("/api/venues/scores")
async def venue_scores(request: Request):
    p = pool(request)
    async with p.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT ON (venue, symbol) venue, symbol, composite_score,
                   avg_funding_30d, liquidity_depth, fee_score, consistency_score, timestamp
            FROM venue_scores ORDER BY venue, symbol, timestamp DESC
        """)
    return rows_to_dicts(rows)


# ── Positions ────────────────────────────────────────────────────────────────

@app.get("/api/positions")
async def positions(request: Request):
    p = pool(request)
    async with p.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT ON (venue, symbol, side)
                   venue, symbol, side, size, notional, entry_price, mark_price,
                   unrealized_pnl, leverage, liquidation_price, timestamp
            FROM venue_positions ORDER BY venue, symbol, side, timestamp DESC
        """)
    return rows_to_dicts(rows)


@app.get("/api/positions/hedges")
async def hedge_positions(request: Request):
    p = pool(request)
    async with p.acquire() as conn:
        rows = await conn.fetch("""
            SELECT * FROM hedge_positions
            WHERE status = 'open'
            ORDER BY opened_at DESC
        """)
    return rows_to_dicts(rows)


# ── Funding ──────────────────────────────────────────────────────────────────

@app.get("/api/funding/matrix")
async def funding_matrix(request: Request):
    p = pool(request)
    async with p.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT ON (venue, symbol) venue, symbol, rate, annualized,
                   mark_price, open_interest, predicted_rate, cycle_hours
            FROM funding_rates
            WHERE timestamp > NOW() - INTERVAL '2 hours'
            ORDER BY venue, symbol, timestamp DESC
        """)
    return rows_to_dicts(rows)


@app.get("/api/funding/history")
async def funding_history(request: Request, venue: str, symbol: str, hours: int = 24):
    p = pool(request)
    async with p.acquire() as conn:
        rows = await conn.fetch("""
            SELECT timestamp, rate, annualized, mark_price
            FROM funding_rates
            WHERE venue = $1 AND symbol = $2
              AND timestamp > NOW() - make_interval(hours => $3)
            ORDER BY timestamp
        """, venue, symbol, hours)
    return rows_to_dicts(rows)


@app.get("/api/funding/income")
async def funding_income(request: Request, days: int = 30):
    p = pool(request)
    async with p.acquire() as conn:
        rows = await conn.fetch("""
            SELECT venue, symbol,
                   SUM(payment) as total_payment,
                   AVG(rate) as avg_rate,
                   COUNT(*) as payment_count
            FROM venue_funding_income
            WHERE timestamp > NOW() - make_interval(days => $1)
            GROUP BY venue, symbol
            ORDER BY total_payment DESC
        """, days)
    return rows_to_dicts(rows)


# ── Opportunities ────────────────────────────────────────────────────────────

@app.get("/api/opportunities")
async def opportunities(request: Request, limit: int = 15):
    p = pool(request)
    async with p.acquire() as conn:
        rows = await conn.fetch("""
            SELECT DISTINCT ON (symbol, short_venue, long_venue)
                   symbol, short_venue, long_venue, spread_annual,
                   net_yield, confidence, timestamp
            FROM funding_opportunities
            WHERE timestamp > NOW() - INTERVAL '2 hours'
            ORDER BY symbol, short_venue, long_venue, timestamp DESC
        """)
    # Sort by spread descending and limit
    result = rows_to_dicts(rows)
    result.sort(key=lambda x: x.get("spread_annual", 0), reverse=True)
    return result[:limit]


# ── Risk ─────────────────────────────────────────────────────────────────────

@app.get("/api/risk/snapshot")
async def risk_snapshot(request: Request):
    p = pool(request)
    async with p.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM risk_snapshots ORDER BY timestamp DESC LIMIT 1"
        )
    if not row:
        return {}
    return dict(row)


@app.get("/api/risk/history")
async def risk_history(request: Request, hours: int = 24):
    p = pool(request)
    async with p.acquire() as conn:
        rows = await conn.fetch("""
            SELECT timestamp, nav, drawdown_pct, max_venue_exposure,
                   max_chain_exposure, halt_triggered
            FROM risk_snapshots
            WHERE timestamp > NOW() - make_interval(hours => $1)
            ORDER BY timestamp
        """, hours)
    return rows_to_dicts(rows)


# ── Logs ─────────────────────────────────────────────────────────────────────

@app.get("/api/agent/log")
async def agent_log(request: Request, limit: int = 20):
    p = pool(request)
    async with p.acquire() as conn:
        rows = await conn.fetch("""
            SELECT timestamp, cycle_number, agent, reasoning, confidence,
                   actions, decisions
            FROM agent_log ORDER BY timestamp DESC LIMIT $1
        """, limit)
    result = []
    for r in rows:
        d = dict(r)
        # Convert jsonb to dicts
        for k in ("actions", "decisions"):
            if d[k] and isinstance(d[k], str):
                d[k] = json.loads(d[k])
        result.append(d)
    return result


@app.get("/api/executions")
async def executions(request: Request, limit: int = 20):
    p = pool(request)
    async with p.acquire() as conn:
        rows = await conn.fetch("""
            SELECT timestamp, position_id, venue, symbol, side, order_id,
                   status, filled_qty, avg_price, fees_paid, gas_cost,
                   slippage_bps, tx_hash
            FROM execution_log ORDER BY timestamp DESC LIMIT $1
        """, limit)
    return rows_to_dicts(rows)


# ── Docker Control ───────────────────────────────────────────────────────────

@app.get("/api/docker/status")
async def docker_status():
    rc, out, err = await docker_cmd(
        "ps", "-a",
        "--filter", "name=hedgehog",
        "--filter", "name=aegis",
        "--format", '{"name":"{{.Names}}","status":"{{.Status}}","state":"{{.State}}","ports":"{{.Ports}}","image":"{{.Image}}"}',
    )
    if rc != 0:
        return {"error": err, "containers": []}
    containers = []
    for line in out.splitlines():
        if line.strip():
            try:
                containers.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return {"containers": containers}


@app.post("/api/docker/container/{name}/{action}")
async def container_action(name: str, action: str):
    if name not in ALLOWED_CONTAINERS:
        raise HTTPException(403, f"Container '{name}' not allowed")
    if action not in ALLOWED_ACTIONS:
        raise HTTPException(400, f"Action '{action}' not allowed")
    rc, out, err = await docker_cmd(action, name)
    if rc != 0:
        raise HTTPException(500, err)
    return {"ok": True, "container": name, "action": action}


@app.post("/api/docker/stack/{action}")
async def stack_action(action: str):
    if action == "up":
        rc, out, err = await docker_compose_cmd("up", "-d")
    elif action == "down":
        rc, out, err = await docker_compose_cmd("down")
    else:
        raise HTTPException(400, f"Invalid stack action: {action}")
    if rc != 0:
        raise HTTPException(500, err)
    return {"ok": True, "action": action, "output": out}


# ── Live Scanner (fetches from venue APIs, cached 30s) ───────────────

_scanner_cache: dict = {"data": None, "ts": 0}
_scanner_lock = asyncio.Lock()
SCANNER_CACHE_TTL = 30  # seconds


@app.get("/api/scanner/opportunities")
async def scanner_opportunities():
    now = time.time()
    if _scanner_cache["data"] and now - _scanner_cache["ts"] < SCANNER_CACHE_TTL:
        return _scanner_cache["data"]
    async with _scanner_lock:
        # Double-check after acquiring lock
        if _scanner_cache["data"] and time.time() - _scanner_cache["ts"] < SCANNER_CACHE_TTL:
            return _scanner_cache["data"]
        try:
            data = await scan_json()
            _scanner_cache["data"] = data
            _scanner_cache["ts"] = time.time()
            return data
        except Exception as e:
            raise HTTPException(500, f"Scanner error: {e}")


# ── Health ───────────────────────────────────────────────────────────────────

@app.get("/api/health")
async def health(request: Request):
    db_ok = False
    if request.app.state.pool:
        try:
            async with request.app.state.pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            db_ok = True
        except Exception:
            pass
    return {"status": "ok", "db": db_ok}


# ── Serve frontend ──────────────────────────────────────────────────────────

STATIC_DIR = Path(__file__).parent / "static"

@app.get("/", response_class=HTMLResponse)
async def index():
    index_file = STATIC_DIR / "index.html"
    return HTMLResponse(index_file.read_text())
