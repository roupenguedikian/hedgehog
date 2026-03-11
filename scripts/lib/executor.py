"""
Hedgehog Engine v2 — Tag Executor.

Executes tags in spec order:
  1. EMERGENCY_EXIT (DEGRADED positions, immediate)
  2. EXIT (free up capital, verify both legs closed)
  3. ROTATION / ROTATION_SINGLE (capital-neutral)
  4. ENTRY (1 per cycle, sort by bndpy desc)

Uses subprocess to call existing venue market order scripts.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone

import asyncpg

from .engine import EngineState, Tag, TagType

SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(SCRIPTS_DIR)

# Map venue name → market order script
VENUE_ORDER_SCRIPTS: dict[str, str] = {
    "hyperliquid": "hl_market_order.py",
    "aster":       "aster_market_order.py",
    "lighter":     "lighter_market_order.py",
    "drift":       "drift_market_order.py",
    "dydx":        "dydx_market_order.py",
    "apex":        "apex_market_order.py",
}


def _run_order(venue: str, symbol: str, side: str, size: float,
               reduce_only: bool = False, dry_run: bool = False) -> tuple[bool, str]:
    """Run a venue market order script via subprocess.

    Returns (success, output_text).
    """
    script = VENUE_ORDER_SCRIPTS.get(venue)
    if not script:
        return False, f"no order script for venue '{venue}'"

    script_path = os.path.join(SCRIPTS_DIR, script)
    cmd = [sys.executable, script_path, symbol, side, str(size)]
    if reduce_only:
        cmd.append("--reduce-only")
    if dry_run:
        cmd.append("--dry-run")

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
            cwd=PROJECT_ROOT,
        )
        output = result.stdout + result.stderr
        success = result.returncode == 0
        return success, output.strip()
    except subprocess.TimeoutExpired:
        return False, "order timed out (30s)"
    except Exception as e:
        return False, str(e)


async def _update_position_status(pool: asyncpg.Pool, position_id: str,
                                  status: str, **extra):
    """Update hedge_positions status and optional columns."""
    sets = ["status = $2"]
    vals: list = [position_id, status]
    idx = 3

    if status == "CLOSED" and "closed_at" not in extra:
        extra["closed_at"] = datetime.now(timezone.utc)
    if status == "DEGRADED" and "degraded_at" not in extra:
        extra["degraded_at"] = datetime.now(timezone.utc)

    for col, val in extra.items():
        sets.append(f"{col} = ${idx}")
        vals.append(val)
        idx += 1

    sql = f"UPDATE hedge_positions SET {', '.join(sets)} WHERE position_id = $1"
    try:
        await pool.execute(sql, *vals)
    except Exception as e:
        print(f"    DB error updating position {position_id}: {e}")


async def _insert_position(pool: asyncpg.Pool, symbol: str,
                           short_venue: str, long_venue: str,
                           size: float, short_price: float, long_price: float):
    """Insert a new hedge_positions row with status ACTIVE."""
    pid = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    basis = short_price - long_price
    try:
        await pool.execute(
            """INSERT INTO hedge_positions
               (position_id, symbol, short_venue, long_venue,
                short_size, long_size, short_entry, long_entry,
                entry_basis, status, opened_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
            pid, symbol, short_venue, long_venue,
            size, size, short_price, long_price,
            basis, "ACTIVE", now,
        )
        print(f"    Position {pid} created: {symbol} short@{short_venue} long@{long_venue} ${size:.2f}")
    except Exception as e:
        print(f"    DB error inserting position: {e}")


