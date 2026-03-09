"""
hedgehog/services/data/venue_scorer.py
Dynamically ranks DeFi perp venues for funding rate capture.

Scoring factors (weights from config/strategy.yaml):
  avg_funding_rate  0.25  — historical yield
  funding_consistency 0.20 — lower std dev = more predictable
  liquidity_depth   0.20  — can you enter/exit without slippage?
  trading_fees      0.15  — lower fees = more net yield
  funding_cycle     0.10  — 1h > 4h > 8h
  contract_maturity 0.05  — older = safer
  uptime            0.05  — chain liveness
"""
from __future__ import annotations

import math
from typing import Optional

import structlog

from models.core import VenueScore, VenueConfig

logger = structlog.get_logger()

DEFAULT_WEIGHTS = {
    "avg_funding_rate": 0.25,
    "funding_consistency": 0.20,
    "liquidity_depth": 0.20,
    "trading_fees": 0.15,
    "funding_cycle": 0.10,
    "contract_maturity": 0.05,
    "uptime": 0.05,
}


class VenueScorer:
    def __init__(
        self,
        collector,
        adapters: dict,
        venue_configs: dict[str, VenueConfig],
        weights: Optional[dict] = None,
    ):
        self.collector = collector
        self.adapters = adapters
        self.venue_configs = venue_configs
        self.weights = weights or DEFAULT_WEIGHTS
        self._scores: dict[tuple[str, str], VenueScore] = {}

    async def score_all(self) -> list[VenueScore]:
        scores = []
        for symbol in self.collector.symbols:
            for venue_name, adapter in self.adapters.items():
                if not adapter.connected:
                    continue
                try:
                    score = await self._score_venue(venue_name, adapter, symbol)
                    if score:
                        scores.append(score)
                        self._scores[(venue_name, symbol)] = score
                except Exception as e:
                    logger.debug("scorer.failed", venue=venue_name, symbol=symbol, error=str(e))
        scores.sort(key=lambda s: s.composite_score, reverse=True)
        return scores

    async def _score_venue(self, venue_name: str, adapter, symbol: str) -> Optional[VenueScore]:
        config = self.venue_configs.get(venue_name)
        if not config:
            return None

        # Current rate from collector
        rate_key = (venue_name, symbol)
        current_rate = self.collector.latest_rates.get(rate_key)
        if not current_rate:
            return None

        avg_rate_annual = current_rate.annualized

        # Historical stats for consistency
        stats = self.collector.get_historical_stats(venue_name, symbol, hours=24)
        rate_std = stats["std"] if stats["count"] > 1 else 0.5

        # Liquidity depth
        depth = 0.0
        try:
            vsymbol = adapter.normalize_symbol(symbol)
            ob = await adapter.get_orderbook(vsymbol, depth=20)
            depth_info = ob.depth_at_pct(1.0)
            depth = (depth_info["bid_depth"] + depth_info["ask_depth"]) / 2
        except Exception:
            pass

        # Component scores (0-1 range)
        rate_score = min(1.0, abs(avg_rate_annual) / 0.50)
        consistency_score = max(0.0, 1.0 - rate_std * 100)
        depth_score = min(1.0, depth / 5_000_000)
        fee_score = max(0.0, 1.0 - config.taker_fee_bps / 10.0)
        cycle_score = 1.0 / config.funding_cycle_hours
        maturity_score = 0.5  # no on-chain maturity data yet
        uptime_score = 0.9    # no liveness tracking yet

        w = self.weights
        composite = (
            rate_score * w.get("avg_funding_rate", 0.25)
            + consistency_score * w.get("funding_consistency", 0.20)
            + depth_score * w.get("liquidity_depth", 0.20)
            + fee_score * w.get("trading_fees", 0.15)
            + cycle_score * w.get("funding_cycle", 0.10)
            + maturity_score * w.get("contract_maturity", 0.05)
            + uptime_score * w.get("uptime", 0.05)
        )

        volume = self.collector.venue_volumes.get(venue_name.lower(), {})

        return VenueScore(
            venue=venue_name,
            symbol=symbol,
            avg_funding_rate_30d=avg_rate_annual,
            funding_rate_std_30d=rate_std,
            liquidity_depth_1pct_usd=depth,
            trading_fee_bps=config.taker_fee_bps + config.maker_fee_bps,
            funding_cycle_hours=config.funding_cycle_hours,
            composite_score=composite,
            daily_volume_usd=volume.get("volume_24h", 0),
        )

    # ── Query methods ────────────────────────────────────────────────────

    def get_top_venues(self, symbol: str, n: int = 5) -> list[VenueScore]:
        venues = [s for (v, sy), s in self._scores.items() if sy == symbol]
        venues.sort(key=lambda s: s.composite_score, reverse=True)
        return venues[:n]

    def get_best_pair(self, symbol: str) -> Optional[dict]:
        venues = self.get_top_venues(symbol, n=20)
        if len(venues) < 2:
            return None
        by_rate = sorted(venues, key=lambda s: s.avg_funding_rate_30d, reverse=True)
        best_short = by_rate[0]
        best_long = by_rate[-1]
        spread = best_short.avg_funding_rate_30d - best_long.avg_funding_rate_30d
        if spread <= 0:
            return None
        return {
            "short_venue": best_short.venue,
            "long_venue": best_long.venue,
            "short_rate_annual": best_short.avg_funding_rate_30d,
            "long_rate_annual": best_long.avg_funding_rate_30d,
            "short_score": best_short.composite_score,
            "long_score": best_long.composite_score,
            "spread_pct": spread * 100,
        }

    def get_best_hedge_pairs(self, symbol: str, top_n: int = 5) -> list[dict]:
        venues = [s for (v, sy), s in self._scores.items() if sy == symbol]
        if len(venues) < 2:
            return []
        by_rate = sorted(venues, key=lambda s: s.avg_funding_rate_30d, reverse=True)
        pairs = []
        for short_vs in by_rate:
            for long_vs in reversed(by_rate):
                if short_vs.venue == long_vs.venue:
                    continue
                spread = short_vs.avg_funding_rate_30d - long_vs.avg_funding_rate_30d
                if spread <= 0:
                    continue
                cq = math.sqrt(short_vs.composite_score * long_vs.composite_score)
                pairs.append({
                    "symbol": symbol,
                    "short_venue": short_vs.venue,
                    "long_venue": long_vs.venue,
                    "spread_annual": spread,
                    "short_score": short_vs.composite_score,
                    "long_score": long_vs.composite_score,
                    "combined_quality": cq,
                    "pair_rank": spread * cq,
                })
        pairs.sort(key=lambda p: p["pair_rank"], reverse=True)
        return pairs[:top_n]

    def print_scoreboard(self, symbol: str):
        top = self.get_top_venues(symbol, n=15)
        if not top:
            print(f"  No scores for {symbol}")
            return
        print(f"\n{'=' * 80}")
        print(f" VENUE SCOREBOARD: {symbol}")
        print(f"{'=' * 80}")
        print(f"  {'#':<4} {'Venue':<15} {'Score':>6} {'Rate (ann)':>12} "
              f"{'Fees':>8} {'Depth':>12} {'Cycle':>6}")
        print(f"  {'-' * 65}")
        for i, s in enumerate(top):
            print(f"  {i+1:<4} {s.venue:<15} {s.composite_score:>6.4f} "
                  f"{s.avg_funding_rate_30d*100:>+11.2f}% "
                  f"{s.trading_fee_bps:>7.1f}bp "
                  f"${s.liquidity_depth_1pct_usd:>10,.0f} "
                  f"{s.funding_cycle_hours:>4}h")
