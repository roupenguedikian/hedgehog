"""
Hedgehog Engine v2 — Decision Engine.

Evaluates every tracked symbol per cycle and returns a Tag indicating
the action to take: HOLD, EXIT, ROTATION, ROTATION_SINGLE, ENTRY,
EMERGENCY_EXIT, or NO_ENTRY.

Implements the decision tree from ENGINE_SPEC_v2.md exactly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum


class TagType(str, Enum):
    HOLD = "HOLD"
    EXIT = "EXIT"
    ROTATION = "ROTATION"
    ROTATION_SINGLE = "ROTATION_SINGLE"
    ENTRY = "ENTRY"
    EMERGENCY_EXIT = "EMERGENCY_EXIT"
    NO_ENTRY = "NO_ENTRY"


@dataclass
class Tag:
    symbol: str
    tag_type: TagType
    short_venue: str | None = None
    long_venue: str | None = None
    bndpy: float = 0.0
    cndpy: float = 0.0
    bsfr: float = 0.0
    blfr: float = 0.0
    bsfr_venue: str | None = None
    blfr_venue: str | None = None
    ndpy_improvement: float = 0.0
    bbh: float = 0.0
    tag_counter: int = 0
    position_size: float | None = None
    status: str | None = None  # ACTIVE, DEGRADED, CLOSED, or None
    rotation_leg: str | None = None  # "short" or "long" for ROTATION_SINGLE
    reason: str = ""


@dataclass
class EngineConfig:
    endpy: float = float(os.environ.get("ENTRY_NDPY", "0.0008"))
    xndpy: float = float(os.environ.get("EXIT_NDPY", "0.0003"))
    mebh: float = float(os.environ.get("ENTRY_BREAKEVEN", "12"))
    mrbh: float = float(os.environ.get("ROTATION_BREAKEVEN", "6"))
    tag_threshold: int = int(os.environ.get("TAG_THRESHOLD", "3"))
    entry_fail_cooldown: int = int(os.environ.get("ENTRY_FAIL_COOLDOWN", "10"))
    max_entries_per_cycle: int = int(os.environ.get("MAX_ENTRIES_PER_CYCLE", "1"))
    rotation_cooldown: int = int(os.environ.get("ROTATION_COOLDOWN", "60"))


# Taker fees (decimal). Lowercase venue names matching DB.
VENUE_FEES: dict[str, float] = {
    "hyperliquid": 0.00045,
    "aster":       0.00035,
    "lighter":     0.0,
    "drift":       0.0003,
    "dydx":        0.0005,
    "apex":        0.00025,
}

# Funding cycle hours per venue.
VENUE_CYCLE_HOURS: dict[str, int] = {
    "hyperliquid": 1,
    "lighter":     8,
    "drift":       1,
    "dydx":        1,
    "apex":        1,
    "aster":       1,  # default; Aster varies per market (1/4/8h)
}


class EngineState:
    """In-memory state that persists across cycles within one process run.

    Resets on restart — spec explicitly accepts this.
    """

    def __init__(self):
        self.entry_counters: dict[str, int] = {}        # symbol → consecutive qualifying cycles
        self.entry_fail_counts: dict[str, int] = {}     # symbol → consecutive entry failures
        self.entry_fail_cooldowns: dict[str, int] = {}   # symbol → remaining cooldown cycles
        self.last_rotation_cycle: dict[str, int] = {}    # position_id → last rotation cycle


def _compute_dpy(rate: float, cycle_hours: int) -> float:
    """Convert a per-cycle rate to daily % yield."""
    return rate * (24 / cycle_hours)


def _compute_breakeven_hours(short_venue: str, long_venue: str, gross_dpy: float) -> float:
    """How many hours of funding to recoup round-trip fees."""
    if gross_dpy <= 0:
        return float("inf")
    entry_fee = VENUE_FEES.get(short_venue, 0) + VENUE_FEES.get(long_venue, 0)
    exit_fee = entry_fee  # symmetric
    round_trip = entry_fee + exit_fee
    hourly_yield = gross_dpy / 24
    return round_trip / hourly_yield


def _compute_rotation_breakeven(
    old_venue: str, new_venue: str, ndpy_improvement: float
) -> float:
    """Breakeven hours for a single-leg rotation."""
    if ndpy_improvement <= 0:
        return float("inf")
    rotation_cost = VENUE_FEES.get(old_venue, 0) + VENUE_FEES.get(new_venue, 0)
    hourly_improvement = ndpy_improvement / 24
    return rotation_cost / hourly_improvement


def _best_rates(
    symbol: str,
    ema_data: dict[tuple[str, str], float],
) -> tuple[str, float, str, float]:
    """Find best short venue (highest EMA) and best long venue (lowest EMA).

    Returns: (short_venue, bsfr, long_venue, blfr)
    """
    candidates = [
        (venue, ema)
        for (venue, sym), ema in ema_data.items()
        if sym == symbol
    ]
    if len(candidates) < 2:
        return "", 0.0, "", 0.0

    # Best short = highest rate (you collect funding)
    short_venue, bsfr = max(candidates, key=lambda x: x[1])
    # Best long = lowest rate (you pay least or get paid)
    long_venue, blfr = min(candidates, key=lambda x: x[1])

    if short_venue == long_venue:
        return "", 0.0, "", 0.0

    return short_venue, bsfr, long_venue, blfr


def evaluate(
    symbol: str,
    positions: dict[str, dict],
    ema_data: dict[tuple[str, str], float],
    config: EngineConfig,
    state: EngineState,
    cycle_number: int,
) -> Tag:
    """Evaluate a single symbol and return a Tag.

    Args:
        symbol: the symbol to evaluate (e.g. "BTC")
        positions: {position_id: row_dict} for this symbol's open positions
            row_dict has: position_id, symbol, short_venue, long_venue,
                          short_size, long_size, status, last_rotation_at
        ema_data: {(venue, symbol): ema_value} from get_venue_emas()
        config: engine thresholds
        state: in-memory counters (persists across cycles)
        cycle_number: current cycle number
    """

    # ── Compute best rates for this symbol ──
    short_venue, bsfr, long_venue, blfr = _best_rates(symbol, ema_data)

    # Common fields set on every tag before return
    def _tag(tag_type: TagType, **kwargs) -> Tag:
        t = Tag(symbol, tag_type, bsfr=bsfr, blfr=blfr,
                bsfr_venue=short_venue, blfr_venue=long_venue,
                tag_counter=state.entry_counters.get(symbol, 0), **kwargs)
        # Set position status/size if we have a position
        if positions:
            pos = next(iter(positions.values()))
            t.status = pos.get("status")
            t.position_size = pos.get("short_size")
        return t

    if not short_venue:
        # Not enough venue data for this symbol
        state.entry_counters[symbol] = 0
        return _tag(TagType.NO_ENTRY, reason="insufficient venue data")

    short_cycle = VENUE_CYCLE_HOURS.get(short_venue, 1)
    long_cycle = VENUE_CYCLE_HOURS.get(long_venue, 1)

    short_dpy = _compute_dpy(bsfr, short_cycle)
    long_dpy = _compute_dpy(blfr, long_cycle)
    bndpy = short_dpy - long_dpy
    bbh = _compute_breakeven_hours(short_venue, long_venue, bndpy)

    # ── Q1: Has open position? ──
    if not positions:
        return _evaluate_entry(symbol, bndpy, bbh, bsfr, blfr,
                               short_venue, long_venue,
                               config, state, cycle_number)

    # There should be at most one active/degraded position per symbol
    pos_id, pos = next(iter(positions.items()))

    # ── Q2: Both legs alive? ──
    if pos["status"] in ("DEGRADED", "degraded"):
        return _tag(TagType.EMERGENCY_EXIT,
                    short_venue=pos["short_venue"], long_venue=pos["long_venue"],
                    bndpy=bndpy, bbh=bbh,
                    reason="DEGRADED position — emergency exit")

    # ── Compute current NDPY from actual position venues ──
    cur_short = pos["short_venue"]
    cur_long = pos["long_venue"]
    cur_short_ema = ema_data.get((cur_short, symbol))
    cur_long_ema = ema_data.get((cur_long, symbol))

    if cur_short_ema is None or cur_long_ema is None:
        return _tag(TagType.HOLD, cndpy=0, bndpy=bndpy,
                    reason="position venue data stale, holding")

    cur_short_cycle = VENUE_CYCLE_HOURS.get(cur_short, 1)
    cur_long_cycle = VENUE_CYCLE_HOURS.get(cur_long, 1)
    cur_short_dpy = _compute_dpy(cur_short_ema, cur_short_cycle)
    cur_long_dpy = _compute_dpy(cur_long_ema, cur_long_cycle)
    cndpy = cur_short_dpy - cur_long_dpy

    # ── Q3: Is current hedge bad? ──
    if cndpy < config.xndpy:
        # ── Q4: Rotate or exit? ──
        if bndpy > config.endpy:
            crbh = _compute_full_rotation_breakeven(
                cur_short, cur_long, short_venue, long_venue,
                bndpy - cndpy,
            )
            if crbh < config.mrbh:
                return _tag(TagType.ROTATION,
                            short_venue=short_venue, long_venue=long_venue,
                            bndpy=bndpy, cndpy=cndpy, bbh=crbh,
                            reason=f"bad hedge, rotating (crbh={crbh:.1f}h)")
            else:
                return _tag(TagType.EXIT,
                            short_venue=cur_short, long_venue=cur_long,
                            bndpy=bndpy, cndpy=cndpy,
                            reason=f"bad hedge, rotation too expensive (crbh={crbh:.1f}h)")
        else:
            return _tag(TagType.EXIT,
                        short_venue=cur_short, long_venue=cur_long,
                        bndpy=bndpy, cndpy=cndpy,
                        reason="bad hedge, nothing worth rotating to")

    # ── Q5: Hedge is fine — is there something better? ──
    if cndpy >= bndpy:
        return _tag(TagType.HOLD, cndpy=cndpy, bndpy=bndpy,
                    reason="already in best group")

    last_rot = state.last_rotation_cycle.get(pos_id, -config.rotation_cooldown - 1)
    if (cycle_number - last_rot) < config.rotation_cooldown:
        return _tag(TagType.HOLD, cndpy=cndpy, bndpy=bndpy,
                    reason="rotation cooldown")

    weak_leg, old_venue, new_venue = _identify_weak_leg(
        cur_short, cur_long, short_venue, long_venue,
        cur_short_ema, cur_long_ema, bsfr, blfr, symbol,
    )
    ndpy_improvement = bndpy - cndpy
    rbh = _compute_rotation_breakeven(old_venue, new_venue, ndpy_improvement)

    if rbh < config.mrbh:
        return _tag(TagType.ROTATION_SINGLE,
                    short_venue=short_venue if weak_leg == "short" else cur_short,
                    long_venue=long_venue if weak_leg == "long" else cur_long,
                    bndpy=bndpy, cndpy=cndpy, bbh=rbh,
                    ndpy_improvement=ndpy_improvement,
                    rotation_leg=weak_leg,
                    reason=f"single-leg rotation ({weak_leg} leg, rbh={rbh:.1f}h)")
    else:
        return _tag(TagType.HOLD, cndpy=cndpy, bndpy=bndpy,
                    reason=f"rotation not worth it (rbh={rbh:.1f}h)")


def _evaluate_entry(
    symbol: str,
    bndpy: float,
    bbh: float,
    bsfr: float,
    blfr: float,
    short_venue: str,
    long_venue: str,
    config: EngineConfig,
    state: EngineState,
    cycle_number: int,
) -> Tag:
    """Q6: No position — worth entering?"""

    def _tag(tag_type: TagType, **kwargs) -> Tag:
        return Tag(symbol, tag_type, bsfr=bsfr, blfr=blfr,
                   bsfr_venue=short_venue, blfr_venue=long_venue,
                   tag_counter=state.entry_counters.get(symbol, 0), **kwargs)

    # Decrement cooldowns
    if symbol in state.entry_fail_cooldowns:
        state.entry_fail_cooldowns[symbol] -= 1
        if state.entry_fail_cooldowns[symbol] <= 0:
            del state.entry_fail_cooldowns[symbol]
            state.entry_fail_counts[symbol] = 0
        else:
            state.entry_counters[symbol] = 0
            return _tag(TagType.NO_ENTRY, bndpy=bndpy,
                        reason=f"entry cooldown ({state.entry_fail_cooldowns[symbol]} cycles left)")

    # 6a: doesn't meet thresholds
    if bndpy < config.endpy or bbh > config.mebh:
        state.entry_counters[symbol] = 0
        reason = f"bndpy={bndpy:.6f}" if bndpy < config.endpy else f"bbh={bbh:.1f}h"
        return _tag(TagType.NO_ENTRY, bndpy=bndpy, bbh=bbh,
                    reason=f"below threshold ({reason})")

    # 6b: meets thresholds — increment counter
    counter = state.entry_counters.get(symbol, 0) + 1
    state.entry_counters[symbol] = counter

    # 6c: check if confirmed
    if counter >= config.tag_threshold:
        return _tag(TagType.ENTRY,
                    short_venue=short_venue, long_venue=long_venue,
                    bndpy=bndpy, bbh=bbh,
                    reason=f"confirmed after {counter} cycles")
    else:
        return _tag(TagType.NO_ENTRY, bndpy=bndpy, bbh=bbh,
                    reason=f"qualifying ({counter}/{config.tag_threshold})")


def _compute_full_rotation_breakeven(
    old_short: str, old_long: str,
    new_short: str, new_long: str,
    ndpy_improvement: float,
) -> float:
    """Breakeven hours for a full rotation (close both legs, open both on new venues)."""
    if ndpy_improvement <= 0:
        return float("inf")
    cost = (VENUE_FEES.get(old_short, 0) + VENUE_FEES.get(old_long, 0) +
            VENUE_FEES.get(new_short, 0) + VENUE_FEES.get(new_long, 0))
    hourly_improvement = ndpy_improvement / 24
    return cost / hourly_improvement


def _identify_weak_leg(
    cur_short: str, cur_long: str,
    best_short: str, best_long: str,
    cur_short_ema: float, cur_long_ema: float,
    bsfr: float, blfr: float,
    symbol: str,
) -> tuple[str, str, str]:
    """Identify which leg is weaker — the one with more room for improvement.

    Returns: (weak_leg, old_venue, new_venue)
    """
    # Short leg improvement: how much more we'd earn by moving to best short venue
    short_improvement = bsfr - cur_short_ema
    # Long leg improvement: how much less we'd pay by moving to best long venue
    long_improvement = cur_long_ema - blfr

    if short_improvement >= long_improvement:
        return "short", cur_short, best_short
    else:
        return "long", cur_long, best_long


def evaluate_all(
    symbols: list[str],
    all_positions: dict[str, dict],
    ema_data: dict[tuple[str, str], float],
    config: EngineConfig,
    state: EngineState,
    cycle_number: int,
) -> list[Tag]:
    """Evaluate all symbols and return list of Tags.

    Args:
        all_positions: {position_id: row_dict} for ALL open/degraded positions.
    """
    tags = []
    for symbol in symbols:
        # Filter positions for this symbol
        sym_positions = {
            pid: pos for pid, pos in all_positions.items()
            if pos["symbol"] == symbol
        }
        tag = evaluate(symbol, sym_positions, ema_data, config, state, cycle_number)
        tags.append(tag)
    return tags


async def write_engine_cycles(pool, tags: list[Tag]):
    """Write one row per tag to engine_cycles table."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    rows = [
        (now, t.symbol, t.tag_type.value, t.status, t.cndpy, t.bndpy,
         t.bsfr, t.blfr, t.bsfr_venue, t.blfr_venue, t.bbh,
         t.tag_counter, t.position_size, t.reason)
        for t in tags
    ]
    try:
        await pool.executemany(
            """INSERT INTO engine_cycles
               (timestamp, symbol, tag, status, cndpy, bndpy,
                bsfr, blfr, bsfr_venue, blfr_venue, bbh,
                tag_counter, position_size, reasoning)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)""",
            rows,
        )
    except Exception as e:
        print(f"  Warning: failed to write engine_cycles: {e}")
