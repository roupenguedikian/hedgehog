"""
hedgehog/main.py
Main entry point. Wires up all components and starts the bot.

Usage:
  python main.py                    # Run in OBSERVE_ONLY mode (default, safe)
  python main.py --mode SEMI_AUTO   # Run with human approval for trades
  python main.py --mode FULL_AUTO   # Fully autonomous (use with caution)
  python main.py --score-only       # Just score venues and print scoreboard
  python main.py --collect-only     # Just collect funding rates continuously
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

import structlog
import yaml

# Configure structured logging
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.dev.ConsoleRenderer(),
    ],
)
logger = structlog.get_logger()


def load_config(config_dir: str = "config") -> dict:
    """Load all YAML configs."""
    configs = {}
    for name in ["venues", "strategy", "risk"]:
        path = Path(config_dir) / f"{name}.yaml"
        if path.exists():
            with open(path) as f:
                configs[name] = yaml.safe_load(f)
        else:
            logger.warning(f"Config not found: {path}")
            configs[name] = {}
    return configs


def build_venue_configs(venues_yaml: dict) -> dict:
    """Parse venue YAML into VenueConfig objects."""
    from models.core import VenueConfig, ChainType, VenueTier

    venue_configs = {}
    for name, cfg in venues_yaml.get("venues", {}).items():
        try:
            venue_configs[name] = VenueConfig(
                name=cfg["name"],
                chain=cfg["chain"],
                chain_type=ChainType(cfg["chain_type"]),
                settlement_chain=cfg.get("settlement_chain", ""),
                funding_cycle_hours=cfg.get("funding_cycle_hours", 8),
                maker_fee_bps=cfg.get("maker_fee_bps", 0),
                taker_fee_bps=cfg.get("taker_fee_bps", 0),
                max_leverage=cfg.get("max_leverage", 20),
                collateral_token=cfg.get("collateral_token", "USDC"),
                has_api=cfg.get("has_api", True),
                api_base_url=cfg.get("api_base_url", ""),
                ws_url=cfg.get("ws_url", ""),
                deposit_chain=cfg.get("deposit_chain", ""),
                tier=VenueTier(cfg.get("tier", "tier_2")),
                zero_gas=cfg.get("zero_gas", False),
                has_escape_hatch=cfg.get("has_escape_hatch", False),
                has_privacy=cfg.get("has_privacy", False),
                has_anti_mev=cfg.get("has_anti_mev", False),
                yield_bearing_collateral=cfg.get("yield_bearing_collateral", False),
                symbol_format=cfg.get("symbol_format", "{symbol}"),
            )
        except Exception as e:
            logger.warning(f"Failed to parse venue config: {name}", error=str(e))
    return venue_configs


def build_adapters(venue_configs: dict) -> dict:
    """Instantiate adapters for all configured venues."""
    from adapters.hyperliquid_adapter import HyperliquidAdapter
    from adapters.generic_rest_adapter import (
        GenericRestAdapter, AsterAdapter, LighterAdapter, EtherealAdapter, ApexAdapter,
    )

    ADAPTER_MAP = {
        "hyperliquid": HyperliquidAdapter,
        "lighter": LighterAdapter,
        "aster": AsterAdapter,
        "drift": GenericRestAdapter,     # TODO: full Drift adapter with driftpy
        "dydx": GenericRestAdapter,      # TODO: full dYdX adapter with v4-client
        "apex": ApexAdapter,
        "paradex": GenericRestAdapter,   # TODO: full Paradex adapter with paradex-py
        "ethereal": EtherealAdapter,
        "injective": GenericRestAdapter,  # TODO: full Injective adapter with injective-py
    }

    adapters = {}
    for name, config in venue_configs.items():
        adapter_cls = ADAPTER_MAP.get(name, GenericRestAdapter)
        adapters[name] = adapter_cls(config)

    return adapters


async def connect_adapters(adapters: dict, venue_configs: dict) -> dict:
    """Connect all adapters. Uses env vars for private keys."""
    connected = {}
    for name, adapter in adapters.items():
        # Look for venue-specific key first, then fallback to generic
        pk = os.getenv(f"{name.upper()}_PRIVATE_KEY", os.getenv("EVM_PRIVATE_KEY", ""))
        if not pk:
            logger.info(f"No private key for {name} — running in read-only mode")
            # Still connect for data access (most venues allow public data without auth)
            try:
                await adapter.connect("")
                connected[name] = adapter
            except Exception:
                pass
            continue

        try:
            success = await adapter.connect(pk)
            if success:
                connected[name] = adapter
                logger.info(f"Connected: {name}")
            else:
                logger.warning(f"Failed to connect: {name}")
        except Exception as e:
            logger.warning(f"Connection error for {name}: {e}")

    return connected


async def run_score_only(configs: dict):
    """Score all venues and print the scoreboard."""
    venue_configs = build_venue_configs(configs["venues"])
    adapters = build_adapters(venue_configs)
    symbols = configs["venues"].get("symbols", ["BTC", "ETH", "SOL"])

    # Connect adapters (read-only is fine)
    connected = await connect_adapters(adapters, venue_configs)

    # Initialize data pipeline
    from services.data.coinglass_client import CoinglassClient, DefiLlamaClient
    from services.data.funding_collector import FundingRateCollector
    from services.data.venue_scorer import VenueScorer

    coinglass = CoinglassClient()
    defillama = DefiLlamaClient()

    collector = FundingRateCollector(
        adapters=connected,
        symbols=symbols,
        coinglass=coinglass,
        defillama=defillama,
        min_spread_annual=configs["strategy"].get("min_spread_annual", 0.10),
    )

    scorer = VenueScorer(
        collector=collector,
        adapters=connected,
        venue_configs=venue_configs,
        weights=configs["strategy"].get("scoring_weights"),
    )

    # Collect data
    logger.info("Collecting funding rates...")
    await collector.collect_once()

    # Score venues
    logger.info("Scoring venues...")
    scores = await scorer.score_all()

    # Print scoreboards
    for symbol in symbols[:3]:
        scorer.print_scoreboard(symbol)

    # Print opportunities
    print("\n" + "=" * 80)
    print(" TOP ARBITRAGE OPPORTUNITIES")
    print("=" * 80)
    for i, opp in enumerate(collector.latest_opportunities[:10]):
        print(
            f"  {i+1}. {opp.symbol:<6} SHORT {opp.short_venue:<15} ({opp.short_rate_annual*100:+.2f}% ann) "
            f"LONG {opp.long_venue:<15} ({opp.long_rate_annual*100:+.2f}% ann) "
            f"= {opp.spread_pct:.2f}% spread"
        )

    # Print rate matrix
    print("\n" + "=" * 80)
    print(" FUNDING RATE MATRIX (annualized %)")
    print("=" * 80)
    matrix = collector.get_rate_matrix()
    for symbol, venues in sorted(matrix.items()):
        parts = ", ".join(f"{v}:{r:+.2f}%" for v, r in sorted(venues.items(), key=lambda x: -x[1]))
        print(f"  {symbol:<6} {parts}")

    await coinglass.close()
    await defillama.close()


async def run_collect_only(configs: dict):
    """Run the funding rate collector continuously, exporting metrics to Prometheus."""
    venue_configs = build_venue_configs(configs["venues"])
    adapters = build_adapters(venue_configs)
    symbols = configs["venues"].get("symbols", ["BTC", "ETH", "SOL"])
    connected = await connect_adapters(adapters, venue_configs)

    from services.data.coinglass_client import CoinglassClient, DefiLlamaClient
    from services.data.funding_collector import FundingRateCollector
    from services.monitoring import metrics as m

    coinglass = CoinglassClient()
    defillama = DefiLlamaClient()

    collector = FundingRateCollector(
        adapters=connected, symbols=symbols,
        coinglass=coinglass, defillama=defillama,
        min_spread_annual=configs["strategy"].get("min_spread_annual", 0.10),
    )

    async def collect_and_export():
        """Collect once and push all metrics to Prometheus gauges."""
        await collector.collect_once()

        # Venue health
        healthy = 0
        for name in connected:
            m.VENUE_UP.labels(venue=name).set(1)
            healthy += 1
        m.VENUES_HEALTHY.set(healthy)

        # Funding rates (annualized, raw, predicted) + market data
        for (venue, symbol), rate in collector.latest_rates.items():
            m.FUNDING_RATE.labels(venue=venue, symbol=symbol).set(rate.annualized)
            m.FUNDING_RATE_RAW.labels(venue=venue, symbol=symbol).set(rate.rate)
            if rate.predicted_rate is not None:
                predicted_ann = rate.predicted_rate * (8760 / rate.cycle_hours)
                m.FUNDING_RATE_PREDICTED.labels(venue=venue, symbol=symbol).set(predicted_ann)
            if rate.mark_price:
                m.MARK_PRICE.labels(venue=venue, symbol=symbol).set(rate.mark_price)
            if rate.index_price:
                m.INDEX_PRICE.labels(venue=venue, symbol=symbol).set(rate.index_price)

        # 24h statistics
        for venue_name in connected:
            for symbol in symbols:
                stats = collector.get_historical_stats(venue_name, symbol, hours=24)
                if stats["count"] > 0:
                    cycle_h = venue_configs.get(
                        venue_name,
                        venue_configs.get(list(venue_configs.keys())[0]),
                    ).funding_cycle_hours
                    m.FUNDING_RATE_MEAN_24H.labels(venue=venue_name, symbol=symbol).set(
                        stats["mean"] * (8760 / cycle_h)
                    )
                    m.FUNDING_RATE_STD_24H.labels(venue=venue_name, symbol=symbol).set(stats["std"])
                    m.FUNDING_RATE_FLIPS_24H.labels(venue=venue_name, symbol=symbol).set(stats["flip_count"])

        # Opportunities
        opps = collector.latest_opportunities
        m.OPPORTUNITIES_FOUND.set(len(opps))
        for opp in opps[:8]:
            m.BEST_SPREAD.labels(
                symbol=opp.symbol,
                short_venue=opp.short_venue,
                long_venue=opp.long_venue,
            ).set(opp.spread_annual)

    logger.info("Starting continuous collection (Ctrl+C to stop)...")
    interval_sec = 30
    while True:
        try:
            await collect_and_export()
        except Exception as e:
            logger.error("funding.collector_cycle_error", error=str(e))
        await asyncio.sleep(interval_sec)


async def run_full_bot(configs: dict, mode: str):
    """Run the full agentic bot."""
    venue_configs = build_venue_configs(configs["venues"])
    adapters = build_adapters(venue_configs)
    symbols = configs["venues"].get("symbols", ["BTC", "ETH", "SOL"])
    connected = await connect_adapters(adapters, venue_configs)

    from services.data.coinglass_client import CoinglassClient, DefiLlamaClient
    from services.data.funding_collector import FundingRateCollector
    from services.data.venue_scorer import VenueScorer
    from services.risk.risk_engine import RiskEngine
    from services.risk.circuit_breaker import CircuitBreaker
    from agents.tools.funding_tools import build_funding_tools

    coinglass = CoinglassClient()
    defillama = DefiLlamaClient()

    collector = FundingRateCollector(
        adapters=connected, symbols=symbols,
        coinglass=coinglass, defillama=defillama,
        min_spread_annual=configs["strategy"].get("min_spread_annual", 0.10),
    )

    scorer = VenueScorer(
        collector=collector, adapters=connected,
        venue_configs=venue_configs,
        weights=configs["strategy"].get("scoring_weights"),
    )

    risk_engine = RiskEngine(
        config=configs["risk"],
        venue_configs=venue_configs,
    )

    circuit_breaker = CircuitBreaker(
        config=configs["risk"].get("circuit_breaker", {}),
        adapters=connected,
    )

    tools = build_funding_tools(collector, scorer, connected, venue_configs)

    # Import orchestrator components
    # Using inline orchestrator to avoid circular imports
    from models.core import PortfolioSnapshot, TradeAction, ActionType, RiskDecision
    from services.monitoring import metrics as m
    import json as json_mod

    class BotEngine:
        """Simplified orchestrator that ties everything together."""

        def __init__(self):
            self.portfolio = PortfolioSnapshot(total_nav=100000)  # Paper trading default
            self.cycle_count = 0
            self.decision_log = []

        async def run_cycle(self):
            self.cycle_count += 1
            cycle_start = time.monotonic()
            logger.info("bot.cycle", n=self.cycle_count, mode=mode)

            # 1. Collect data
            await collector.collect_once()
            scores = await scorer.score_all()

            # Update venue metrics
            healthy = 0
            for name in connected:
                m.VENUE_UP.labels(venue=name).set(1)
                healthy += 1
            m.VENUES_HEALTHY.set(healthy)
            for vs in (scores or []):
                venue_name = vs.venue
                if venue_name:
                    m.VENUE_SCORE.labels(venue=venue_name).set(
                        vs.composite_score
                    )
                    # Score components from VenueScore model fields
                    component_map = {
                        "avg_funding_rate": vs.avg_funding_rate_30d,
                        "funding_consistency": vs.funding_rate_std_30d,
                        "liquidity_depth": vs.liquidity_depth_1pct_usd,
                        "trading_fees": vs.trading_fee_bps,
                        "funding_cycle": vs.funding_cycle_hours,
                        "contract_maturity": vs.contract_age_months,
                        "uptime": vs.uptime_30d,
                    }
                    for comp, val in component_map.items():
                        if val:
                            m.VENUE_SCORE_COMPONENT.labels(
                                venue=venue_name, component=comp
                            ).set(val)

            # Update funding rate metrics (raw + annualized + predicted)
            for (venue, symbol), rate in collector.latest_rates.items():
                ann = rate.annualized
                m.FUNDING_RATE.labels(venue=venue, symbol=symbol).set(ann)
                m.FUNDING_RATE_RAW.labels(venue=venue, symbol=symbol).set(rate.rate)
                if rate.predicted_rate is not None:
                    predicted_ann = rate.predicted_rate * (8760 / rate.cycle_hours)
                    m.FUNDING_RATE_PREDICTED.labels(venue=venue, symbol=symbol).set(predicted_ann)
                # Market data from funding rate objects
                if rate.mark_price:
                    m.MARK_PRICE.labels(venue=venue, symbol=symbol).set(rate.mark_price)
                if rate.index_price:
                    m.INDEX_PRICE.labels(venue=venue, symbol=symbol).set(rate.index_price)

            # Funding rate 24h statistics
            for venue_name in connected:
                for symbol in symbols:
                    stats = collector.get_historical_stats(venue_name, symbol, hours=24)
                    if stats["count"] > 0:
                        m.FUNDING_RATE_MEAN_24H.labels(venue=venue_name, symbol=symbol).set(
                            stats["mean"] * (8760 / venue_configs.get(venue_name, venue_configs.get(list(venue_configs.keys())[0])).funding_cycle_hours)
                        )
                        m.FUNDING_RATE_STD_24H.labels(venue=venue_name, symbol=symbol).set(stats["std"])
                        m.FUNDING_RATE_FLIPS_24H.labels(venue=venue_name, symbol=symbol).set(stats["flip_count"])

            # Per-venue balances and orderbook data
            for venue_name, adapter in connected.items():
                try:
                    t0 = time.monotonic()
                    balance = await adapter.get_balance()
                    m.VENUE_API_LATENCY.labels(venue=venue_name, endpoint="get_balance").observe(
                        time.monotonic() - t0
                    )
                    available = balance.get("available", 0)
                    total = balance.get("total", balance.get("equity", 0))
                    margin_used = balance.get("margin", balance.get("margin_used", 0))
                    m.VENUE_BALANCE_AVAILABLE.labels(venue=venue_name).set(available)
                    m.VENUE_BALANCE_TOTAL.labels(venue=venue_name).set(total)
                    m.VENUE_MARGIN_USED.labels(venue=venue_name).set(margin_used)
                    if total > 0:
                        m.VENUE_MARGIN_RATIO.labels(venue=venue_name).set(available / total)
                except Exception as e:
                    logger.debug("metrics.balance_error", venue=venue_name, error=str(e))

                # Orderbook data for top 3 symbols
                for symbol in symbols[:3]:
                    try:
                        norm_sym = adapter.normalize_symbol(symbol)
                        t0 = time.monotonic()
                        ob = await adapter.get_orderbook(norm_sym, depth=10)
                        m.VENUE_API_LATENCY.labels(venue=venue_name, endpoint="get_orderbook").observe(
                            time.monotonic() - t0
                        )
                        m.ORDERBOOK_SPREAD_BPS.labels(venue=venue_name, symbol=symbol).set(ob.spread_bps)
                        depth = ob.depth_at_pct(1.0)
                        m.ORDERBOOK_BID_DEPTH_USD.labels(venue=venue_name, symbol=symbol).set(depth["bid_depth"])
                        m.ORDERBOOK_ASK_DEPTH_USD.labels(venue=venue_name, symbol=symbol).set(depth["ask_depth"])
                        if ob.asks:
                            oi_rate = collector.latest_rates.get((venue_name, symbol))
                            if oi_rate and hasattr(oi_rate, 'mark_price') and oi_rate.mark_price:
                                pass  # OI set above from funding rate
                    except Exception:
                        pass

            # Per-venue position counts and exposure
            venue_notional = {}
            venue_pos_count = {}
            venue_funding = {}
            for hedge in self.portfolio.positions:
                for leg, venue_attr in [(hedge.short_leg, "short_leg"), (hedge.long_leg, "long_leg")]:
                    v = leg.venue
                    venue_notional[v] = venue_notional.get(v, 0) + leg.size_usd
                    venue_pos_count[v] = venue_pos_count.get(v, 0) + 1
                    venue_funding[v] = venue_funding.get(v, 0) + leg.funding_accrued

                # Per-position detailed metrics
                m.POSITION_FUNDING_ACCRUED.labels(
                    hedge_id=hedge.id, symbol=hedge.symbol,
                    short_venue=hedge.short_leg.venue, long_venue=hedge.long_leg.venue,
                ).set(hedge.total_funding_accrued)
                m.POSITION_SIZE.labels(
                    hedge_id=hedge.id, symbol=hedge.symbol,
                    short_venue=hedge.short_leg.venue, long_venue=hedge.long_leg.venue,
                ).set(hedge.net_size_usd)
                m.POSITION_PNL.labels(
                    hedge_id=hedge.id, symbol=hedge.symbol,
                ).set(hedge.net_unrealized_pnl)

                for side_label, leg in [("short", hedge.short_leg), ("long", hedge.long_leg)]:
                    m.POSITION_LEVERAGE.labels(
                        hedge_id=hedge.id, symbol=hedge.symbol,
                        venue=leg.venue, side=side_label,
                    ).set(leg.leverage)
                    m.POSITION_LIQUIDATION_PRICE.labels(
                        hedge_id=hedge.id, symbol=hedge.symbol,
                        venue=leg.venue, side=side_label,
                    ).set(leg.liquidation_price)
                    m.POSITION_UNREALIZED_PNL.labels(
                        hedge_id=hedge.id, symbol=hedge.symbol,
                        venue=leg.venue, side=side_label,
                    ).set(leg.unrealized_pnl)
                    m.POSITION_ENTRY_PRICE.labels(
                        hedge_id=hedge.id, symbol=hedge.symbol,
                        venue=leg.venue, side=side_label,
                    ).set(leg.entry_price)
                    m.POSITION_MARK_PRICE.labels(
                        hedge_id=hedge.id, symbol=hedge.symbol,
                        venue=leg.venue, side=side_label,
                    ).set(leg.mark_price)

            nav = self.portfolio.total_nav or 1
            for v in connected:
                notional = venue_notional.get(v, 0)
                m.VENUE_NOTIONAL_USD.labels(venue=v).set(notional)
                m.VENUE_POSITION_COUNT.labels(venue=v).set(venue_pos_count.get(v, 0))
                m.VENUE_EXPOSURE_PCT.labels(venue=v).set((notional / nav) * 100 if nav > 0 else 0)
                m.VENUE_FUNDING_COLLECTED.labels(venue=v).set(venue_funding.get(v, 0))

            # 2. Find opportunities
            opps = collector.latest_opportunities
            m.OPPORTUNITIES_FOUND.set(len(opps))
            for opp in opps[:8]:
                m.BEST_SPREAD.labels(
                    symbol=opp.symbol,
                    short_venue=opp.short_venue,
                    long_venue=opp.long_venue,
                ).set(opp.spread_annual)

            if not opps:
                logger.info("bot.no_opportunities")
                m.CYCLE_COUNT.inc()
                m.CYCLE_DURATION.observe(time.monotonic() - cycle_start)
                return

            # 3. Evaluate top opportunities
            for opp in opps[:3]:
                action = TradeAction(
                    action_type=ActionType.ENTER_HEDGE,
                    symbol=opp.symbol,
                    short_venue=opp.short_venue,
                    long_venue=opp.long_venue,
                    size_usd=min(self.portfolio.total_nav * 0.15, 25000),
                    expected_annual_yield=opp.spread_annual,
                    confidence=0.7,
                    reasoning=f"Spread {opp.spread_pct:.1f}%",
                )

                decision, checks = risk_engine.evaluate_trade(action, self.portfolio)
                m.TRADE_DECISIONS.labels(decision=decision.value).inc()
                logger.info("bot.risk_eval", symbol=opp.symbol, decision=decision.value,
                            spread=f"{opp.spread_pct:.1f}%")

                if decision in (RiskDecision.APPROVE, RiskDecision.RESIZE):
                    if mode == "OBSERVE_ONLY":
                        logger.info("bot.would_execute", action=action.model_dump())
                    elif mode == "FULL_AUTO":
                        logger.info("bot.executing", action=action.model_dump())
                        # Execute here...
                    else:
                        logger.info("bot.awaiting_approval", action=action.model_dump())

                self.decision_log.append({
                    "cycle": self.cycle_count,
                    "symbol": opp.symbol,
                    "spread": opp.spread_pct,
                    "decision": decision.value,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })

            # Update portfolio metrics
            m.PORTFOLIO_NAV.set(self.portfolio.total_nav)
            m.PORTFOLIO_PNL.set(getattr(self.portfolio, "total_pnl", 0))
            m.PORTFOLIO_FUNDING_COLLECTED.set(self.portfolio.total_funding_collected)
            m.ACTIVE_POSITIONS.set(len(self.portfolio.positions))
            m.RISK_DRAWDOWN_PCT.set(self.portfolio.drawdown_pct)
            m.CYCLE_COUNT.inc()
            m.CYCLE_DURATION.observe(time.monotonic() - cycle_start)

    engine = BotEngine()
    interval = configs["strategy"].get("strategy_eval_interval_sec", 60)

    # Also run circuit breaker in background
    cb_task = asyncio.create_task(
        circuit_breaker.monitor_loop(lambda: engine.portfolio, interval=5)
    )

    logger.info("bot.started", mode=mode, interval=interval, venues=list(connected.keys()))

    try:
        while True:
            if circuit_breaker.triggered:
                logger.warning("bot.circuit_breaker_active")
                await asyncio.sleep(30)
                continue
            try:
                await engine.run_cycle()
            except Exception as e:
                logger.error("bot.cycle_error", error=str(e), cycle=engine.cycle_count)
            await asyncio.sleep(interval)
    except KeyboardInterrupt:
        logger.info("bot.stopped")
    finally:
        cb_task.cancel()
        await coinglass.close()
        await defillama.close()


def main():
    parser = argparse.ArgumentParser(description="HedgeHog — DeFi Funding Rate Hedge Bot")
    parser.add_argument("--mode", choices=["OBSERVE_ONLY", "SUPERVISED", "SEMI_AUTO", "FULL_AUTO"],
                        default="OBSERVE_ONLY", help="Operating mode")
    parser.add_argument("--score-only", action="store_true", help="Score venues and exit")
    parser.add_argument("--collect-only", action="store_true", help="Collect rates continuously")
    parser.add_argument("--config-dir", default="config", help="Config directory path")
    parser.add_argument("--metrics-port", type=int, default=8000, help="Prometheus metrics port")
    args = parser.parse_args()

    # Start Prometheus metrics server
    from services.monitoring.metrics import start_metrics_server
    start_metrics_server(args.metrics_port)
    logger.info("metrics.started", port=args.metrics_port)

    configs = load_config(args.config_dir)
    configs["strategy"]["mode"] = args.mode

    print("""
    ╔══════════════════════════════════════════════╗
    ║          AEGIS PROTOCOL v1.0                 ║
    ║   DeFi Funding Rate Hedge Bot                ║
    ║                                              ║
    ║   Venues: Hyperliquid · Lighter · Aster      ║
    ║           Drift · dYdX · ApeX · Paradex      ║
    ║           Ethereal · Injective               ║
    ╚══════════════════════════════════════════════╝
    """)

    if args.score_only:
        asyncio.run(run_score_only(configs))
    elif args.collect_only:
        asyncio.run(run_collect_only(configs))
    else:
        asyncio.run(run_full_bot(configs, args.mode))


if __name__ == "__main__":
    main()