async def execute_tags(
    pool: asyncpg.Pool,
    tags: list[Tag],
    positions: dict[str, dict],
    state: EngineState,
    cycle_number: int,
    dry_run: bool = False,
    size_map: dict[str, float] | None = None,
):
    """Execute all tags in spec-defined order.

    Args:
        pool: asyncpg connection pool
        tags: all tags from engine.evaluate_all()
        positions: {position_id: row_dict} for all open/degraded positions
        state: engine state (for fail counters, rotation tracking)
        cycle_number: current cycle number
        dry_run: if True, print actions but don't execute
        size_map: {symbol: size_usd} for ENTRY tags (from sizing)
    """
    if size_map is None:
        size_map = {}

    emergency_exits = [t for t in tags if t.tag_type == TagType.EMERGENCY_EXIT]
    exits = [t for t in tags if t.tag_type == TagType.EXIT]
    rotations = [t for t in tags
                 if t.tag_type in (TagType.ROTATION, TagType.ROTATION_SINGLE)]
    entries = [t for t in tags if t.tag_type == TagType.ENTRY]

    # Sort rotations by improvement desc
    rotations.sort(key=lambda t: t.ndpy_improvement, reverse=True)
    # Sort entries by bndpy desc
    entries.sort(key=lambda t: t.bndpy, reverse=True)

    # ── 1. EMERGENCY EXITS ──
    for tag in emergency_exits:
        print(f"\n  [EMERGENCY EXIT] {tag.symbol} — {tag.reason}")
        pos_items = [(pid, p) for pid, p in positions.items()
                     if p["symbol"] == tag.symbol]
        for pid, pos in pos_items:
            await _execute_exit(pool, pid, pos, dry_run)

    # ── 2. EXITS ──
    for tag in exits:
        print(f"\n  [EXIT] {tag.symbol} — {tag.reason}")
        pos_items = [(pid, p) for pid, p in positions.items()
                     if p["symbol"] == tag.symbol]
        for pid, pos in pos_items:
            await _execute_exit(pool, pid, pos, dry_run)

    # ── 3. ROTATIONS ──
    for tag in rotations:
        print(f"\n  [{tag.tag_type.value}] {tag.symbol} — {tag.reason}")
        pos_items = [(pid, p) for pid, p in positions.items()
                     if p["symbol"] == tag.symbol]
        for pid, pos in pos_items:
            if tag.tag_type == TagType.ROTATION_SINGLE:
                await _execute_rotation_single(pool, pid, pos, tag, state,
                                               cycle_number, dry_run)
            else:
                await _execute_full_rotation(pool, pid, pos, tag, state,
                                             cycle_number, dry_run)

    # ── 4. ENTRIES ──
    entries_executed = 0
    max_entries = int(os.environ.get("MAX_ENTRIES_PER_CYCLE", "1"))
    entry_fail_cooldown = int(os.environ.get("ENTRY_FAIL_COOLDOWN", "10"))

    for tag in entries:
        if entries_executed >= max_entries:
            break

        # Check fail cooldown
        if state.entry_fail_counts.get(tag.symbol, 0) >= 3:
            state.entry_fail_cooldowns[tag.symbol] = entry_fail_cooldown
            print(f"\n  [ENTRY SKIP] {tag.symbol} — 3 consecutive failures, cooling down")
            continue

        size = size_map.get(tag.symbol, 0)
        if size <= 0:
            print(f"\n  [ENTRY SKIP] {tag.symbol} — size too small")
            continue

        print(f"\n  [ENTRY] {tag.symbol} short@{tag.short_venue} long@{tag.long_venue} "
              f"${size:.2f} — {tag.reason}")

        success = await _execute_entry(pool, tag, size, state, dry_run)
        if success:
            entries_executed += 1
            state.entry_counters[tag.symbol] = 0
            state.entry_fail_counts[tag.symbol] = 0


async def _execute_exit(pool: asyncpg.Pool, position_id: str,
                        pos: dict, dry_run: bool):
    """Close both legs of a position."""
    symbol = pos["symbol"]
    short_venue = pos["short_venue"]
    long_venue = pos["long_venue"]
    short_size = pos.get("short_size", 0)
    long_size = pos.get("long_size", 0)

    print(f"    Closing short@{short_venue} ({short_size}) + long@{long_venue} ({long_size})")

    if dry_run:
        print(f"    [DRY RUN] Would close both legs")
        return

    # Close short leg (buy to close)
    short_ok, short_out = _run_order(short_venue, symbol, "buy", short_size, reduce_only=True)
    print(f"    Short leg close: {'OK' if short_ok else 'FAILED'}")

    # Close long leg (sell to close)
    long_ok, long_out = _run_order(long_venue, symbol, "sell", long_size, reduce_only=True)
    print(f"    Long leg close: {'OK' if long_ok else 'FAILED'}")

    if short_ok and long_ok:
        await _update_position_status(pool, position_id, "CLOSED")
        print(f"    Position {position_id} → CLOSED")
    else:
        await _update_position_status(pool, position_id, "DEGRADED")
        print(f"    Position {position_id} → DEGRADED (partial close)")


