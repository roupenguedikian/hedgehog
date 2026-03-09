"""
hedgehog/agents/tools/funding_tools.py
LangChain tools that agents can call to access market data.
"""
from __future__ import annotations
from typing import Optional


def build_funding_tools(collector, scorer, adapters, venue_configs):
    """
    Build LangChain tools that the strategist agent can invoke.
    These are plain functions wrapped as tools — the agent calls them via tool_use.
    """

    def get_funding_rate_matrix() -> str:
        """
        Get current funding rates across ALL venues for ALL symbols.
        Returns a matrix of annualized funding rates (as percentages).
        """
        matrix = collector.get_rate_matrix()
        lines = ["FUNDING RATE MATRIX (annualized %):", ""]
        for symbol, venues in sorted(matrix.items()):
            parts = [f"  {v}: {r:+.2f}%" for v, r in sorted(venues.items(), key=lambda x: -x[1])]
            lines.append(f"{symbol}:")
            lines.extend(parts)
            lines.append("")
        return "\n".join(lines)

    def get_top_opportunities(n: int = 10) -> str:
        """
        Get the top funding rate arbitrage opportunities (venue pairs with highest spread).
        Returns opportunities sorted by annualized spread.
        """
        opps = collector.latest_opportunities[:n]
        if not opps:
            return "No opportunities found above minimum spread threshold."

        lines = ["TOP FUNDING RATE OPPORTUNITIES:", ""]
        for i, opp in enumerate(opps):
            lines.append(
                f"{i+1}. {opp.symbol}: SHORT {opp.short_venue} ({opp.short_rate_annual*100:+.2f}% ann) "
                f"/ LONG {opp.long_venue} ({opp.long_rate_annual*100:+.2f}% ann) "
                f"= SPREAD {opp.spread_annual*100:.2f}%"
            )
        return "\n".join(lines)

    def get_venue_scores(symbol: str = "BTC") -> str:
        """
        Get venue scores for a specific symbol.
        Shows composite score, funding rate, fees, liquidity, and cycle.
        """
        top = scorer.get_top_venues(symbol, n=15)
        if not top:
            return f"No scores available for {symbol}."

        lines = [f"VENUE SCORES FOR {symbol}:", ""]
        for i, s in enumerate(top):
            lines.append(
                f"{i+1}. {s.venue:<15} score={s.composite_score:.4f} "
                f"rate={s.avg_funding_rate_30d*100:+.2f}%ann "
                f"fee={s.trading_fee_bps:.1f}bps "
                f"cycle={s.funding_cycle_hours}h "
                f"liq=${s.liquidity_depth_1pct_usd:,.0f}"
            )
        return "\n".join(lines)

    def get_best_pair(symbol: str = "BTC") -> str:
        """
        Get the best short+long venue pair for a symbol based on scores and rates.
        """
        pair = scorer.get_best_pair(symbol)
        if not pair:
            return f"Cannot determine best pair for {symbol} — insufficient data."

        return (
            f"BEST PAIR FOR {symbol}:\n"
            f"  SHORT: {pair['short_venue']} (rate={pair['short_rate_annual']*100:+.2f}%ann, score={pair['short_score']:.4f})\n"
            f"  LONG:  {pair['long_venue']} (rate={pair['long_rate_annual']*100:+.2f}%ann, score={pair['long_score']:.4f})\n"
            f"  SPREAD: {pair['spread_pct']:.2f}% annualized"
        )

    def get_venue_funding_history(venue: str, symbol: str, hours: int = 24) -> str:
        """
        Get funding rate statistics for a specific venue+symbol over recent hours.
        Shows mean, std, min, max, and number of sign flips.
        """
        stats = collector.get_historical_stats(venue, symbol, hours)
        cycle = venue_configs.get(venue)
        cycle_h = cycle.funding_cycle_hours if cycle else 8
        ann_mean = stats["mean"] * (8760 / cycle_h) * 100

        return (
            f"FUNDING STATS for {venue}/{symbol} (last {hours}h):\n"
            f"  Mean rate: {stats['mean']*100:.4f}% per cycle ({ann_mean:.2f}% annualized)\n"
            f"  Std dev:   {stats['std']*100:.4f}%\n"
            f"  Min:       {stats['min']*100:.4f}%\n"
            f"  Max:       {stats['max']*100:.4f}%\n"
            f"  Samples:   {stats['count']}\n"
            f"  Flips:     {stats['flip_count']} (rate changed sign)"
        )

    def estimate_net_yield(
        short_venue: str, long_venue: str, symbol: str, size_usd: float = 10000, hold_days: int = 7
    ) -> str:
        """
        Estimate net yield for a hedge after ALL costs (fees, gas, opportunity cost).
        """
        short_key = (short_venue, symbol)
        long_key = (long_venue, symbol)

        short_rate = collector.latest_rates.get(short_key)
        long_rate = collector.latest_rates.get(long_key)

        if not short_rate or not long_rate:
            return f"Missing rate data for {short_venue}/{symbol} or {long_venue}/{symbol}"

        short_cfg = venue_configs.get(short_venue)
        long_cfg = venue_configs.get(long_venue)
        if not short_cfg or not long_cfg:
            return "Missing venue config"

        # Gross spread
        gross_annual = short_rate.annualized - long_rate.annualized

        # Trading fees (entry + exit on both legs)
        short_fee = (short_cfg.taker_fee_bps / 10000) * 2  # entry + exit
        long_fee = (long_cfg.taker_fee_bps / 10000) * 2
        total_fee = short_fee + long_fee

        # Gas costs
        short_gas = adapters[short_venue].estimate_gas_cost("trade") * 2 / size_usd if short_venue in adapters else 0
        long_gas = adapters[long_venue].estimate_gas_cost("trade") * 2 / size_usd if long_venue in adapters else 0

        # Amortize fixed costs over hold period
        fixed_cost_annual = (total_fee + short_gas + long_gas) * (365 / hold_days)

        net_annual = gross_annual - fixed_cost_annual
        breakeven_days = (total_fee + short_gas + long_gas) / (gross_annual / 365) if gross_annual > 0 else float("inf")

        # Ethereal bonus
        ethereal_bonus = ""
        if short_venue == "ethereal" or long_venue == "ethereal":
            ethereal_bonus = "\n  ⚡ Ethereal bonus: USDe collateral earns ~15-25% APY on top of funding"

        return (
            f"NET YIELD ESTIMATE: {short_venue}(short) / {long_venue}(long) on {symbol}\n"
            f"  Gross spread:     {gross_annual*100:+.2f}% annual\n"
            f"  Trading fees:     {total_fee*100:.4f}% round-trip\n"
            f"  Gas costs:        ${(short_gas + long_gas) * size_usd:.4f}\n"
            f"  Net annual yield: {net_annual*100:+.2f}%\n"
            f"  Breakeven:        {breakeven_days:.1f} days\n"
            f"  On ${size_usd:,.0f} for {hold_days}d: ${size_usd * net_annual * hold_days / 365:.2f}"
            f"{ethereal_bonus}"
        )

    # Return as dict of tool name -> function
    return {
        "get_funding_rate_matrix": get_funding_rate_matrix,
        "get_top_opportunities": get_top_opportunities,
        "get_venue_scores": get_venue_scores,
        "get_best_pair": get_best_pair,
        "get_venue_funding_history": get_venue_funding_history,
        "estimate_net_yield": estimate_net_yield,
    }
