#!/usr/bin/env python3
"""
Hedgehog full portfolio query — runs all 10 venue queries in parallel.
Usage: python3 connectors/hedgehog_query.py [subcommand]
       subcommand is passed through to each venue connector (default: all)
"""
import asyncio
import os
import subprocess
import sys

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(SCRIPTS_DIR))
PYTHON = os.path.join(PROJECT_ROOT, ".venv", "bin", "python")
if not os.path.exists(PYTHON):
    PYTHON = sys.executable

VENUES = [
    ("Hyperliquid", "hl_query.py"),
    ("Aster", "aster_query.py"),
    ("Lighter", "lighter_query.py"),
    ("Apex Omni", "apex_query.py"),
    ("dYdX v4", "dydx_query.py"),
    ("Drift", "drift_query.py"),
    ("EdgeX", "edgex_query.py"),
    ("Paradex", "paradex_query.py"),
    ("Ethereal", "ethereal_query.py"),
    ("trade.xyz", "tradexyz_query.py"),
]


async def run_venue(name: str, script: str, sub_args: list[str]) -> tuple[str, str, int]:
    """Run a venue query script and capture its output."""
    script_path = os.path.join(SCRIPTS_DIR, script)
    if not os.path.exists(script_path):
        return name, f"  [SKIP] {script} not found", 1

    cmd = [PYTHON, script_path] + sub_args
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=PROJECT_ROOT,
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode()
    if proc.returncode != 0 and stderr:
        output += f"\n  [STDERR] {stderr.decode()[:200]}"
    return name, output, proc.returncode


async def main():
    sub_args = sys.argv[1:] if len(sys.argv) > 1 else []

    print("=" * 80)
    print("  HEDGEHOG — Full Portfolio Query (all 10 venues)")
    print("=" * 80)
    print(f"  Running {len(VENUES)} venue queries in parallel...\n")

    tasks = [run_venue(name, script, sub_args) for name, script in VENUES]
    results = await asyncio.gather(*tasks)

    for name, output, rc in results:
        status = "OK" if rc == 0 else f"ERR({rc})"
        print(f"\n{'─' * 80}")
        print(f"  [{status}] {name}")
        print(f"{'─' * 80}")
        print(output)

    # Print summary footer
    ok = sum(1 for _, _, rc in results if rc == 0)
    fail = len(results) - ok
    print(f"\n{'═' * 80}")
    print(f"  QUERY COMPLETE: {ok}/{len(results)} venues succeeded", end="")
    if fail:
        failed = [name for name, _, rc in results if rc != 0]
        print(f" (failed: {', '.join(failed)})", end="")
    print()
    print(f"{'═' * 80}")


if __name__ == "__main__":
    asyncio.run(main())