async def _execute_rotation_single(
    pool: asyncpg.Pool, position_id: str, pos: dict,
    tag: Tag, state: EngineState, cycle_number: int, dry_run: bool,
):
    """Rotate a single leg: close weak leg, open replacement on new venue."""
    symbol = pos["symbol"]
    leg = tag.rotation_leg  # "short" or "long"

    if leg == "short":
        old_venue = pos["short_venue"]
        new_venue = tag.short_venue
        close_side, open_side = "buy", "sell"
        size = pos.get("short_size", 0)
    else:
        old_venue = pos["long_venue"]
        new_venue = tag.long_venue
        close_side, open_side = "sell", "buy"
        size = pos.get("long_size", 0)

    print(f"    Rotating {leg} leg: {old_venue} → {new_venue} (size={size})")

    if dry_run:
        print(f"    [DRY RUN] Would rotate {leg} leg")
        return

    # Close old leg
    close_ok, _ = _run_order(old_venue, symbol, close_side, size, reduce_only=True)
    if not close_ok:
        print(f"    Close failed — aborting rotation, will retry next cycle")
        return

    # Open new leg
    open_ok, _ = _run_order(new_venue, symbol, open_side, size)
    if not open_ok:
        print(f"    Open failed — position now DEGRADED")
        await _update_position_status(pool, position_id, "DEGRADED")
        return

    # Update position venues
    now = datetime.now(timezone.utc)
    if leg == "short":
        await pool.execute(
            "UPDATE hedge_positions SET short_venue=$1, last_rotation_at=$2 WHERE position_id=$3",
            new_venue, now, position_id,
        )
    else:
        await pool.execute(
            "UPDATE hedge_positions SET long_venue=$1, last_rotation_at=$2 WHERE position_id=$3",
            new_venue, now, position_id,
        )
    state.last_rotation_cycle[position_id] = cycle_number
    print(f"    Rotation complete: {leg} leg now on {new_venue}")


async def _execute_full_rotation(
    pool: asyncpg.Pool, position_id: str, pos: dict,
    tag: Tag, state: EngineState, cycle_number: int, dry_run: bool,
):
    """Full rotation: close both legs, open both on new venues."""
    symbol = pos["symbol"]
    size = min(pos.get("short_size", 0), pos.get("long_size", 0))

    print(f"    Full rotation: {pos['short_venue']}/{pos['long_venue']} → "
          f"{tag.short_venue}/{tag.long_venue}")

    if dry_run:
        print(f"    [DRY RUN] Would fully rotate")
        return

    # Close both legs
    short_ok, _ = _run_order(pos["short_venue"], symbol, "buy", size, reduce_only=True)
    long_ok, _ = _run_order(pos["long_venue"], symbol, "sell", size, reduce_only=True)

    if not (short_ok and long_ok):
        await _update_position_status(pool, position_id, "DEGRADED")
        print(f"    Close failed — position DEGRADED")
        return

    # Open both legs on new venues
    new_short_ok, _ = _run_order(tag.short_venue, symbol, "sell", size)
    new_long_ok, _ = _run_order(tag.long_venue, symbol, "buy", size)

    if not (new_short_ok and new_long_ok):
        await _update_position_status(pool, position_id, "DEGRADED")
        print(f"    Open failed — position DEGRADED")
        return

    now = datetime.now(timezone.utc)
    await pool.execute(
        """UPDATE hedge_positions SET short_venue=$1, long_venue=$2,
           last_rotation_at=$3 WHERE position_id=$4""",
        tag.short_venue, tag.long_venue, now, position_id,
    )
    state.last_rotation_cycle[position_id] = cycle_number
    print(f"    Full rotation complete")


async def _execute_entry(
    pool: asyncpg.Pool, tag: Tag, size: float,
    state: EngineState, dry_run: bool,
) -> bool:
    """Open a new hedge: short on one venue, long on another.

    Returns True if both legs filled successfully.
    """
    symbol = tag.symbol

    if dry_run:
        print(f"    [DRY RUN] Would open short@{tag.short_venue} + long@{tag.long_venue} ${size:.2f}")
        return True

    # TODO: convert USD size to base asset size using mark price
    # For now, this is a placeholder — real implementation needs price lookup
    print(f"    Opening short@{tag.short_venue} + long@{tag.long_venue}")

    # Place short leg
    short_ok, short_out = _run_order(tag.short_venue, symbol, "sell", size)
    # Place long leg
    long_ok, long_out = _run_order(tag.long_venue, symbol, "buy", size)

    if short_ok and long_ok:
        # Both filled — create position
        await _insert_position(pool, symbol, tag.short_venue, tag.long_venue,
                               size, 0.0, 0.0)  # prices would come from fill data
        return True

    if short_ok != long_ok:
        # One leg filled, other failed — DEGRADED
        print(f"    Partial fill — attempting recovery")
        # Try to close the filled leg
        if short_ok:
            _run_order(tag.short_venue, symbol, "buy", size, reduce_only=True)
        else:
            _run_order(tag.long_venue, symbol, "sell", size, reduce_only=True)
        state.entry_fail_counts[symbol] = state.entry_fail_counts.get(symbol, 0) + 1
        return False

    # Both failed
    state.entry_fail_counts[symbol] = state.entry_fail_counts.get(symbol, 0) + 1
    print(f"    Both legs failed (fail count: {state.entry_fail_counts[symbol]})")
    return False
