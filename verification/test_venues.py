"""
tests/exchange_verification/test_venues.py

Pytest-compatible exchange verification.
Run with: pytest tests/exchange_verification/test_venues.py -v

Environment variables control behavior:
  VERIFY_VENUES=hyperliquid,drift     # which venues to test (default: all)
  VERIFY_TIER=1                       # max tier (default: 1 for CI safety)
  VERIFY_SYMBOL=ETH                   # test symbol (default: ETH)

Tier 3 tests are NEVER auto-run in CI — they require VERIFY_TIER=3 explicitly.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Add project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def get_test_config():
    """Read test configuration from environment."""
    venues_str = os.getenv("VERIFY_VENUES", "")
    tier = int(os.getenv("VERIFY_TIER", "1"))
    symbol = os.getenv("VERIFY_SYMBOL", "ETH")

    if venues_str:
        venues = [v.strip() for v in venues_str.split(",")]
    else:
        venues = [
            "hyperliquid", "lighter", "aster", "drift", "dydx",
            "apex", "paradex", "ethereal", "injective",
        ]

    return venues, tier, symbol


def load_yaml_config():
    """Load venues.yaml."""
    import yaml
    for path in ["config/venues.yaml", str(PROJECT_ROOT / "config" / "venues.yaml")]:
        if os.path.exists(path):
            with open(path) as f:
                return yaml.safe_load(f)
    pytest.skip("config/venues.yaml not found")


VENUES_TO_TEST, MAX_TIER, TEST_SYMBOL = get_test_config()


# ═══════════════════════════════════════════════════════════════════
# Parametrized tests — one per venue
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def yaml_config():
    return load_yaml_config()


@pytest.mark.parametrize("venue_name", VENUES_TO_TEST)
class TestVenueVerification:
    """
    Parametrized test class — runs the verification harness per venue.
    Each venue gets its own test methods for clear reporting.
    """

    @pytest.mark.asyncio
    async def test_tier1_readonly(self, venue_name, yaml_config):
        """Tier 1: Public endpoints — always safe to run."""
        venue_yaml = yaml_config.get("venues", {}).get(venue_name)
        if not venue_yaml:
            pytest.skip(f"Venue {venue_name} not in config")

        from tests.exchange_verification.runner import build_adapter
        from tests.exchange_verification.harness import ExchangeVerifier
        from tests.exchange_verification.venue_params import get_min_size

        adapter = build_adapter(venue_name, venue_yaml)

        try:
            await adapter.connect("")
        except Exception as e:
            pytest.fail(f"Connection failed: {e}")

        verifier = ExchangeVerifier(adapter, venue_yaml)
        report = await verifier.run(tier=1, symbol=TEST_SYMBOL)

        await adapter.disconnect()

        print(report.summary())
        assert report.failed == 0, (
            f"{venue_name} tier 1 had {report.failed} failure(s): "
            + "; ".join(r.message for r in report.results if r.status.value == "FAIL")
        )

    @pytest.mark.asyncio
    async def test_tier2_authenticated(self, venue_name, yaml_config):
        """Tier 2: Balance + positions. Requires credentials."""
        if MAX_TIER < 2:
            pytest.skip("VERIFY_TIER < 2")

        venue_yaml = yaml_config.get("venues", {}).get(venue_name)
        if not venue_yaml:
            pytest.skip(f"Venue {venue_name} not in config")

        pk = os.getenv(f"{venue_name.upper()}_PRIVATE_KEY", os.getenv("EVM_PRIVATE_KEY", ""))
        if not pk:
            pytest.skip(f"No private key for {venue_name}")

        from tests.exchange_verification.runner import build_adapter
        from tests.exchange_verification.harness import ExchangeVerifier

        adapter = build_adapter(venue_name, venue_yaml)
        await adapter.connect(pk)

        verifier = ExchangeVerifier(adapter, venue_yaml)
        report = await verifier.run(tier=2, symbol=TEST_SYMBOL)

        await adapter.disconnect()

        print(report.summary())
        assert report.failed == 0, (
            f"{venue_name} tier 2 had {report.failed} failure(s): "
            + "; ".join(r.message for r in report.results if r.status.value == "FAIL")
        )

    @pytest.mark.asyncio
    async def test_tier3_write_operations(self, venue_name, yaml_config):
        """
        Tier 3: Real orders on mainnet.
        ONLY runs when VERIFY_TIER=3 is explicitly set.
        """
        if MAX_TIER < 3:
            pytest.skip("VERIFY_TIER < 3 (tier 3 requires explicit opt-in)")

        venue_yaml = yaml_config.get("venues", {}).get(venue_name)
        if not venue_yaml:
            pytest.skip(f"Venue {venue_name} not in config")

        pk = os.getenv(f"{venue_name.upper()}_PRIVATE_KEY", os.getenv("EVM_PRIVATE_KEY", ""))
        if not pk:
            pytest.skip(f"No private key for {venue_name}")

        from tests.exchange_verification.runner import build_adapter
        from tests.exchange_verification.harness import ExchangeVerifier
        from tests.exchange_verification.venue_params import get_min_size

        adapter = build_adapter(venue_name, venue_yaml)
        await adapter.connect(pk)

        min_size = get_min_size(venue_name, TEST_SYMBOL)
        verifier = ExchangeVerifier(adapter, venue_yaml)
        report = await verifier.run(
            tier=3, symbol=TEST_SYMBOL, min_order_size=min_size,
        )

        await adapter.disconnect()

        print(report.summary())
        assert report.failed == 0, (
            f"{venue_name} tier 3 had {report.failed} failure(s): "
            + "; ".join(r.message for r in report.results if r.status.value == "FAIL")
        )


# ═══════════════════════════════════════════════════════════════════
# Standalone cross-venue consistency checks
# ═══════════════════════════════════════════════════════════════════

class TestCrossVenueConsistency:
    """
    Tests that run across all venues to check for consistency
    in data formatting, symbol handling, etc.
    """

    @pytest.mark.asyncio
    async def test_symbol_normalization_all_venues(self, yaml_config):
        """All adapters should normalize symbols without crashing."""
        from tests.exchange_verification.runner import build_adapter

        venues = yaml_config.get("venues", {})
        for vname, vconfig in venues.items():
            if vname not in VENUES_TO_TEST:
                continue

            adapter = build_adapter(vname, vconfig)
            for sym in ["BTC", "ETH", "SOL"]:
                normalized = adapter.normalize_symbol(sym)
                assert normalized, f"{vname}: normalize_symbol('{sym}') returned empty"
                assert isinstance(normalized, str), f"{vname}: expected str, got {type(normalized)}"

    @pytest.mark.asyncio
    async def test_fee_calculations_all_venues(self, yaml_config):
        """All adapters should return valid fee calculations."""
        from tests.exchange_verification.runner import build_adapter

        venues = yaml_config.get("venues", {})
        for vname, vconfig in venues.items():
            if vname not in VENUES_TO_TEST:
                continue

            adapter = build_adapter(vname, vconfig)
            fee = adapter.round_trip_fee_bps()
            assert fee >= 0, f"{vname}: negative round-trip fee: {fee}"
            assert fee < 100, f"{vname}: fee {fee} bps seems too high"

    @pytest.mark.asyncio
    async def test_gas_estimates_all_venues(self, yaml_config):
        """All adapters should return valid gas estimates."""
        from tests.exchange_verification.runner import build_adapter

        venues = yaml_config.get("venues", {})
        for vname, vconfig in venues.items():
            if vname not in VENUES_TO_TEST:
                continue

            adapter = build_adapter(vname, vconfig)
            gas = adapter.estimate_gas_cost("trade")
            assert gas >= 0, f"{vname}: negative gas: {gas}"
            if vconfig.get("zero_gas"):
                assert gas == 0, f"{vname}: claims zero_gas but estimate is {gas}"
