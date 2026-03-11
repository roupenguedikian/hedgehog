"""
hedgehog/services/data/funding_collector.py
Unified funding rate collector across all DeFi venues.
Polls every venue every 30s, normalizes rates, detects opportunities.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Optional

import structlog

from adapters.base_adapter import BaseDefiAdapter
from services.data.coinglass_client import CoinglassClient, DefiLlamaClient

logger = structlog.get_logger()


class FundingRateCollector:
    """
    Continuously collects funding rates from all connected venues.
    
    Data flow:
    1. Direct API calls to each venue adapter (primary)
    2. CoinGlass API for cross-exchange comparison (supplementary)
    3. Stores in memory (latest snapshot) + TimescaleDB (historical)
    4. Detects arbitrage opportunities in real-time
    """

    def __init__(
        self,
        adapters: dict[str, BaseDefiAdapter],
        symbols: list[str],
        coinglass: Optional[CoinglassClient] = None,
        defillama: Optional[DefiLlamaClient] = None,
        min_spread_annual: float = 0.10,
    ):
        self.adapters = adapters
        self.symbols = symbols
        self.coinglass = coinglass
        self.defillama = defillama
        self.min_spread_annual = min_spread_annual

        # Latest state
        self.latest_rates: dict[tuple[str, str], dict] = {}  # (venue, symbol) -> rate dict
        self.latest_opportunities: list[dict] = []
        self.venue_volumes: dict[str, dict] = {}  # venue -> {volume_24h, ...}
        self.last_update: Optional[datetime] = None

        # Historical buffer (last 24h in memory, rest in DB)
        self.rate_history: list[dict] = []
        self._max_history = 10000

    async def collect_once(self) -> dict:
        """
        Run one collection cycle across all venues and symbols.
        Returns the current rate matrix + opportunities.
        """
        start = time.monotonic()
        tasks = []

        # 1. Collect from all venue adapters
        for venue_name, adapter in self.adapters.items():
            if not adapter.connected:
                continue
            for symbol in self.symbols:
                tasks.append(self._collect_venue_rate(venue_name, adapter, symbol))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        successful = 0
        for result in results:
            if isinstance(result, Exception):
                logger.warning("funding.collect_error", error=str(result))
                continue
            if result is None:
                continue
            key = (result["venue"], result["symbol"])
            self.latest_rates[key] = result
            self.rate_history.append(result)
            successful += 1

        # Trim history buffer
        if len(self.rate_history) > self._max_history:
            self.rate_history = self.rate_history[-self._max_history:]

        # 2. Supplement with CoinGlass data
        if self.coinglass:
            await self._collect_coinglass_supplement()

        # 3. Get volume data from DefiLlama
        if self.defillama:
            self.venue_volumes = await self.defillama.get_perp_volumes()

        # 4. Detect opportunities
        self.latest_opportunities = self._find_opportunities()

        elapsed = time.monotonic() - start
        self.last_update = datetime.now(timezone.utc)

        logger.info(
            "funding.collected",
            rates=successful,
            opportunities=len(self.latest_opportunities),
            elapsed_ms=round(elapsed * 1000),
        )

        return {
            "rates": self.latest_rates,
            "opportunities": self.latest_opportunities,
            "timestamp": self.last_update,
        }

    async def _collect_venue_rate(
        self, venue_name: str, adapter: BaseDefiAdapter, symbol: str
    ) -> Optional[dict]:
        """Collect funding rate from a single venue + symbol.
        Pass raw symbol — each adapter normalizes internally in get_funding_rate.
        Returns a plain dict with keys: venue, symbol, rate, cycle_hours, mark_price,
        index_price, next_funding_ts, predicted_rate, timestamp.
        """
        try:
            rate_obj = await adapter.get_funding_rate(symbol)
            # Build a plain dict, pulling attributes from whatever the adapter returns
            rate_val = getattr(rate_obj, "rate", None) or rate_obj.get("rate", 0) if isinstance(rate_obj, dict) else rate_obj.rate
            cycle_hours = getattr(rate_obj, "cycle_hours", 8) if not isinstance(rate_obj, dict) else rate_obj.get("cycle_hours", 8)
            return {
                "venue": venue_name,
                "symbol": symbol,
                "rate": rate_val,
                "cycle_hours": cycle_hours,
                "mark_price": getattr(rate_obj, "mark_price", None) if not isinstance(rate_obj, dict) else rate_obj.get("mark_price"),
                "index_price": getattr(rate_obj, "index_price", None) if not isinstance(rate_obj, dict) else rate_obj.get("index_price"),
                "next_funding_ts": getattr(rate_obj, "next_funding_ts", None) if not isinstance(rate_obj, dict) else rate_obj.get("next_funding_ts"),
                "predicted_rate": getattr(rate_obj, "predicted_rate", None) if not isinstance(rate_obj, dict) else rate_obj.get("predicted_rate"),
                "timestamp": datetime.now(timezone.utc),
            }
        except Exception as e:
            # Many venues don't support all symbols — this is expected
            logger.debug("funding.venue_error", venue=venue_name, symbol=symbol, error=str(e))
            return None

    async def _collect_coinglass_supplement(self):
        """Fetch CoinGlass data for additional context."""
        try:
            for symbol in self.symbols[:5]:  # Top 5 symbols only to avoid rate limits
                cg_rates = await self.coinglass.get_current_funding_rates(symbol)
                for item in cg_rates:
                    exchange = item["exchange"].lower()
                    # Only add if we don't already have direct data
                    key = (exchange, symbol)
                    if key not in self.latest_rates:
                        self.latest_rates[key] = {
                            "venue": exchange,
                            "symbol": symbol,
                            "rate": item["rate"],
                            "cycle_hours": 8,  # CoinGlass default assumption
                            "mark_price": None,
                            "index_price": None,
                            "next_funding_ts": None,
                            "predicted_rate": None,
                            "timestamp": datetime.now(timezone.utc),
                        }
        except Exception as e:
            logger.warning("funding.coinglass_supplement_error", error=str(e))

    @staticmethod
    def _annualized(rate: dict) -> float:
        """Compute annualized funding rate from a rate dict."""
        return rate["rate"] * (8760 / rate.get("cycle_hours", 8))

    def _find_opportunities(self) -> list[dict]:
        """
        Find perp-perp hedge opportunities.
        For each symbol, find the venue pair with the widest funding rate spread.
        """
        opportunities = []

        for symbol in self.symbols:
            # Collect all rates for this symbol across venues
            rates_for_symbol = []
            for (venue, sym), rate in self.latest_rates.items():
                if sym == symbol and venue in self.adapters:  # Only our connected venues
                    rates_for_symbol.append((venue, rate))

            if len(rates_for_symbol) < 2:
                continue

            # Sort by annualized rate descending
            rates_for_symbol.sort(key=lambda x: self._annualized(x[1]), reverse=True)

            # Best opportunity: highest rate venue (short) vs lowest (long)
            best_short_venue, best_short_rate = rates_for_symbol[0]
            best_long_venue, best_long_rate = rates_for_symbol[-1]

            short_ann = self._annualized(best_short_rate)
            long_ann = self._annualized(best_long_rate)
            spread = short_ann - long_ann

            if spread >= self.min_spread_annual:
                opp = {
                    "symbol": symbol,
                    "short_venue": best_short_venue,
                    "long_venue": best_long_venue,
                    "short_rate_annual": short_ann,
                    "long_rate_annual": long_ann,
                    "spread_annual": spread,
                }
                opportunities.append(opp)

            # Also check second-best pairs (for diversification)
            if len(rates_for_symbol) >= 3:
                second_short_venue, second_short_rate = rates_for_symbol[1]
                second_short_ann = self._annualized(second_short_rate)
                second_spread = second_short_ann - long_ann
                if second_spread >= self.min_spread_annual and second_short_venue != best_short_venue:
                    opportunities.append({
                        "symbol": symbol,
                        "short_venue": second_short_venue,
                        "long_venue": best_long_venue,
                        "short_rate_annual": second_short_ann,
                        "long_rate_annual": long_ann,
                        "spread_annual": second_spread,
                    })

        # Sort by spread descending
        opportunities.sort(key=lambda o: o["spread_annual"], reverse=True)
        return opportunities

    def get_rate_matrix(self) -> dict[str, dict[str, float]]:
        """
        Build a symbol x venue matrix of annualized funding rates.
        Useful for the strategist agent.
        """
        matrix = {}
        for (venue, symbol), rate in self.latest_rates.items():
            if symbol not in matrix:
                matrix[symbol] = {}
            annualized = self._annualized(rate)
            matrix[symbol][venue] = round(annualized * 100, 2)  # as percentage
        return matrix

    def get_historical_stats(self, venue: str, symbol: str, hours: int = 24) -> dict:
        """
        Compute statistics on recent funding rate history for a venue+symbol.
        Returns mean, std, min, max, flip_count.
        """
        now = datetime.now(timezone.utc)
        cutoff = now.timestamp() - hours * 3600

        relevant = [
            r for r in self.rate_history
            if r["venue"] == venue and r["symbol"] == symbol and r["timestamp"].timestamp() > cutoff
        ]

        if not relevant:
            return {"mean": 0, "std": 0, "min": 0, "max": 0, "count": 0, "flip_count": 0}

        rates = [r["rate"] for r in relevant]
        import statistics
        mean = statistics.mean(rates)
        std = statistics.stdev(rates) if len(rates) > 1 else 0
        flip_count = sum(1 for i in range(1, len(rates)) if rates[i] * rates[i-1] < 0)

        return {
            "mean": mean,
            "std": std,
            "min": min(rates),
            "max": max(rates),
            "count": len(rates),
            "flip_count": flip_count,
        }

    async def run_continuous(self, interval_sec: int = 30):
        """Run the collector in a continuous loop."""
        logger.info("funding.collector_started", interval=interval_sec, venues=list(self.adapters.keys()))
        while True:
            try:
                await self.collect_once()
            except Exception as e:
                logger.error("funding.collector_cycle_error", error=str(e))
            await asyncio.sleep(interval_sec)
