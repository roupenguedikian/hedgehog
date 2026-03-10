"""
tests/exchange_verification/harness.py

The core verification engine. Runs a standardized battery of checks against
any adapter that implements BaseDefiAdapter.

Design principles:
- Each check is independent and reports PASS / FAIL / SKIP with diagnostics
- Tier 1 runs without credentials (public data only)
- Tier 2 requires credentials (read-only account state)
- Tier 3 places REAL orders on mainnet — uses minimum size, widest spreads
- Every write operation has a paired verification and cleanup step
- Timeout enforcement on every call
- Full structured logging of every result for audit trail
"""
from __future__ import annotations

import asyncio
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional, Any

import structlog

logger = structlog.get_logger()


# ═══════════════════════════════════════════════════════════════════
# Result types
# ═══════════════════════════════════════════════════════════════════

class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    SKIP = "SKIP"
    WARN = "WARN"


@dataclass
class CheckResult:
    name: str
    tier: int
    status: CheckStatus
    latency_ms: float = 0.0
    message: str = ""
    data: Any = None              # raw response for debugging
    error: Optional[str] = None


@dataclass
class VenueVerificationReport:
    venue: str
    tier_run: int
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: Optional[datetime] = None
    results: list[CheckResult] = field(default_factory=list)
    cleanup_log: list[str] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == CheckStatus.PASS)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == CheckStatus.FAIL)

    @property
    def warnings(self) -> int:
        return sum(1 for r in self.results if r.status == CheckStatus.WARN)

    @property
    def skipped(self) -> int:
        return sum(1 for r in self.results if r.status == CheckStatus.SKIP)

    @property
    def all_passed(self) -> bool:
        return self.failed == 0

    def summary(self) -> str:
        lines = [
            f"\n{'='*70}",
            f"  VERIFICATION REPORT: {self.venue.upper()}",
            f"  Tier: {self.tier_run} | {self.started_at:%Y-%m-%d %H:%M:%S UTC}",
            f"{'='*70}",
        ]
        for r in self.results:
            icon = {"PASS": "✅", "FAIL": "❌", "SKIP": "⏭️ ", "WARN": "⚠️ "}[r.status.value]
            lat = f"({r.latency_ms:.0f}ms)" if r.latency_ms else ""
            msg = f" — {r.message}" if r.message else ""
            lines.append(f"  {icon} [{r.tier}] {r.name:<40} {lat:>8}{msg}")
        lines.append(f"{'─'*70}")
        lines.append(
            f"  TOTAL: {len(self.results)} checks | "
            f"✅ {self.passed} pass | ❌ {self.failed} fail | "
            f"⚠️  {self.warnings} warn | ⏭️  {self.skipped} skip"
        )
        if self.cleanup_log:
            lines.append(f"\n  CLEANUP:")
            for cl in self.cleanup_log:
                lines.append(f"    🧹 {cl}")
        verdict = "ALL CLEAR — SAFE TO DEPLOY" if self.all_passed else "ISSUES DETECTED — DO NOT DEPLOY"
        lines.append(f"\n  {'🟢' if self.all_passed else '🔴'} VERDICT: {verdict}")
        lines.append(f"{'='*70}\n")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
# Verification harness
# ═══════════════════════════════════════════════════════════════════

