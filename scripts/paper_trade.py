#!/usr/bin/env python3
"""
Hedgehog Paper Trader — simulates funding rate arbitrage using live scanner data.

Usage:
    python3 scripts/paper_trade.py                        # single cycle
    python3 scripts/paper_trade.py --loop                 # continuous (60s default)
    python3 scripts/paper_trade.py --loop --interval 120  # custom interval
    python3 scripts/paper_trade.py --report               # performance report
    python3 scripts/paper_trade.py --reset                # reset simulation state

Environment overrides:
    PAPER_INITIAL_NAV=100000   ENTRY_NDPY=0.0008   EXIT_NDPY=0.0003
    PAPER_BASE_SIZE=200        ENTRY_BREAKEVEN=12   TAG_THRESHOLD=3
"""
import asyncio
import json
import math
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

# ── Add project root to path ─────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "connectors"))

from opportunities_query import (
    VENUE_FEES,
    VENUE_TIER,
    build_hedge_pairs,
    fetch_apex,
    fetch_aster,
    fetch_drift,
    fetch_dydx,
    fetch_edgex,
    fetch_ethereal,
    fetch_hl,
    fetch_lighter,
    fetch_paradex,
    fetch_tradexyz,
    load_ema,
    save_ema,
    update_ema,
)

# ── Load .env ─────────────────────────────────────────────────────
def _load_env(path):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))

_load_env(PROJECT_ROOT / ".env")

# ── ANSI colors ───────────────────────────────────────────────────
G = "\033[92m"
R = "\033[91m"
Y = "\033[93m"
D = "\033[2m"
B = "\033[1m"
X = "\033[0m"

# ── Files ─────────────────────────────────────────────────────────
STATE_FILE = PROJECT_ROOT / "data" / "paper_state.json"
LEDGER_FILE = PROJECT_ROOT / "data" / "paper_ledger.jsonl"

# ── Config ────────────────────────────────────────────────────────
INITIAL_NAV = float(os.environ.get("PAPER_INITIAL_NAV", "100000"))
BASE_SIZE_USD = float(os.environ.get("PAPER_BASE_SIZE", "200"))
MAX_POSITIONS = 12
MAX_POSITION_PCT = 0.20
ENDPY = float(os.environ.get("ENTRY_NDPY", "0.0008"))      # 0.08%/day ≈ 29% APY
XNDPY = float(os.environ.get("EXIT_NDPY", "0.0003"))       # 0.03%/day ≈ 11% APY
MEBH = float(os.environ.get("ENTRY_BREAKEVEN", "12"))       # max breakeven hours
MRBH = 6.0                                                   # max rotation breakeven hours
TAG_THRESHOLD = int(os.environ.get("TAG_THRESHOLD", "3"))    # consecutive qualifying cycles
MIN_SPREAD_APY = 10.0                                        # 10% annual
EXIT_SPREAD_APY = 3.0                                        # 3% annual
MAX_HOLD_DAYS = 30
MIN_VOLUME = 100_000

# Venue funding cycle hours (for rate → daily conversion)
VENUE_CYCLE_HOURS = {
    "HL": 1, "Lighter": 8, "Drift": 1, "dYdX": 1,
    "Aster": 1, "Apex": 8, "Paradex": 8, "Ethereal": 1,
    "EdgeX": 4, "XYZ": 1,
}

VENUES = [
    ("HL", fetch_hl), ("Aster", fetch_aster), ("Lighter", fetch_lighter),
    ("Apex", fetch_apex), ("dYdX", fetch_dydx), ("Drift", fetch_drift),
    ("EdgeX", fetch_edgex), ("Paradex", fetch_paradex), ("Ethereal", fetch_ethereal),
    ("XYZ", fetch_tradexyz),
]


# ═════════════════════════════════════════════════════════════════
# STATE MANAGEMENT
# ═════════════════════════════════════════════════════════════════

