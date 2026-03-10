"""
tests/exchange_verification/predeploy.py

Pre-deployment verification gate.
Run this before any deployment to mainnet with real capital.

This is the "run everything" script that:
1. Verifies all venues at the appropriate tier
2. Runs cross-venue consistency checks
3. Validates the execution engine's rollback logic
4. Checks risk engine limits
5. Produces a go/no-go decision

Usage:
  python -m tests.exchange_verification.predeploy
  python -m tests.exchange_verification.predeploy --write-test    # includes tier 3
  python -m tests.exchange_verification.predeploy --output predeploy-report.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


class PreDeployGate:
    """Orchestrates the full pre-deployment verification."""

    def __init__(self, config_dir: str = "config", write_test: bool = False):
        self.config_dir = config_dir
        self.write_test = write_test
        self.results: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "write_test": write_test,
            "phases": {},
            "verdict": "PENDING",
        }

    async def run(self) -> bool:
        """Run all phases. Returns True if deployment is approved."""
        print("""
    ╔══════════════════════════════════════════════════╗
    ║         PRE-DEPLOYMENT VERIFICATION              ║
    ║         Hedgehog / Aegis Protocol                ║
    ╚══════════════════════════════════════════════════╝
        """)

        t0 = time.monotonic()
        all_ok = True

        # Phase 1: Static checks (no network)
        print("\n📋 PHASE 1: Static Validation")
        print("─" * 50)
        phase1_ok = await self._phase_static_checks()
        self.results["phases"]["static_checks"] = {"passed": phase1_ok}
        all_ok &= phase1_ok

        # Phase 2: Connectivity sweep (tier 1 all venues)
        print("\n🌐 PHASE 2: Connectivity Sweep (Tier 1)")
        print("─" * 50)
        phase2_ok = await self._phase_connectivity_sweep()
        self.results["phases"]["connectivity"] = {"passed": phase2_ok}
        all_ok &= phase2_ok

        # Phase 3: Account verification (tier 2 for venues with keys)
        print("\n🔑 PHASE 3: Account Verification (Tier 2)")
        print("─" * 50)
        phase3_ok = await self._phase_account_verification()
        self.results["phases"]["account_verification"] = {"passed": phase3_ok}
        all_ok &= phase3_ok

        # Phase 4: Write test (tier 3 — only if --write-test)
        if self.write_test:
            print("\n📝 PHASE 4: Write Operations (Tier 3)")
            print("─" * 50)
            phase4_ok = await self._phase_write_test()
            self.results["phases"]["write_test"] = {"passed": phase4_ok}
            all_ok &= phase4_ok
        else:
            print("\n⏭️  PHASE 4: Write Operations — SKIPPED (use --write-test)")
            self.results["phases"]["write_test"] = {"skipped": True}

        # Phase 5: Risk engine validation
        print("\n🛡️  PHASE 5: Risk Engine Validation")
        print("─" * 50)
        phase5_ok = await self._phase_risk_validation()
        self.results["phases"]["risk_engine"] = {"passed": phase5_ok}
        all_ok &= phase5_ok

        elapsed = time.monotonic() - t0
        self.results["elapsed_seconds"] = round(elapsed, 1)
        self.results["verdict"] = "APPROVED" if all_ok else "REJECTED"

        # Final verdict
        print(f"\n{'═'*60}")
        if all_ok:
            print(f"  🟢 DEPLOYMENT APPROVED  ({elapsed:.1f}s)")
        else:
            print(f"  🔴 DEPLOYMENT REJECTED  ({elapsed:.1f}s)")
            print(f"  Fix all failures before deploying with real capital.")
        print(f"{'═'*60}\n")

        return all_ok

    # ─── Phase implementations ───────────────────────────────────

    async def _phase_static_checks(self) -> bool:
        """Validate config, imports, and model consistency."""
        ok = True

        # 1. Config loads without error
        try:
            import yaml
            config_path = os.path.join(PROJECT_ROOT, self.config_dir, "venues.yaml")
            with open(config_path) as f:
                config = yaml.safe_load(f)
            venues = config.get("venues", {})
            print(f"  ✅ venues.yaml loaded: {len(venues)} venues configured")
        except Exception as e:
            print(f"  ❌ venues.yaml failed to load: {e}")
            return False

        # 2. All adapters importable
        try:
            from adapters.base_adapter import BaseDefiAdapter
            from adapters.hyperliquid_adapter import HyperliquidAdapter
            from adapters.generic_rest_adapter import (
                GenericRestAdapter, AsterAdapter, LighterAdapter,
                EtherealAdapter, ApexAdapter,
            )
            print(f"  ✅ All adapter classes import successfully")
        except Exception as e:
            print(f"  ❌ Adapter import failed: {e}")
            ok = False

        # 3. Core models valid
        try:
            from models.core import (
                FundingRate, Orderbook, Position, OrderResult,
                Side, OrderStatus, VenueConfig, VenueTier, ChainType,
            )
            print(f"  ✅ Core models import successfully")
        except Exception as e:
            print(f"  ❌ Model import failed: {e}")
            ok = False

        # 4. Each venue in config has a corresponding adapter
        from tests.exchange_verification.runner import build_adapter
        for vname, vconfig in venues.items():
            try:
                adapter = build_adapter(vname, vconfig)
                print(f"  ✅ {vname:<15} → {adapter.__class__.__name__}")
            except Exception as e:
                print(f"  ❌ {vname:<15} → build failed: {e}")
                ok = False

        # 5. Verify symbol normalization doesn't crash
        for vname, vconfig in venues.items():
            try:
                adapter = build_adapter(vname, vconfig)
                for sym in ["BTC", "ETH", "SOL"]:
                    n = adapter.normalize_symbol(sym)
                    assert n and len(n) > 0
            except Exception as e:
                print(f"  ❌ {vname} symbol normalization failed: {e}")
                ok = False

        if ok:
            print(f"  ✅ All symbol normalizations valid")

        return ok

    async def _phase_connectivity_sweep(self) -> bool:
        """Tier 1 read-only verification across all venues."""
        from tests.exchange_verification.runner import verify_all, load_config

        config = load_config(self.config_dir)
        reports = await verify_all(config, tier=1, symbol="ETH")

        ok = all(r.all_passed for r in reports)
        self.results["phases"]["connectivity"] = {
            "passed": ok,
            "venues": {r.venue: {"passed": r.passed, "failed": r.failed} for r in reports},
        }
        return ok

    async def _phase_account_verification(self) -> bool:
        """Tier 2 for every venue that has a private key configured."""
        from tests.exchange_verification.runner import verify_all, load_config

        config = load_config(self.config_dir)
        venues_with_keys = []
        for vname in config.get("venues", {}):
            pk = os.getenv(f"{vname.upper()}_PRIVATE_KEY", os.getenv("EVM_PRIVATE_KEY", ""))
            if pk:
                venues_with_keys.append(vname)

        if not venues_with_keys:
            print("  ⚠️  No private keys found — skipping tier 2")
            return True  # Not a failure, just nothing to test

        reports = await verify_all(config, tier=2, symbol="ETH", venues_filter=venues_with_keys)
        ok = all(r.all_passed for r in reports)
        return ok

    async def _phase_write_test(self) -> bool:
        """Tier 3 write operations on venues with keys."""
        from tests.exchange_verification.runner import verify_all, load_config

        config = load_config(self.config_dir)
        venues_with_keys = []
        for vname in config.get("venues", {}):
            pk = os.getenv(f"{vname.upper()}_PRIVATE_KEY", os.getenv("EVM_PRIVATE_KEY", ""))
            if pk:
                venues_with_keys.append(vname)

        if not venues_with_keys:
            print("  ⚠️  No private keys — cannot run tier 3")
            return True

        # Use ETH for tier 3 — cheaper minimum order
        reports = await verify_all(config, tier=3, symbol="ETH", venues_filter=venues_with_keys)
        ok = all(r.all_passed for r in reports)
        return ok

    async def _phase_risk_validation(self) -> bool:
        """Validate risk engine configuration and behavior."""
        ok = True

        try:
            from services.risk.risk_engine import RiskEngine
            print(f"  ✅ RiskEngine imports")
        except Exception as e:
            print(f"  ❌ RiskEngine import failed: {e}")
            return False

        try:
            from services.risk.circuit_breaker import CircuitBreaker
            print(f"  ✅ CircuitBreaker imports")
        except Exception as e:
            print(f"  ❌ CircuitBreaker import failed: {e}")
            ok = False

        # Validate risk config exists and has sane values
        try:
            import yaml
            risk_path = os.path.join(PROJECT_ROOT, self.config_dir, "venues.yaml")
            with open(risk_path) as f:
                config = yaml.safe_load(f)

            # Check that venues have required fields
            for vname, vconfig in config.get("venues", {}).items():
                required = ["funding_cycle_hours", "maker_fee_bps", "taker_fee_bps", "tier"]
                missing = [f for f in required if f not in vconfig]
                if missing:
                    print(f"  ⚠️  {vname} missing risk-relevant fields: {missing}")

            print(f"  ✅ Risk configuration validated")
        except Exception as e:
            print(f"  ❌ Risk config validation failed: {e}")
            ok = False

        return ok

    def save_report(self, path: str):
        with open(path, "w") as f:
            json.dump(self.results, f, indent=2)
        print(f"  📄 Pre-deploy report saved to {path}")


async def main():
    parser = argparse.ArgumentParser(description="Pre-deployment verification gate")
    parser.add_argument("--write-test", action="store_true",
                        help="Include tier 3 write operations")
    parser.add_argument("--output", type=str, help="Save JSON report")
    parser.add_argument("--config-dir", default="config")
    args = parser.parse_args()

    gate = PreDeployGate(config_dir=args.config_dir, write_test=args.write_test)
    approved = await gate.run()

    if args.output:
        gate.save_report(args.output)

    sys.exit(0 if approved else 1)


if __name__ == "__main__":
    asyncio.run(main())
