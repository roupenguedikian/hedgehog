"""
tests/exchange_verification/runner.py

CLI runner for exchange verification.

Usage:
  # Verify single venue (read-only)
  python -m tests.exchange_verification.runner --venue hyperliquid --tier 1

  # Verify single venue (full write test)
  python -m tests.exchange_verification.runner --venue hyperliquid --tier 3 --symbol BTC

  # Verify ALL venues (read-only sweep)
  python -m tests.exchange_verification.runner --all --tier 1

  # Verify all venues, save JSON report
  python -m tests.exchange_verification.runner --all --tier 2 --output report.json

  # Dry-run: show what would be tested
  python -m tests.exchange_verification.runner --venue hyperliquid --tier 3 --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import structlog

from tests.exchange_verification.harness import ExchangeVerifier, VenueVerificationReport
from tests.exchange_verification.venue_params import get_venue_params, get_min_size, VENUE_TEST_PARAMS

logger = structlog.get_logger()


ALL_VENUES = list(VENUE_TEST_PARAMS.keys())


def load_config(config_dir: str = "config") -> dict:
    """Load venue configs from YAML."""
    import yaml

    venues_path = os.path.join(config_dir, "venues.yaml")
    if not os.path.exists(venues_path):
        # Try from project root
        venues_path = os.path.join(PROJECT_ROOT, config_dir, "venues.yaml")

    with open(venues_path) as f:
        return yaml.safe_load(f)


def build_adapter(venue_name: str, venue_config: dict):
    """Instantiate the correct adapter class for a venue."""
    from models.core import VenueConfig, VenueTier, ChainType

    # Build VenueConfig from YAML dict
    vc = VenueConfig(
        name=venue_config.get("name", venue_name),
        chain=venue_config.get("chain", "unknown"),
        chain_type=ChainType(venue_config.get("chain_type", "evm")),
        settlement_chain=venue_config.get("settlement_chain", "unknown"),
        funding_cycle_hours=venue_config.get("funding_cycle_hours", 8),
        maker_fee_bps=venue_config.get("maker_fee_bps", 0),
        taker_fee_bps=venue_config.get("taker_fee_bps", 0),
        max_leverage=venue_config.get("max_leverage", 20),
        collateral_token=venue_config.get("collateral_token", "USDC"),
        api_base_url=venue_config.get("api_base_url", ""),
        ws_url=venue_config.get("ws_url", ""),
        deposit_chain=venue_config.get("deposit_chain", ""),
        tier=VenueTier(venue_config.get("tier", "tier_3")),
        zero_gas=venue_config.get("zero_gas", False),
        symbol_format=venue_config.get("symbol_format", "{symbol}"),
        symbol_overrides=venue_config.get("symbol_overrides", {}),
    )

    # Import the correct adapter class
    from adapters.hyperliquid_adapter import HyperliquidAdapter
    from adapters.generic_rest_adapter import (
        GenericRestAdapter, AsterAdapter, LighterAdapter,
        EtherealAdapter, ApexAdapter,
    )

    ADAPTER_MAP = {
        "hyperliquid": HyperliquidAdapter,
        "lighter": LighterAdapter,
        "aster": AsterAdapter,
        "drift": GenericRestAdapter,
        "dydx": GenericRestAdapter,
        "apex": ApexAdapter,
        "paradex": GenericRestAdapter,
        "ethereal": EtherealAdapter,
        "injective": GenericRestAdapter,
    }

    cls = ADAPTER_MAP.get(venue_name.lower(), GenericRestAdapter)
    return cls(vc)


async def verify_venue(
    venue_name: str,
    venue_yaml: dict,
    tier: int,
    symbol: str,
    verbose: bool = False,
) -> VenueVerificationReport:
    """Run full verification for a single venue."""

    print(f"\n{'─'*50}")
    print(f"  Verifying: {venue_name.upper()} (tier {tier})")
    print(f"{'─'*50}")

    adapter = build_adapter(venue_name, venue_yaml)
    params = get_venue_params(venue_name)
    min_size = get_min_size(venue_name, symbol)

    # Connect
    pk = os.getenv(f"{venue_name.upper()}_PRIVATE_KEY", os.getenv("EVM_PRIVATE_KEY", ""))

    if tier >= 2 and not pk:
        print(f"  ⚠️  No private key for {venue_name} — will stop at tier 1")
        tier = 1

    try:
        await adapter.connect(pk)
    except Exception as e:
        print(f"  ❌ Connection failed: {e}")
        report = VenueVerificationReport(venue=venue_name, tier_run=tier)
        from tests.exchange_verification.harness import CheckResult, CheckStatus
        report.results.append(CheckResult(
            name="connection", tier=0, status=CheckStatus.FAIL,
            message=f"Connection failed: {e}",
        ))
        return report

    verifier = ExchangeVerifier(adapter, venue_yaml)
    report = await verifier.run(
        tier=tier,
        symbol=symbol,
        min_order_size=min_size,
        price_offset_pct=params["price_offset_pct"],
    )

    # Disconnect
    try:
        await adapter.disconnect()
    except Exception:
        pass

    # Print report
    print(report.summary())

    if verbose:
        for r in report.results:
            if r.error:
                print(f"\n  --- Error detail for {r.name} ---")
                print(f"  {r.error}")

    return report


async def verify_all(
    config: dict,
    tier: int,
    symbol: str,
    venues_filter: list[str] | None = None,
    verbose: bool = False,
) -> list[VenueVerificationReport]:
    """Verify multiple venues sequentially."""

    venues = config.get("venues", {})
    target_venues = venues_filter or list(venues.keys())
    reports = []

    for vname in target_venues:
        if vname not in venues:
            print(f"  ⚠️  Venue '{vname}' not in config — skipping")
            continue

        report = await verify_venue(vname, venues[vname], tier, symbol, verbose)
        reports.append(report)

    # Final summary
    print(f"\n{'═'*70}")
    print(f"  AGGREGATE RESULTS ({len(reports)} venues)")
    print(f"{'═'*70}")

    total_pass = sum(r.passed for r in reports)
    total_fail = sum(r.failed for r in reports)
    total_warn = sum(r.warnings for r in reports)

    for r in reports:
        icon = "🟢" if r.all_passed else "🔴"
        print(f"  {icon} {r.venue:<20} ✅ {r.passed} pass | ❌ {r.failed} fail | ⚠️  {r.warnings} warn")

    print(f"\n  TOTAL: ✅ {total_pass} | ❌ {total_fail} | ⚠️  {total_warn}")

    if total_fail == 0:
        print(f"\n  🟢 ALL VENUES CLEAR — DEPLOYMENT APPROVED")
    else:
        print(f"\n  🔴 {total_fail} FAILURES — FIX BEFORE DEPLOYING")

    print(f"{'═'*70}\n")
    return reports


def export_report(reports: list[VenueVerificationReport], output_path: str):
    """Export reports as JSON."""
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "venues": {},
    }

    for r in reports:
        data["venues"][r.venue] = {
            "tier": r.tier_run,
            "passed": r.passed,
            "failed": r.failed,
            "warnings": r.warnings,
            "skipped": r.skipped,
            "all_passed": r.all_passed,
            "results": [
                {
                    "name": cr.name,
                    "tier": cr.tier,
                    "status": cr.status.value,
                    "latency_ms": cr.latency_ms,
                    "message": cr.message,
                }
                for cr in r.results
            ],
            "cleanup_log": r.cleanup_log,
        }

    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"  📄 Report saved to {output_path}")


def show_dry_run(venue: str, tier: int, symbol: str):
    """Print what would be tested without running anything."""
    params = get_venue_params(venue)

    print(f"\n  DRY RUN: {venue.upper()}")
    print(f"  Symbol: {symbol} | Min size: {get_min_size(venue, symbol)}")
    print(f"  Notes: {params['notes']}")
    print()

    checks_t1 = [
        "connection", "funding_rate", "funding_history",
        "orderbook", "orderbook_depth", "mark_price",
        "symbol_normalize", "gas_estimate", "fee_roundtrip",
    ]
    checks_t2 = [
        "balance", "balance_fields", "positions",
        "position_integrity", "deposit_info",
    ]
    checks_t3 = [
        "place_limit_buy", "verify_open_order", "cancel_order",
        "verify_cancelled", "place_limit_sell", "cancel_all",
        "verify_all_cancelled", "market_order_buy",
        "verify_position_after_fill", "close_position",
        "verify_position_closed",
    ]

    for t, checks in [(1, checks_t1), (2, checks_t2), (3, checks_t3)]:
        if t > tier:
            break
        print(f"  Tier {t}:")
        for c in checks:
            print(f"    {'✓' if t <= tier else '·'} {c}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Exchange Verification Suite for Hedgehog",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --venue hyperliquid --tier 1        Read-only check
  %(prog)s --venue hyperliquid --tier 3        Full write test
  %(prog)s --all --tier 1                      Sweep all venues
  %(prog)s --all --tier 2 --output report.json Save report
  %(prog)s --venue drift --tier 3 --dry-run    Preview checks
        """,
    )

    parser.add_argument("--venue", type=str, help="Single venue to verify")
    parser.add_argument("--all", action="store_true", help="Verify all venues")
    parser.add_argument("--tier", type=int, default=1, choices=[1, 2, 3],
                        help="Max verification tier (1=read, 2=auth, 3=write)")
    parser.add_argument("--symbol", type=str, default="ETH",
                        help="Symbol to test (default: ETH — cheaper for tier 3)")
    parser.add_argument("--output", type=str, help="Save JSON report to file")
    parser.add_argument("--verbose", action="store_true", help="Show full error traces")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be tested")
    parser.add_argument("--config-dir", default="config", help="Config directory path")

    args = parser.parse_args()

    if not args.venue and not args.all:
        parser.error("Specify --venue NAME or --all")

    if args.dry_run:
        venues = [args.venue] if args.venue else ALL_VENUES
        for v in venues:
            show_dry_run(v, args.tier, args.symbol)
        return

    # Safety gate for tier 3
    if args.tier == 3:
        print("\n  ⚠️  TIER 3 VERIFICATION PLACES REAL ORDERS ON MAINNET")
        print(f"  Symbol: {args.symbol} | Min size: {get_min_size(args.venue or 'hyperliquid', args.symbol)}")
        print("  Orders use prices 5%+ from market to avoid fills,")
        print("  but market orders WILL fill with real funds.\n")
        confirm = input("  Type 'YES' to proceed: ").strip()
        if confirm != "YES":
            print("  Aborted.")
            return

    config = load_config(args.config_dir)

    if args.venue:
        reports = asyncio.run(
            verify_all(config, args.tier, args.symbol, [args.venue], args.verbose)
        )
    else:
        reports = asyncio.run(
            verify_all(config, args.tier, args.symbol, verbose=args.verbose)
        )

    if args.output:
        export_report(reports, args.output)

    # Exit code: 1 if any failures
    total_fail = sum(r.failed for r in reports)
    sys.exit(1 if total_fail > 0 else 0)


if __name__ == "__main__":
    main()