def _new_state() -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "started_at": now,
        "last_cycle_at": None,
        "cycle_count": 0,
        "portfolio": {
            "nav": INITIAL_NAV,
            "cash": INITIAL_NAV,
            "peak_nav": INITIAL_NAV,
            "total_funding_earned": 0.0,
            "total_fees_paid": 0.0,
            "total_realized_pnl": 0.0,
        },
        "positions": {},
        "entry_counters": {},
        "closed_summary": {
            "count": 0,
            "total_funding": 0.0,
            "total_fees": 0.0,
            "total_pnl": 0.0,
            "total_hold_hours": 0.0,
            "wins": 0,
        },
    }


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return _new_state()


def save_state(state: dict):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.rename(STATE_FILE)


def log_event(event: dict):
    LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with open(LEDGER_FILE, "a") as f:
        f.write(json.dumps(event) + "\n")


# ═════════════════════════════════════════════════════════════════
# DATA FETCHING
# ═════════════════════════════════════════════════════════════════

async def fetch_all() -> dict[str, dict]:
    """Fetch rates from all venues, returns {venue: {symbol: {apy, volume}}}."""
    async with httpx.AsyncClient(timeout=25.0) as client:
        results = await asyncio.gather(
            *[fn(client) for _, fn in VENUES],
            return_exceptions=True,
        )

    venue_data = {}
    for (name, _), result in zip(VENUES, results):
        if isinstance(result, Exception):
            venue_data[name] = {}
        else:
            venue_data[name] = result
    return venue_data


# ═════════════════════════════════════════════════════════════════
# FUNDING SIMULATION — sample + settle model
#
# Each poll samples the current rate into an unsettled accumulator.
# At the top of each funding hour (or 8h for venues like Apex/Paradex),
# the accumulated rate settles into a discrete payment — matching how
# real venues work.
#
# Position fields:
#   funding_settled   — total settled (realized) funding payments
#   funding_unsettled — projected funding accumulating toward next settlement
#   funding_earned    — settled + unsettled (used for NAV / display)
#   _short_samples    — list of (apy, weight_hours) samples since last settlement
#   _long_samples     — same for long leg
#   _last_short_settle_hour — UTC hour of last short leg settlement
#   _last_long_settle_hour  — UTC hour of last long leg settlement
# ═════════════════════════════════════════════════════════════════

