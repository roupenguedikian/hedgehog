#!/usr/bin/env python3
"""
Hedgehog Engine v2 — Main Entry Point.

Single-process pipeline run by cron every 60s:
  collect → ingest → EMA → evaluate → size → execute

Usage:
    python3 scripts/collect_all.py                # single cycle, dry-run
    python3 scripts/collect_all.py --live          # single cycle, live execution
    python3 scripts/collect_all.py --loop          # continuous, every 60s
    python3 scripts/collect_all.py --loop --live   # continuous, live

Environment: see ENGINE_SPEC_v2.md for all env vars and defaults.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ── Load .env ──────────────────────────────────────────────────
def load_env(path: str):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_env(str(PROJECT_ROOT / ".env"))

# Add project root to path (for adapter imports used by collect_to_db)
sys.path.insert(0, str(PROJECT_ROOT))

import asyncpg
import yaml

# Import from existing collector
from scripts.collect_to_db import ALL_VENUES, get_database_url, run_collection

# Import engine library
from scripts.lib.ema import get_venue_emas
from scripts.lib.engine import EngineConfig, EngineState, Tag, TagType, evaluate_all, write_engine_cycles
from scripts.lib.executor import execute_tags
from scripts.lib.sizing import compute_size

# ── Config ─────────────────────────────────────────────────────

COLLECT_INTERVAL = int(os.environ.get("COLLECT_INTERVAL", "60"))
LOCKFILE = Path("/tmp") / "hedgehog_engine_v2.lock"


def load_symbols() -> list[str]:
    """Load tracked symbols from config/venues.yaml."""
    cfg_path = PROJECT_ROOT / "config" / "venues.yaml"
    if cfg_path.exists():
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f)
        return cfg.get("symbols", [])
    return ["BTC", "ETH", "SOL"]


def _acquire_lock() -> int | None:
    """Acquire a lockfile to prevent overlapping runs. Returns fd or None."""
    LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCKFILE), os.O_CREAT | os.O_WRONLY)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(fd, str(os.getpid()).encode())
        return fd
    except (OSError, IOError):
        return None


def _release_lock(fd: int):
    """Release the lockfile."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        LOCKFILE.unlink(missing_ok=True)
    except Exception:
        pass