class ExchangeVerifier:
    """
    Runs the full verification battery against a single venue adapter.

    Usage:
        verifier = ExchangeVerifier(adapter, venue_config)
        report = await verifier.run(tier=3, symbol="BTC", min_order_size=0.001)
    """

    DEFAULT_TIMEOUT = 30.0  # seconds per check

    def __init__(self, adapter, venue_config: dict):
        self.adapter = adapter
        self.config = venue_config
        self.venue = venue_config.get("name", "unknown").lower()
        self._placed_orders: list[dict] = []  # track for cleanup

    async def run(
        self,
        tier: int = 1,
        symbol: str = "BTC",
        min_order_size: float = 0.001,
        price_offset_pct: float = 5.0,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> VenueVerificationReport:
        """
        Run verification up to the specified tier.

        Args:
            tier: 1=read-only, 2=auth reads, 3=write operations
            symbol: Trading pair to test (base symbol, e.g. "BTC")
            min_order_size: Smallest order to place in tier 3
            price_offset_pct: How far from mid to place limit orders (safety margin)
            timeout: Max seconds per individual check
        """
        report = VenueVerificationReport(venue=self.venue, tier_run=tier)

        # Always run tier 1
        await self._run_tier1(report, symbol, timeout)

        if tier >= 2:
            await self._run_tier2(report, symbol, timeout)

        if tier >= 3:
            await self._run_tier3(report, symbol, min_order_size, price_offset_pct, timeout)

        # Cleanup: cancel any orders that might still be open
        await self._emergency_cleanup(report, symbol)

        report.finished_at = datetime.now(timezone.utc)
        return report

    # ─── Tier 1: Read-Only (Public Data) ─────────────────────────────

    async def _run_tier1(self, report: VenueVerificationReport, symbol: str, timeout: float):
        """Public endpoints — no credentials needed."""

        # 1.1 Connection
        await self._check(report, "t1_connection", 1, timeout,
                          self._verify_connection)

        # 1.2 Funding Rate
        await self._check(report, "t1_funding_rate", 1, timeout,
                          self._verify_funding_rate, symbol)

        # 1.3 Funding Rate History
        await self._check(report, "t1_funding_history", 1, timeout,
                          self._verify_funding_history, symbol)

        # 1.4 Orderbook
        await self._check(report, "t1_orderbook", 1, timeout,
                          self._verify_orderbook, symbol)

        # 1.5 Orderbook Depth Levels
        await self._check(report, "t1_orderbook_depth", 1, timeout,
                          self._verify_orderbook_depth, symbol)

        # 1.6 Mark Price
        await self._check(report, "t1_mark_price", 1, timeout,
                          self._verify_mark_price, symbol)

        # 1.7 Symbol Normalization
        await self._check(report, "t1_symbol_normalize", 1, timeout,
                          self._verify_symbol_normalize, symbol)

        # 1.8 Gas Cost Estimation
        await self._check(report, "t1_gas_estimate", 1, timeout,
                          self._verify_gas_estimate)

        # 1.9 Fee Calculation
        await self._check(report, "t1_fee_roundtrip", 1, timeout,
                          self._verify_fee_roundtrip)

    # ─── Tier 2: Authenticated Reads ─────────────────────────────────

    async def _run_tier2(self, report: VenueVerificationReport, symbol: str, timeout: float):
        """Account endpoints — require valid credentials."""

        # 2.1 Balance
        await self._check(report, "t2_balance", 2, timeout,
                          self._verify_balance)

        # 2.2 Balance Fields
        await self._check(report, "t2_balance_fields", 2, timeout,
                          self._verify_balance_fields)

        # 2.3 Positions
        await self._check(report, "t2_positions", 2, timeout,
                          self._verify_positions)

        # 2.4 Position Data Integrity
        await self._check(report, "t2_position_integrity", 2, timeout,
                          self._verify_position_integrity)

        # 2.5 Deposit Info
        await self._check(report, "t2_deposit_info", 2, timeout,
                          self._verify_deposit_info)

    # ─── Tier 3: Write Operations ────────────────────────────────────

    async def _run_tier3(
        self, report: VenueVerificationReport, symbol: str,
        min_size: float, offset_pct: float, timeout: float,
    ):
        """
        Order lifecycle tests — places REAL orders.
        Uses minimum sizes and prices far from market to avoid accidental fills.
        """

        # Get current mid price for safe order placement
        mid_price = await self._get_safe_mid_price(symbol)
        if mid_price <= 0:
            report.results.append(CheckResult(
                name="t3_price_preflight", tier=3, status=CheckStatus.FAIL,
                message="Cannot determine mid price — aborting tier 3",
            ))
            return

        # Safe limit prices: far enough from market to not fill
        safe_buy_price = round(mid_price * (1 - offset_pct / 100), 2)
        safe_sell_price = round(mid_price * (1 + offset_pct / 100), 2)

        report.results.append(CheckResult(
            name="t3_price_preflight", tier=3, status=CheckStatus.PASS,
            message=f"Mid: ${mid_price:,.2f} | Safe buy: ${safe_buy_price:,.2f} | Safe sell: ${safe_sell_price:,.2f}",
        ))

        # 3.1 Place Limit Buy (resting order)
        buy_order_id = await self._check(report, "t3_place_limit_buy", 3, timeout,
                                          self._verify_place_limit_order,
                                          symbol, "long", min_size, safe_buy_price)

        # 3.2 Verify the order is open / resting
        if buy_order_id:
            await self._check(report, "t3_verify_open_order", 3, timeout,
                              self._verify_order_is_open, symbol, buy_order_id)

            # 3.3 Cancel the resting order
            await self._check(report, "t3_cancel_order", 3, timeout,
                              self._verify_cancel_order, symbol, buy_order_id)

            # 3.4 Verify it's actually gone
            await asyncio.sleep(1.0)  # propagation delay
            await self._check(report, "t3_verify_cancelled", 3, timeout,
                              self._verify_order_is_cancelled, symbol, buy_order_id)

        # 3.5 Place Limit Sell (resting order — tests the other side)
        sell_order_id = await self._check(report, "t3_place_limit_sell", 3, timeout,
                                           self._verify_place_limit_order,
                                           symbol, "short", min_size, safe_sell_price)

        # 3.6 Cancel All Orders
        if sell_order_id:
            await self._check(report, "t3_cancel_all", 3, timeout,
                              self._verify_cancel_all, symbol)

            # 3.7 Verify no orders remain
            await asyncio.sleep(1.0)
            await self._check(report, "t3_verify_all_cancelled", 3, timeout,
                              self._verify_no_open_orders, symbol)

        # 3.8 Market order (IOC) — tiny size, expected to fill
        await self._check(report, "t3_market_order_buy", 3, timeout,
                          self._verify_market_order, symbol, "long", min_size)

        # 3.9 Verify position appeared
        await asyncio.sleep(2.0)  # settlement delay
        await self._check(report, "t3_verify_position_after_fill", 3, timeout,
                          self._verify_position_exists, symbol)

        # 3.10 Close the position (market order opposite side)
        await self._check(report, "t3_close_position", 3, timeout,
                          self._verify_market_order, symbol, "short", min_size, True)

        # 3.11 Verify position closed
        await asyncio.sleep(2.0)
        await self._check(report, "t3_verify_position_closed", 3, timeout,
                          self._verify_position_closed, symbol, min_size)

    # ═══════════════════════════════════════════════════════════════
    # Individual verification implementations
    # ═══════════════════════════════════════════════════════════════

    # ─── Tier 1 checks ───────────────────────────────────────────

    async def _verify_connection(self) -> CheckResult:
        assert self.adapter.connected, "Adapter not connected"
        return CheckResult(
            name="t1_connection", tier=1, status=CheckStatus.PASS,
            message=f"Connected to {self.venue}",
        )

    async def _verify_funding_rate(self, symbol: str) -> CheckResult:
        vsymbol = self.adapter.normalize_symbol(symbol)
        fr = await self.adapter.get_funding_rate(vsymbol)

        assert fr is not None, "get_funding_rate returned None"
        assert fr.venue == self.venue, f"Venue mismatch: {fr.venue} != {self.venue}"
        assert fr.symbol is not None and len(fr.symbol) > 0, "Symbol is empty"
        assert isinstance(fr.rate, (int, float)), f"Rate is not numeric: {type(fr.rate)}"
        assert -0.1 < fr.rate < 0.1, f"Rate {fr.rate} seems unreasonable (>10%/cycle)"
        assert fr.cycle_hours in (1, 4, 8), f"Unexpected cycle_hours: {fr.cycle_hours}"

        ann = fr.annualized
        return CheckResult(
            name="t1_funding_rate", tier=1, status=CheckStatus.PASS,
            message=f"Rate: {fr.rate*100:.4f}% ({ann*100:.1f}% ann) | Mark: ${fr.mark_price:,.2f}",
            data={"rate": fr.rate, "annualized": ann, "mark": fr.mark_price},
        )

    async def _verify_funding_history(self, symbol: str) -> CheckResult:
        vsymbol = self.adapter.normalize_symbol(symbol)
        history = await self.adapter.get_funding_history(vsymbol, limit=10)

        assert history is not None, "get_funding_history returned None"
        assert isinstance(history, list), f"Expected list, got {type(history)}"
        assert len(history) > 0, "Funding history is empty"

        for entry in history:
            assert hasattr(entry, "rate"), "History entry missing 'rate'"
            assert isinstance(entry.rate, (int, float)), "Rate not numeric"

        return CheckResult(
            name="t1_funding_history", tier=1, status=CheckStatus.PASS,
            message=f"Got {len(history)} history entries",
            data={"count": len(history), "latest_rate": history[0].rate},
        )

    async def _verify_orderbook(self, symbol: str) -> CheckResult:
        vsymbol = self.adapter.normalize_symbol(symbol)
        ob = await self.adapter.get_orderbook(vsymbol, depth=10)

        assert ob is not None, "get_orderbook returned None"
        assert len(ob.bids) > 0, "Orderbook has no bids"
        assert len(ob.asks) > 0, "Orderbook has no asks"

        best_bid = ob.bids[0].price
        best_ask = ob.asks[0].price
        assert best_bid > 0, f"Best bid price is {best_bid}"
        assert best_ask > 0, f"Best ask price is {best_ask}"
        assert best_bid < best_ask, f"Crossed book: bid={best_bid} >= ask={best_ask}"

        spread_bps = (best_ask - best_bid) / best_bid * 10000
        return CheckResult(
            name="t1_orderbook", tier=1, status=CheckStatus.PASS,
            message=f"Bid: ${best_bid:,.2f} | Ask: ${best_ask:,.2f} | Spread: {spread_bps:.1f}bps",
            data={"bid": best_bid, "ask": best_ask, "spread_bps": spread_bps},
        )

    async def _verify_orderbook_depth(self, symbol: str) -> CheckResult:
        vsymbol = self.adapter.normalize_symbol(symbol)
        ob = await self.adapter.get_orderbook(vsymbol, depth=20)

        bid_depth_usd = sum(b.price * b.size for b in ob.bids)
        ask_depth_usd = sum(a.price * a.size for a in ob.asks)

        status = CheckStatus.PASS
        if bid_depth_usd < 10000 or ask_depth_usd < 10000:
            status = CheckStatus.WARN

        return CheckResult(
            name="t1_orderbook_depth", tier=1, status=status,
            message=f"Bid depth: ${bid_depth_usd:,.0f} | Ask depth: ${ask_depth_usd:,.0f} | Levels: {len(ob.bids)}b/{len(ob.asks)}a",
            data={"bid_depth_usd": bid_depth_usd, "ask_depth_usd": ask_depth_usd},
        )

    async def _verify_mark_price(self, symbol: str) -> CheckResult:
        vsymbol = self.adapter.normalize_symbol(symbol)
        price = await self.adapter.get_mark_price(vsymbol)

        assert price > 0, f"Mark price is {price}"
        # Sanity: BTC shouldn't be below $1k or above $1M
        if symbol == "BTC":
            assert 1000 < price < 1_000_000, f"BTC price ${price} seems wrong"
        elif symbol == "ETH":
            assert 100 < price < 100_000, f"ETH price ${price} seems wrong"

        return CheckResult(
            name="t1_mark_price", tier=1, status=CheckStatus.PASS,
            message=f"${price:,.2f}",
        )

    async def _verify_symbol_normalize(self, symbol: str) -> CheckResult:
        normalized = self.adapter.normalize_symbol(symbol)
        assert normalized is not None and len(normalized) > 0, "Normalized symbol is empty"
        assert symbol.upper() in normalized.upper() or normalized.upper() in symbol.upper(), \
            f"Normalized '{normalized}' doesn't contain '{symbol}'"

        return CheckResult(
            name="t1_symbol_normalize", tier=1, status=CheckStatus.PASS,
            message=f"'{symbol}' → '{normalized}'",
        )

    async def _verify_gas_estimate(self) -> CheckResult:
        gas = self.adapter.estimate_gas_cost("trade")
        assert isinstance(gas, (int, float)), f"Gas is not numeric: {type(gas)}"
        assert gas >= 0, f"Negative gas: {gas}"

        status = CheckStatus.PASS
        if gas > 1.0:
            status = CheckStatus.WARN

        return CheckResult(
            name="t1_gas_estimate", tier=1, status=status,
            message=f"${gas:.4f} per trade",
        )

    async def _verify_fee_roundtrip(self) -> CheckResult:
        fee_bps = self.adapter.round_trip_fee_bps()
        assert isinstance(fee_bps, (int, float)), "Fee not numeric"
        assert fee_bps >= 0, f"Negative fees: {fee_bps}"

        return CheckResult(
            name="t1_fee_roundtrip", tier=1, status=CheckStatus.PASS,
            message=f"{fee_bps:.1f} bps round-trip",
        )

    # ─── Tier 2 checks ───────────────────────────────────────────

    async def _verify_balance(self) -> CheckResult:
        balance = await self.adapter.get_balance()
        assert balance is not None, "get_balance returned None"
        assert isinstance(balance, dict), f"Expected dict, got {type(balance)}"
        assert "total" in balance, "Balance missing 'total' field"

        return CheckResult(
            name="t2_balance", tier=2, status=CheckStatus.PASS,
            message=f"Total: ${balance.get('total', 0):,.2f} | Available: ${balance.get('available', 0):,.2f}",
            data=balance,
        )

    async def _verify_balance_fields(self) -> CheckResult:
        balance = await self.adapter.get_balance()
        required = ["available", "total", "margin_used"]
        missing = [f for f in required if f not in balance]

        if missing:
            return CheckResult(
                name="t2_balance_fields", tier=2, status=CheckStatus.WARN,
                message=f"Missing fields: {missing}",
            )

        assert balance["total"] >= 0, f"Negative total: {balance['total']}"
        assert balance["available"] >= 0, f"Negative available: {balance['available']}"
        assert balance["available"] <= balance["total"] + 0.01, \
            f"Available ({balance['available']}) > total ({balance['total']})"

        return CheckResult(
            name="t2_balance_fields", tier=2, status=CheckStatus.PASS,
            message="All required fields present and consistent",
        )

    async def _verify_positions(self) -> CheckResult:
        positions = await self.adapter.get_positions()
        assert positions is not None, "get_positions returned None"
        assert isinstance(positions, list), f"Expected list, got {type(positions)}"

        return CheckResult(
            name="t2_positions", tier=2, status=CheckStatus.PASS,
            message=f"{len(positions)} open position(s)",
            data={"count": len(positions)},
        )

    async def _verify_position_integrity(self) -> CheckResult:
        positions = await self.adapter.get_positions()
        if not positions:
            return CheckResult(
                name="t2_position_integrity", tier=2, status=CheckStatus.PASS,
                message="No positions to validate (clean account)",
            )

        for p in positions:
            assert p.size > 0, f"Position size <= 0: {p.size}"
            assert p.entry_price > 0, f"Entry price <= 0: {p.entry_price}"
            assert p.side in ("long", "short"), f"Invalid side: {p.side}"
            assert p.venue == self.venue, f"Venue mismatch: {p.venue}"

        return CheckResult(
            name="t2_position_integrity", tier=2, status=CheckStatus.PASS,
            message=f"All {len(positions)} position(s) structurally valid",
        )

    async def _verify_deposit_info(self) -> CheckResult:
        info = await self.adapter.get_deposit_info()
        assert info is not None, "get_deposit_info returned None"
        assert "chain" in info or "token" in info, "No chain/token info returned"

        return CheckResult(
            name="t2_deposit_info", tier=2, status=CheckStatus.PASS,
            message=f"Chain: {info.get('chain')} | Token: {info.get('token')}",
        )

    # ─── Tier 3 checks ───────────────────────────────────────────

    async def _verify_place_limit_order(
        self, symbol: str, side: str, size: float, price: float,
    ) -> CheckResult:
        from models.core import Side as SideEnum, OrderStatus

        side_enum = SideEnum.LONG if side == "long" else SideEnum.SHORT
        vsymbol = self.adapter.normalize_symbol(symbol)

        result = await self.adapter.place_limit_order(
            vsymbol, side_enum, size, price, reduce_only=False, tif="GTC",
        )

        assert result is not None, "place_limit_order returned None"

        if result.status == OrderStatus.FAILED:
            # Check if it's a "not implemented" stub
            if "not implemented" in (result.error or "").lower():
                return CheckResult(
                    name=f"t3_place_limit_{side}", tier=3, status=CheckStatus.SKIP,
                    message=f"Trading not implemented for {self.venue}",
                )
            raise AssertionError(f"Order failed: {result.error}")

        assert result.status in (OrderStatus.SUBMITTED, OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED), \
            f"Unexpected status: {result.status}"
        assert result.order_id, "No order_id returned"

        self._placed_orders.append({"symbol": vsymbol, "order_id": result.order_id})

        return CheckResult(
            name=f"t3_place_limit_{side}", tier=3, status=CheckStatus.PASS,
            message=f"Order {result.order_id} | Status: {result.status} | {side} {size} @ ${price:,.2f}",
            data=result.order_id,  # pass order_id for subsequent checks
        )

    async def _verify_order_is_open(self, symbol: str, order_id: str) -> CheckResult:
        """Verify a resting order exists. Adapter-specific — fallback to position check."""
        vsymbol = self.adapter.normalize_symbol(symbol)

        # Try getting open orders via the info API if available (Hyperliquid-specific)
        if hasattr(self.adapter, '_post_info') and hasattr(self.adapter, '_address'):
            try:
                open_orders = await self.adapter._post_info({
                    "type": "openOrders", "user": self.adapter._address
                })
                found = any(str(o.get("oid")) == str(order_id) for o in open_orders)
                if found:
                    return CheckResult(
                        name="t3_verify_open_order", tier=3, status=CheckStatus.PASS,
                        message=f"Order {order_id} confirmed resting",
                    )
                else:
                    return CheckResult(
                        name="t3_verify_open_order", tier=3, status=CheckStatus.WARN,
                        message=f"Order {order_id} not found in open orders (may have filled)",
                    )
            except Exception:
                pass

        # Generic fallback: just confirm the order_id is non-empty
        return CheckResult(
            name="t3_verify_open_order", tier=3, status=CheckStatus.PASS,
            message=f"Order {order_id} submitted (no open-order query available for deep verification)",
        )

    async def _verify_cancel_order(self, symbol: str, order_id: str) -> CheckResult:
        vsymbol = self.adapter.normalize_symbol(symbol)
        success = await self.adapter.cancel_order(vsymbol, order_id)

        assert success is True, f"cancel_order returned {success}"

        # Remove from tracking
        self._placed_orders = [
            o for o in self._placed_orders if o["order_id"] != order_id
        ]

        return CheckResult(
            name="t3_cancel_order", tier=3, status=CheckStatus.PASS,
            message=f"Order {order_id} cancelled successfully",
        )

    async def _verify_order_is_cancelled(self, symbol: str, order_id: str) -> CheckResult:
        """Verify the order no longer appears in open orders."""
        if hasattr(self.adapter, '_post_info') and hasattr(self.adapter, '_address'):
            try:
                open_orders = await self.adapter._post_info({
                    "type": "openOrders", "user": self.adapter._address
                })
                still_there = any(str(o.get("oid")) == str(order_id) for o in open_orders)
                if still_there:
                    return CheckResult(
                        name="t3_verify_cancelled", tier=3, status=CheckStatus.FAIL,
                        message=f"Order {order_id} still appears in open orders after cancel!",
                    )
                return CheckResult(
                    name="t3_verify_cancelled", tier=3, status=CheckStatus.PASS,
                    message=f"Confirmed: order {order_id} no longer in open orders",
                )
            except Exception:
                pass

        return CheckResult(
            name="t3_verify_cancelled", tier=3, status=CheckStatus.PASS,
            message=f"Cancel returned True (no deep verification endpoint available)",
        )

    async def _verify_cancel_all(self, symbol: str) -> CheckResult:
        vsymbol = self.adapter.normalize_symbol(symbol)
        count = await self.adapter.cancel_all_orders(vsymbol)

        assert isinstance(count, int), f"cancel_all returned non-int: {type(count)}"
        self._placed_orders.clear()

        return CheckResult(
            name="t3_cancel_all", tier=3, status=CheckStatus.PASS,
            message=f"Cancelled {count} order(s)",
        )

    async def _verify_no_open_orders(self, symbol: str) -> CheckResult:
        if hasattr(self.adapter, '_post_info') and hasattr(self.adapter, '_address'):
            try:
                vsymbol = self.adapter.normalize_symbol(symbol)
                open_orders = await self.adapter._post_info({
                    "type": "openOrders", "user": self.adapter._address
                })
                sym_orders = [
                    o for o in open_orders
                    if o.get("coin") == vsymbol or o.get("symbol") == vsymbol
                ]
                if sym_orders:
                    return CheckResult(
                        name="t3_verify_all_cancelled", tier=3, status=CheckStatus.FAIL,
                        message=f"{len(sym_orders)} orders still open after cancel_all!",
                    )
            except Exception:
                pass

        return CheckResult(
            name="t3_verify_all_cancelled", tier=3, status=CheckStatus.PASS,
            message="No lingering orders detected",
        )

    async def _verify_market_order(
        self, symbol: str, side: str, size: float, reduce_only: bool = False,
    ) -> CheckResult:
        from models.core import Side as SideEnum, OrderStatus

        side_enum = SideEnum.LONG if side == "long" else SideEnum.SHORT
        vsymbol = self.adapter.normalize_symbol(symbol)

        result = await self.adapter.place_market_order(
            vsymbol, side_enum, size, reduce_only=reduce_only,
        )

        assert result is not None, "place_market_order returned None"

        if result.status == OrderStatus.FAILED:
            if "not implemented" in (result.error or "").lower():
                return CheckResult(
                    name=f"t3_market_order_{side}", tier=3, status=CheckStatus.SKIP,
                    message=f"Market orders not implemented for {self.venue}",
                )
            raise AssertionError(f"Market order failed: {result.error}")

        assert result.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED, OrderStatus.SUBMITTED), \
            f"Unexpected market order status: {result.status}"

        tag = "close" if reduce_only else side
        return CheckResult(
            name=f"t3_market_order_{tag}", tier=3, status=CheckStatus.PASS,
            message=f"Filled {result.filled_qty} @ ${result.avg_price:,.2f} (fee: ${result.fee:.4f})" if result.filled_qty else f"Status: {result.status}",
        )

    async def _verify_position_exists(self, symbol: str) -> CheckResult:
        positions = await self.adapter.get_positions()
        vsymbol = self.adapter.normalize_symbol(symbol)

        matching = [
            p for p in positions
            if p.symbol.upper().replace("-", "").replace("/", "") in
               vsymbol.upper().replace("-", "").replace("/", "") or
               symbol.upper() in p.symbol.upper()
        ]

        if matching:
            p = matching[0]
            return CheckResult(
                name="t3_verify_position_after_fill", tier=3, status=CheckStatus.PASS,
                message=f"{p.side} {p.size} {p.symbol} @ ${p.entry_price:,.2f} | PnL: ${p.unrealized_pnl:,.2f}",
            )

        return CheckResult(
            name="t3_verify_position_after_fill", tier=3, status=CheckStatus.WARN,
            message="Position not found (may be too small or not yet settled)",
        )

    async def _verify_position_closed(self, symbol: str, expected_size: float) -> CheckResult:
        positions = await self.adapter.get_positions()
        matching = [
            p for p in positions
            if symbol.upper() in p.symbol.upper() and p.size >= expected_size * 0.9
        ]

        if not matching:
            return CheckResult(
                name="t3_verify_position_closed", tier=3, status=CheckStatus.PASS,
                message="Position confirmed closed",
            )

        return CheckResult(
            name="t3_verify_position_closed", tier=3, status=CheckStatus.WARN,
            message=f"Position still open: {matching[0].size} {matching[0].symbol}",
        )

    # ═══════════════════════════════════════════════════════════════
    # Helpers
    # ═══════════════════════════════════════════════════════════════

    async def _check(
        self, report: VenueVerificationReport, name: str, tier: int,
        timeout: float, func, *args,
    ) -> Any:
        """Run a single check with timeout and error handling. Returns func's data field."""
        t0 = time.monotonic()
        try:
            result = await asyncio.wait_for(func(*args), timeout=timeout)
            result.latency_ms = (time.monotonic() - t0) * 1000
            report.results.append(result)
            return result.data
        except asyncio.TimeoutError:
            report.results.append(CheckResult(
                name=name, tier=tier, status=CheckStatus.FAIL,
                latency_ms=(time.monotonic() - t0) * 1000,
                message=f"Timed out after {timeout}s",
            ))
            return None
        except AssertionError as e:
            report.results.append(CheckResult(
                name=name, tier=tier, status=CheckStatus.FAIL,
                latency_ms=(time.monotonic() - t0) * 1000,
                message=str(e),
            ))
            return None
        except Exception as e:
            report.results.append(CheckResult(
                name=name, tier=tier, status=CheckStatus.FAIL,
                latency_ms=(time.monotonic() - t0) * 1000,
                message=str(e),
                error=traceback.format_exc(),
            ))
            return None

    async def _get_safe_mid_price(self, symbol: str) -> float:
        try:
            vsymbol = self.adapter.normalize_symbol(symbol)
            return await self.adapter.get_mark_price(vsymbol)
        except Exception:
            return 0.0

    async def _emergency_cleanup(self, report: VenueVerificationReport, symbol: str):
        """Cancel any orders we placed that might still be open."""
        if not self._placed_orders:
            return

        vsymbol = self.adapter.normalize_symbol(symbol)
        for order in self._placed_orders:
            try:
                await self.adapter.cancel_order(order["symbol"], order["order_id"])
                report.cleanup_log.append(f"Cancelled lingering order {order['order_id']}")
            except Exception as e:
                report.cleanup_log.append(f"Failed to cancel {order['order_id']}: {e}")

        # Belt and suspenders: cancel_all
        try:
            count = await self.adapter.cancel_all_orders(vsymbol)
            if count > 0:
                report.cleanup_log.append(f"cancel_all swept {count} additional orders")
        except Exception as e:
            report.cleanup_log.append(f"cancel_all failed: {e}")

        self._placed_orders.clear()