def _settlement_hour(now: datetime, cycle_hours: int) -> int:
    """Return the most recent settlement hour boundary as UTC hour-of-day.

    For 1h cycles: every hour (0,1,2,...,23).
    For 4h cycles: 0,4,8,12,16,20.
    For 8h cycles: 0,8,16.
    """
    h = now.hour
    return (h // cycle_hours) * cycle_hours


def _next_settlement(now: datetime, cycle_hours: int) -> datetime:
    """Return the next settlement boundary as a datetime."""
    h = now.hour
    next_h = ((h // cycle_hours) + 1) * cycle_hours
    result = now.replace(minute=0, second=0, microsecond=0)
    if next_h >= 24:
        from datetime import timedelta
        result = result.replace(hour=0) + timedelta(days=1)
    else:
        result = result.replace(hour=next_h)
    return result


def process_funding(state: dict, venue_data: dict, now: datetime, elapsed_hours: float):
    """Sample current rates and settle any completed funding periods.

    Two phases per position per leg:
    1. SAMPLE: record the current rate weighted by elapsed time
    2. SETTLE: if a funding boundary was crossed, compute the payment
       from accumulated samples and reset the accumulator
    """
    if elapsed_hours <= 0:
        return

    for pid, pos in state["positions"].items():
        symbol = pos["symbol"]
        short_venue = pos["short_venue"]
        long_venue = pos["long_venue"]
        size = pos["size_usd"]

        # Look up current rates
        short_info = venue_data.get(short_venue, {}).get(symbol)
        long_info = venue_data.get(long_venue, {}).get(symbol)
        short_apy = short_info["apy"] if short_info else pos.get("last_short_apy", 0)
        long_apy = long_info["apy"] if long_info else pos.get("last_long_apy", 0)

        pos["last_short_apy"] = short_apy
        pos["last_long_apy"] = long_apy
        pos["last_spread_apy"] = short_apy - long_apy
        pos["cycles_held"] = pos.get("cycles_held", 0) + 1

        short_cycle = VENUE_CYCLE_HOURS.get(short_venue, 1)
        long_cycle = VENUE_CYCLE_HOURS.get(long_venue, 1)

        # ── Phase 1: SAMPLE — accumulate rate × time ──
        short_samples = pos.setdefault("_short_samples", [])
        long_samples = pos.setdefault("_long_samples", [])
        short_samples.append((short_apy, elapsed_hours))
        long_samples.append((long_apy, elapsed_hours))

        # Compute unsettled projection (TWAP of samples × cycle_rate)
        def _twap(samples):
            total_w = sum(w for _, w in samples)
            if total_w <= 0:
                return 0.0
            return sum(apy * w for apy, w in samples) / total_w

        short_twap = _twap(short_samples)
        long_twap = _twap(long_samples)
        sample_hours_short = sum(w for _, w in short_samples)
        sample_hours_long = sum(w for _, w in long_samples)

        # Projected unsettled: what the next settlement would pay at current TWAP
        short_unsettled = size * (short_twap / 100 / 8760) * sample_hours_short
        long_unsettled = -size * (long_twap / 100 / 8760) * sample_hours_long
        pos["funding_unsettled"] = round(short_unsettled + long_unsettled, 8)

        # ── Phase 2: SETTLE — check if funding boundary crossed ──
        last_short_settle = pos.get("_last_short_settle_hour")
        last_long_settle = pos.get("_last_long_settle_hour")
        current_short_boundary = _settlement_hour(now, short_cycle)
        current_long_boundary = _settlement_hour(now, long_cycle)

        settled_this_cycle = 0.0

        # Short leg settlement
        if last_short_settle is not None and current_short_boundary != last_short_settle:
            # A settlement boundary was crossed — settle using TWAP
            twap = _twap(short_samples)
            payment = size * (twap / 100) / (8760 / short_cycle)  # one cycle's worth
            settled_this_cycle += payment
            pos["_short_samples"] = []  # reset accumulator

            log_event({
                "event": "SETTLE", "id": pid, "symbol": symbol,
                "leg": "short", "venue": short_venue,
                "twap_apy": round(twap, 2), "cycle_hours": short_cycle,
                "payment_usd": round(payment, 6),
            })
        pos["_last_short_settle_hour"] = current_short_boundary

        # Long leg settlement
        if last_long_settle is not None and current_long_boundary != last_long_settle:
            twap = _twap(long_samples)
            payment = -size * (twap / 100) / (8760 / long_cycle)
            settled_this_cycle += payment
            pos["_long_samples"] = []

            log_event({
                "event": "SETTLE", "id": pid, "symbol": symbol,
                "leg": "long", "venue": long_venue,
                "twap_apy": round(twap, 2), "cycle_hours": long_cycle,
                "payment_usd": round(payment, 6),
            })
        pos["_last_long_settle_hour"] = current_long_boundary

        # Update settled funding
        if settled_this_cycle != 0:
            pos["funding_settled"] = pos.get("funding_settled", 0) + settled_this_cycle

        # Total = settled + unsettled (for NAV)
        pos["funding_earned"] = pos.get("funding_settled", 0) + pos.get("funding_unsettled", 0)

        # Log sample event (less verbose — only log rate snapshot, not every accrual)
        log_event({
            "event": "SAMPLE", "id": pid, "symbol": symbol,
            "short_apy": round(short_apy, 2), "long_apy": round(long_apy, 2),
            "unsettled_usd": round(pos["funding_unsettled"], 6),
            "settled_usd": round(pos.get("funding_settled", 0), 6),
            "total_usd": round(pos["funding_earned"], 6),
        })


# ═════════════════════════════════════════════════════════════════
# EXIT LOGIC
# ═════════════════════════════════════════════════════════════════

def evaluate_exits(state: dict) -> list[str]:
    """Return list of position IDs to exit."""
    now = datetime.now(timezone.utc)
    exits = []

    for pid, pos in state["positions"].items():
        spread = pos.get("last_spread_apy", 0)
        ndpy = spread / 100 / 365

        opened = datetime.fromisoformat(pos["opened_at"])
        hold_hours = (now - opened).total_seconds() / 3600

        reason = None

        if ndpy < XNDPY:
            reason = f"spread degraded (ndpy={ndpy:.6f} < {XNDPY})"
        elif spread < EXIT_SPREAD_APY:
            reason = f"spread below exit threshold ({spread:.1f}% < {EXIT_SPREAD_APY}%)"
        elif hold_hours > MAX_HOLD_DAYS * 24:
            reason = f"max hold exceeded ({hold_hours / 24:.0f} days)"

        if reason:
            pos["_exit_reason"] = reason
            exits.append(pid)

    return exits


def execute_exits(state: dict, exit_ids: list[str]):
    """Close positions and update portfolio."""
    now = datetime.now(timezone.utc)
    portfolio = state["portfolio"]
    summary = state["closed_summary"]

    for pid in exit_ids:
        pos = state["positions"].pop(pid)
        opened = datetime.fromisoformat(pos["opened_at"])
        hold_hours = (now - opened).total_seconds() / 3600

        funding = pos.get("funding_earned", 0)
        fee = pos.get("entry_fee_usd", 0)
        net_pnl = funding - fee

        # Return capital + PnL to cash
        portfolio["cash"] += pos["size_usd"] + net_pnl
        portfolio["total_funding_earned"] += funding
        portfolio["total_realized_pnl"] += net_pnl

        summary["count"] += 1
        summary["total_funding"] += funding
        summary["total_fees"] += fee
        summary["total_pnl"] += net_pnl
        summary["total_hold_hours"] += hold_hours
        if net_pnl > 0:
            summary["wins"] += 1

        reason = pos.get("_exit_reason", "unknown")
        print(f"  {R}EXIT{X}  {pos['symbol']:<8} {pos['short_venue']}↔{pos['long_venue']}  "
              f"held {hold_hours:.0f}h  funding ${funding:+,.2f}  fee ${fee:.2f}  "
              f"net ${net_pnl:+,.2f}  ({reason})")

        log_event({
            "event": "EXIT", "id": pid, "symbol": pos["symbol"],
            "short_venue": pos["short_venue"], "long_venue": pos["long_venue"],
            "reason": reason, "hold_hours": round(hold_hours, 1),
            "funding_earned": round(funding, 4), "fees_paid": round(fee, 4),
            "net_pnl": round(net_pnl, 4),
        })


# ═════════════════════════════════════════════════════════════════
# ROTATION LOGIC
# ═════════════════════════════════════════════════════════════════

def evaluate_rotations(state: dict, pairs: list[dict]):
    """Check if any open position can be improved with a single-leg rotation."""
    pair_by_symbol = {}
    for p in pairs:
        sym = p["symbol"]
        if sym not in pair_by_symbol or p["score"] > pair_by_symbol[sym]["score"]:
            pair_by_symbol[sym] = p

    for pid, pos in list(state["positions"].items()):
        symbol = pos["symbol"]
        best = pair_by_symbol.get(symbol)
        if not best:
            continue

        cur_spread = pos.get("last_spread_apy", 0)
        best_spread = best["ema_spread"]

        if best_spread <= cur_spread:
            continue

        # Identify which leg to rotate
        improvement_apy = best_spread - cur_spread
        improvement_ndpy = improvement_apy / 100 / 365

        # Determine which leg changed
        new_short = best["short_venue"]
        new_long = best["long_venue"]
        old_short = pos["short_venue"]
        old_long = pos["long_venue"]

        if new_short == old_short and new_long == old_long:
            continue

        # Single-leg rotation: pick the leg that differs
        if new_short != old_short and new_long == old_long:
            rot_leg, old_v, new_v = "short", old_short, new_short
        elif new_long != old_long and new_short == old_short:
            rot_leg, old_v, new_v = "long", old_long, new_long
        else:
            # Both legs differ — pick the bigger improvement
            # For simplicity, skip full rotations (too expensive)
            continue

        # Rotation breakeven
        rot_cost = VENUE_FEES.get(old_v, 0) + VENUE_FEES.get(new_v, 0)
        hourly_improvement = improvement_ndpy / 24 if improvement_ndpy > 0 else 0
        rbh = (rot_cost / hourly_improvement) if hourly_improvement > 0 else float("inf")

        if rbh < MRBH:
            rot_fee_usd = pos["size_usd"] * rot_cost
            pos["short_venue"] = new_short
            pos["long_venue"] = new_long
            pos["entry_fee_usd"] = pos.get("entry_fee_usd", 0) + rot_fee_usd
            state["portfolio"]["total_fees_paid"] += rot_fee_usd

            print(f"  {Y}ROTATE{X} {symbol:<8} {rot_leg} leg: {old_v}→{new_v}  "
                  f"rbh={rbh:.1f}h  rot_fee=${rot_fee_usd:.2f}")

            log_event({
                "event": "ROTATION", "id": pid, "symbol": symbol,
                "leg": rot_leg, "from_venue": old_v, "to_venue": new_v,
                "rbh": round(rbh, 1), "rotation_fee": round(rot_fee_usd, 4),
            })


# ═════════════════════════════════════════════════════════════════
# ENTRY LOGIC
# ═════════════════════════════════════════════════════════════════

def evaluate_entries(state: dict, pairs: list[dict]):
    """Enter new positions from qualifying opportunities."""
    portfolio = state["portfolio"]
    counters = state["entry_counters"]
    open_symbols = {p["symbol"] for p in state["positions"].values()}
    entered_this_cycle = 0

    # Track which symbols still qualify
    qualifying = set()

    for pair in pairs:
        symbol = pair["symbol"]
        if symbol in open_symbols:
            continue

        # Convert to NDPY for threshold checks
        ndpy = pair["ema_spread"] / 100 / 365
        be_hours = pair["be_hours"]

        # Must meet thresholds
        if ndpy < ENDPY or be_hours > MEBH or pair["ema_spread"] < MIN_SPREAD_APY:
            continue

        qualifying.add(symbol)

        # Increment counter
        count = counters.get(symbol, 0) + 1
        counters[symbol] = count

        # Check confirmation
        if count < TAG_THRESHOLD:
            continue

        # Max 1 entry per cycle
        if entered_this_cycle >= 1:
            continue

        # Position sizing: base * (ndpy / endpy), capped
        nav = portfolio["nav"]
        target_size = BASE_SIZE_USD * (ndpy / ENDPY)
        max_size = MAX_POSITION_PCT * nav
        available = portfolio["cash"]
        size = min(target_size, max_size, available)

        if size < BASE_SIZE_USD:
            continue
        if len(state["positions"]) >= MAX_POSITIONS:
            continue

        size = round(size, 2)

        # Compute entry fee (round-trip reserved at entry)
        short_fee = VENUE_FEES.get(pair["short_venue"], 0)
        long_fee = VENUE_FEES.get(pair["long_venue"], 0)
        fee_usd = size * 2 * (short_fee + long_fee)

        pid = uuid.uuid4().hex[:8]
        state["positions"][pid] = {
            "id": pid,
            "symbol": symbol,
            "short_venue": pair["short_venue"],
            "long_venue": pair["long_venue"],
            "size_usd": size,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "entry_short_apy": pair["short_apy"],
            "entry_long_apy": pair["long_apy"],
            "entry_spread_apy": pair["spread"],
            "entry_score": pair["score"],
            "entry_fee_usd": round(fee_usd, 4),
            "funding_earned": 0.0,
            "last_short_apy": pair["short_apy"],
            "last_long_apy": pair["long_apy"],
            "last_spread_apy": pair["spread"],
            "cycles_held": 0,
        }

        portfolio["cash"] -= size
        portfolio["total_fees_paid"] += fee_usd
        open_symbols.add(symbol)
        entered_this_cycle += 1
        counters[symbol] = 0  # reset after entry

        print(f"  {G}ENTRY{X}  {symbol:<8} short@{pair['short_venue']:<9} long@{pair['long_venue']:<9} "
              f"${size:>10,.2f}  spread={pair['spread']:.1f}%  score={pair['score']:.0f}  "
              f"ndpy={ndpy:.6f}  fee=${fee_usd:.2f}")

        log_event({
            "event": "ENTRY", "id": pid, "symbol": symbol,
            "short_venue": pair["short_venue"], "long_venue": pair["long_venue"],
            "size_usd": size, "spread_apy": round(pair["spread"], 2),
            "score": round(pair["score"], 1), "fee_usd": round(fee_usd, 4),
        })

    # Reset counters for symbols that no longer qualify
    for sym in list(counters):
        if sym not in qualifying:
            counters[sym] = 0


# ═════════════════════════════════════════════════════════════════
# NAV UPDATE
# ═════════════════════════════════════════════════════════════════

def update_nav(state: dict):
    portfolio = state["portfolio"]
    deployed = sum(p["size_usd"] for p in state["positions"].values())
    unrealized = sum(p.get("funding_earned", 0) for p in state["positions"].values())
    portfolio["nav"] = round(portfolio["cash"] + deployed + unrealized, 2)
    portfolio["peak_nav"] = max(portfolio["peak_nav"], portfolio["nav"])


# ═════════════════════════════════════════════════════════════════
# CYCLE
# ═════════════════════════════════════════════════════════════════

async def run_cycle(state: dict):
    """Execute one simulation cycle."""
    import time

    now = datetime.now(timezone.utc)
    now_ts = time.time()
    state["cycle_count"] += 1
    cycle = state["cycle_count"]

    # Elapsed time since last cycle
    last = state.get("last_cycle_at")
    if last:
        last_dt = datetime.fromisoformat(last)
        elapsed_hours = (now - last_dt).total_seconds() / 3600
    else:
        elapsed_hours = 0

    ts_str = now.strftime("%H:%M:%S")
    n_pos = len(state["positions"])
    print(f"\n{'─' * 90}")
    print(f"  {B}CYCLE {cycle}{X}  {ts_str} UTC  |  "
          f"{n_pos} positions  |  NAV ${state['portfolio']['nav']:,.2f}  |  "
          f"dt={elapsed_hours:.1f}h")
    print(f"{'─' * 90}")

    # 1. Fetch rates
    print(f"  Fetching rates from {len(VENUES)} venues...", end="", flush=True)
    venue_data = await fetch_all()
    active = sum(1 for v in venue_data.values() if v)
    print(f" {active}/{len(VENUES)} active")

    # 2. Update EMA
    current_apy = {}
    for venue, symbols in venue_data.items():
        for symbol, info in symbols.items():
            if info["volume"] >= MIN_VOLUME:
                current_apy[f"{venue}:{symbol}"] = info["apy"]

    prev_ts, prev_ema = load_ema()
    dt = now_ts - prev_ts if prev_ts > 0 else 0
    ema = update_ema(current_apy, prev_ema, dt)
    save_ema(now_ts, ema)

    # 3. Sample rates and settle funding
    if elapsed_hours > 0 and state["positions"]:
        process_funding(state, venue_data, now, elapsed_hours)
        settled = sum(p.get("funding_settled", 0) for p in state["positions"].values())
        unsettled = sum(p.get("funding_unsettled", 0) for p in state["positions"].values())
        print(f"  Funding for {len(state['positions'])} positions ({elapsed_hours:.2f}h):  "
              f"settled ${settled:+,.4f}  unsettled ${unsettled:+,.4f}  "
              f"total ${settled + unsettled:+,.4f}")

    # 4. Build hedge pairs
    pairs = build_hedge_pairs(venue_data, MIN_VOLUME, ema)

    # 5. Evaluate exits
    exit_ids = evaluate_exits(state)
    if exit_ids:
        execute_exits(state, exit_ids)

    # 6. Evaluate rotations
    if state["positions"]:
        evaluate_rotations(state, pairs)

    # 7. Evaluate entries
    evaluate_entries(state, pairs)

    # 8. Update NAV
    update_nav(state)

    # 9. Print summary
    portfolio = state["portfolio"]
    starting = INITIAL_NAV
    pnl = portfolio["nav"] - starting
    pnl_pct = (pnl / starting) * 100
    drawdown = ((portfolio["peak_nav"] - portfolio["nav"]) / portfolio["peak_nav"]) * 100

    print(f"\n  {B}Portfolio{X}:  NAV ${portfolio['nav']:>12,.2f}  "
          f"({'+' if pnl >= 0 else ''}{pnl_pct:.4f}%)  "
          f"cash ${portfolio['cash']:>12,.2f}  "
          f"DD {drawdown:.3f}%")

    if state["positions"]:
        print(f"\n  {'SYMBOL':<8} {'SHORT@':<9} {'LONG@':<9} {'SIZE':>10} "
              f"{'SPREAD':>8} {'SETTLED':>10} {'UNSETTLED':>10} {'HELD':>6}")
        print("  " + "─" * 80)
        for pid, pos in state["positions"].items():
            opened = datetime.fromisoformat(pos["opened_at"])
            hold_h = (now - opened).total_seconds() / 3600
            settled = pos.get("funding_settled", 0)
            unsettled = pos.get("funding_unsettled", 0)
            spread = pos.get("last_spread_apy", 0)
            sc = G if settled > 0 else R if settled < 0 else ""
            uc = G if unsettled > 0 else R if unsettled < 0 else ""
            print(f"  {pos['symbol']:<8} {pos['short_venue']:<9} {pos['long_venue']:<9} "
                  f"${pos['size_usd']:>9,.2f} {spread:>+7.1f}% "
                  f"{sc}${settled:>+9,.4f}{X} {uc}${unsettled:>+9,.4f}{X} {hold_h:>5.0f}h")

    counters_active = {k: v for k, v in state["entry_counters"].items() if v > 0}
    if counters_active:
        parts = [f"{sym}({c}/{TAG_THRESHOLD})" for sym, c in
                 sorted(counters_active.items(), key=lambda x: -x[1])[:8]]
        print(f"\n  Qualifying: {', '.join(parts)}")

    state["last_cycle_at"] = now.isoformat()
    save_state(state)


# ═════════════════════════════════════════════════════════════════
# REPORT
# ═════════════════════════════════════════════════════════════════

def print_report():
    if not STATE_FILE.exists():
        print("  No simulation state found. Run a cycle first.")
        return

    state = load_state()
    p = state["portfolio"]
    s = state["closed_summary"]
    now = datetime.now(timezone.utc)
    started = datetime.fromisoformat(state["started_at"])
    days = max((now - started).total_seconds() / 86400, 0.01)

    print(f"\n{'═' * 70}")
    print(f"  {B}HEDGEHOG PAPER TRADING REPORT{X}")
    print(f"  Period: {started.strftime('%Y-%m-%d %H:%M')} → {now.strftime('%Y-%m-%d %H:%M')} ({days:.1f} days)")
    print(f"  Cycles: {state['cycle_count']}")
    print(f"{'═' * 70}")

    pnl = p["nav"] - INITIAL_NAV
    pnl_pct = (pnl / INITIAL_NAV) * 100
    dd = ((p["peak_nav"] - p["nav"]) / p["peak_nav"]) * 100 if p["peak_nav"] > 0 else 0
    ann_return = (pnl_pct / days) * 365 if days > 0 else 0

    settled = sum(pos.get("funding_settled", 0) for pos in state["positions"].values())
    unsettled = sum(pos.get("funding_unsettled", 0) for pos in state["positions"].values())

    print(f"\n  {B}Portfolio{X}")
    print(f"  {'─' * 40}")
    print(f"  Starting NAV:      ${INITIAL_NAV:>14,.2f}")
    print(f"  Current NAV:       ${p['nav']:>14,.2f}  ({pnl_pct:+.4f}%)")
    print(f"  Peak NAV:          ${p['peak_nav']:>14,.2f}")
    print(f"  Max Drawdown:      {dd:>13.3f}%")

    print(f"\n  {B}Performance{X}")
    print(f"  {'─' * 40}")
    print(f"  Settled Funding:   ${p['total_funding_earned'] + settled:>14,.4f}")
    print(f"  Unsettled Fund:    ${unsettled:>14,.4f}")
    print(f"  Total Fees:        ${p['total_fees_paid']:>14,.4f}")
    print(f"  Realized PnL:      ${p['total_realized_pnl']:>14,.4f}")
    print(f"  Total PnL:         ${pnl:>14,.4f}")
    print(f"  Annualized:        {ann_return:>13.2f}%")

    n_open = len(state["positions"])
    avg_hold = (s["total_hold_hours"] / s["count"]) if s["count"] > 0 else 0
    win_rate = (s["wins"] / s["count"] * 100) if s["count"] > 0 else 0

    print(f"\n  {B}Positions{X}")
    print(f"  {'─' * 40}")
    print(f"  Open:              {n_open:>14}")
    print(f"  Closed:            {s['count']:>14}")
    print(f"  Avg Hold:          {avg_hold:>13.1f}h")
    print(f"  Win Rate:          {win_rate:>13.1f}%")

    if state["positions"]:
        print(f"\n  {B}Current Positions{X}")
        print(f"  {'─' * 40}")
        for pid, pos in state["positions"].items():
            opened = datetime.fromisoformat(pos["opened_at"])
            hold_h = (now - opened).total_seconds() / 3600
            fund = pos.get("funding_earned", 0)
            spread = pos.get("last_spread_apy", 0)
            fc = G if fund > 0 else R
            print(f"  {pos['symbol']:<8} short@{pos['short_venue']:<9} long@{pos['long_venue']:<9} "
                  f"${pos['size_usd']:>8,.2f}  {fc}${fund:>+8,.4f}{X} funding  "
                  f"{hold_h:.0f}h held  {spread:+.1f}% spread")

    # Recent events from ledger
    if LEDGER_FILE.exists():
        lines = LEDGER_FILE.read_text().strip().split("\n")
        recent = lines[-20:] if len(lines) > 20 else lines
        entries = sum(1 for l in lines if '"ENTRY"' in l)
        exits = sum(1 for l in lines if '"EXIT"' in l)
        rotations = sum(1 for l in lines if '"ROTATION"' in l)
        samples = sum(1 for l in lines if '"SAMPLE"' in l)
        settles = sum(1 for l in lines if '"SETTLE"' in l)
        print(f"\n  {B}Ledger{X}")
        print(f"  {'─' * 40}")
        print(f"  Total events:      {len(lines):>14}")
        print(f"  Entries:           {entries:>14}")
        print(f"  Exits:             {exits:>14}")
        print(f"  Rotations:         {rotations:>14}")
        print(f"  Rate samples:      {samples:>14}")
        print(f"  Settlements:       {settles:>14}")

    print()


# ═════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════

async def main():
    args = sys.argv[1:]

    if "--reset" in args:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
        if LEDGER_FILE.exists():
            LEDGER_FILE.unlink()
        print("  Paper trading state reset.")
        return

    if "--report" in args:
        print_report()
        return

    loop_mode = "--loop" in args
    interval = 60
    for a in args:
        if a.startswith("--interval"):
            if "=" in a:
                interval = int(a.split("=", 1)[1])
            else:
                idx = args.index(a)
                if idx + 1 < len(args):
                    interval = int(args[idx + 1])

    state = load_state()

    if loop_mode:
        print(f"  {B}Hedgehog Paper Trader{X} — continuous mode ({interval}s interval)")
        print(f"  NAV: ${state['portfolio']['nav']:,.2f}  |  "
              f"Positions: {len(state['positions'])}  |  "
              f"Cycles: {state['cycle_count']}")
        print(f"  Thresholds: endpy={ENDPY}  xndpy={XNDPY}  mebh={MEBH}h  "
              f"tag={TAG_THRESHOLD}  max_pos={MAX_POSITIONS}")

        while True:
            try:
                await run_cycle(state)
                await asyncio.sleep(interval)
            except KeyboardInterrupt:
                print(f"\n  Stopped. {state['cycle_count']} cycles completed.")
                save_state(state)
                break
            except Exception as e:
                print(f"  {R}Cycle error: {e}{X}")
                await asyncio.sleep(interval)
    else:
        await run_cycle(state)


if __name__ == "__main__":
    asyncio.run(main())