async def run_cycle(pool: asyncpg.Pool, symbols: list[str],
                    config: EngineConfig, state: EngineState,
                    cycle_number: int, dry_run: bool):
    """Run one complete engine cycle."""
    t0 = time.monotonic()
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")

    print(f"\n{'━'*60}")
    print(f"  Cycle {cycle_number} — {ts} {'[DRY RUN]' if dry_run else '[LIVE]'}")
    print(f"{'━'*60}")

    # ── Step 1: Collect from all venues ──
    print(f"\n  ▸ Collecting from {len(ALL_VENUES)} venues...")
    await run_collection(pool, ALL_VENUES)

    # ── Step 2: Compute EMA (includes freshness + outlier gates) ──
    print(f"  ▸ Computing EMA...")
    ema_data, outlier_flags = await get_venue_emas(pool, symbols)
    fresh_count = len(set(v for v, _ in ema_data.keys()))
    print(f"    {len(ema_data)} venue/symbol pairs, {fresh_count} fresh venues")

    if not ema_data:
        print(f"  ✗ No fresh data — skipping evaluation")
        return

    # ── Step 3: Read NAV ──
    row = await pool.fetchrow("SELECT total_nav FROM portfolio_summary")
    nav = float(row["total_nav"]) if row and row["total_nav"] else 0.0
    print(f"  ▸ NAV: ${nav:,.2f}")

    if nav <= 0:
        print(f"  ✗ NAV is zero — skipping evaluation")
        return

    # ── Step 4: Load positions ──
    pos_rows = await pool.fetch(
        "SELECT * FROM hedge_positions WHERE status IN ('ACTIVE', 'DEGRADED', 'open')"
    )
    positions = {r["position_id"]: dict(r) for r in pos_rows}
    active = sum(1 for p in positions.values() if p["status"] in ("ACTIVE", "open"))
    degraded = sum(1 for p in positions.values() if p["status"] == "DEGRADED")
    print(f"  ▸ Positions: {active} active, {degraded} degraded")

    # ── Step 5: Evaluate all symbols ──
    print(f"  ▸ Evaluating {len(symbols)} symbols...")
    tags = evaluate_all(symbols, positions, ema_data, config, state, cycle_number)

    # Print tag summary
    tag_counts: dict[str, int] = {}
    for tag in tags:
        tag_counts[tag.tag_type.value] = tag_counts.get(tag.tag_type.value, 0) + 1

    print(f"\n  Tags: {', '.join(f'{k}={v}' for k, v in sorted(tag_counts.items()))}")

    # Print actionable tags
    for tag in tags:
        if tag.tag_type not in (TagType.HOLD, TagType.NO_ENTRY):
            print(f"    {tag.tag_type.value:20s} {tag.symbol:6s} "
                  f"bndpy={tag.bndpy:.6f} — {tag.reason}")

    # Print entry qualifying progress
    for tag in tags:
        if tag.tag_type == TagType.NO_ENTRY and "qualifying" in tag.reason:
            print(f"    {'QUALIFYING':20s} {tag.symbol:6s} "
                  f"bndpy={tag.bndpy:.6f} — {tag.reason}")

    # ── Write engine_cycles (audit trail + dashboard data source) ──
    await write_engine_cycles(pool, tags)

    # ── Step 6: Compute sizes for entries/rotations ──
    size_map: dict[str, float] = {}
    entry_tags = [t for t in tags if t.tag_type == TagType.ENTRY]
    for tag in entry_tags:
        size = await compute_size(pool, tag.bndpy, config.endpy, nav,
                                  tag.short_venue, tag.long_venue)
        if size > 0:
            size_map[tag.symbol] = size
            print(f"    Entry size {tag.symbol}: ${size:.2f}")

    # ── Step 7: Execute ──
    actionable = [t for t in tags
                  if t.tag_type not in (TagType.HOLD, TagType.NO_ENTRY)]
    if actionable:
        print(f"\n  ▸ Executing {len(actionable)} actions...")
        await execute_tags(pool, tags, positions, state, cycle_number,
                           dry_run=dry_run, size_map=size_map)
    else:
        print(f"\n  ▸ No actions to execute")

    elapsed = time.monotonic() - t0
    print(f"\n  Cycle {cycle_number} complete in {elapsed:.1f}s")


async def main():
    parser = argparse.ArgumentParser(
        description="Hedgehog Engine v2 — cron-driven funding rate arbitrage",
    )
    parser.add_argument("--live", action="store_true",
                        help="Execute orders (default is dry-run)")
    parser.add_argument("--loop", action="store_true",
                        help="Run continuously every COLLECT_INTERVAL seconds")
    parser.add_argument("--interval", type=int, default=COLLECT_INTERVAL,
                        help=f"Loop interval in seconds (default: {COLLECT_INTERVAL})")
    args = parser.parse_args()

    dry_run = not args.live

    # Lockfile to prevent overlapping runs
    lock_fd = _acquire_lock()
    if lock_fd is None:
        print("  Another engine cycle is already running — skipping")
        sys.exit(0)

    symbols = load_symbols()
    config = EngineConfig()
    state = EngineState()

    db_url = get_database_url()
    pool = await asyncpg.create_pool(db_url, min_size=2, max_size=10)

    print(f"  Engine v2 started — {len(symbols)} symbols, "
          f"{'LIVE' if not dry_run else 'DRY RUN'}")
    print(f"  Thresholds: endpy={config.endpy} xndpy={config.xndpy} "
          f"mebh={config.mebh}h mrbh={config.mrbh}h")

    cycle_number = 0
    try:
        if args.loop:
            print(f"  Looping every {args.interval}s. Ctrl+C to stop.\n")
            while True:
                cycle_number += 1
                await run_cycle(pool, symbols, config, state, cycle_number, dry_run)
                await asyncio.sleep(args.interval)
        else:
            cycle_number = 1
            await run_cycle(pool, symbols, config, state, cycle_number, dry_run)
    except KeyboardInterrupt:
        print("\n  Stopped by user.")
    finally:
        await pool.close()
        _release_lock(lock_fd)


if __name__ == "__main__":
    asyncio.run(main())
