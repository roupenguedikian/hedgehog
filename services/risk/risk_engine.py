"""
hedgehog/services/risk/risk_engine.py
Risk evaluation for proposed trades against portfolio limits.
"""
from __future__ import annotations

import structlog
from models.core import RiskDecision

logger = structlog.get_logger()


class RiskEngine:
    """
    Evaluates proposed TradeActions against risk limits from config/risk.yaml.
    Returns (RiskDecision, list[RiskCheck]) for each trade.
    """

    def __init__(self, config: dict, venue_configs: dict[str, VenueConfig]):
        self.config = config
        self.venue_configs = venue_configs

    def evaluate_trade(
        self, action: TradeAction, portfolio: PortfolioSnapshot,
    ) -> tuple[RiskDecision, list[RiskCheck]]:
        checks: list[RiskCheck] = []

        # 1. Drawdown
        dd_limit = self.config.get("max_drawdown_pct", 5.0)
        checks.append(RiskCheck(
            name="drawdown",
            status="CRITICAL" if portfolio.drawdown_pct >= dd_limit else "OK",
            value=portfolio.drawdown_pct,
            limit=dd_limit,
            message=f"Drawdown {portfolio.drawdown_pct:.2f}%",
        ))

        # 2. Position size vs NAV
        nav = portfolio.total_nav
        pos_pct = (action.size_usd / nav * 100) if nav > 0 else 100.0
        max_pos_pct = 20.0
        checks.append(RiskCheck(
            name="position_size",
            status="WARNING" if pos_pct > max_pos_pct else "OK",
            value=pos_pct,
            limit=max_pos_pct,
            message=f"Position {pos_pct:.1f}% of NAV",
        ))

        # 3. Venue concentration
        max_venue_pct = self.config.get("max_single_venue_pct", 25.0)
        venue_exposure = self._venue_exposure(portfolio)
        for venue in [action.short_venue, action.long_venue]:
            if not venue:
                continue
            current = venue_exposure.get(venue, 0.0)
            projected = current + pos_pct
            checks.append(RiskCheck(
                name=f"venue_{venue}",
                status="WARNING" if projected > max_venue_pct else "OK",
                value=projected,
                limit=max_venue_pct,
                message=f"{venue} projected {projected:.1f}% of NAV",
            ))

        # 4. Chain concentration
        max_chain_pct = self.config.get("max_single_chain_pct", 40.0)
        chain_exposure = self._chain_exposure(portfolio)
        for venue in [action.short_venue, action.long_venue]:
            vc = self.venue_configs.get(venue)
            if not vc:
                continue
            chain = vc.chain
            current = chain_exposure.get(chain, 0.0)
            projected = current + pos_pct
            if projected > max_chain_pct:
                checks.append(RiskCheck(
                    name=f"chain_{chain}",
                    status="WARNING",
                    value=projected,
                    limit=max_chain_pct,
                    message=f"Chain {chain} projected {projected:.1f}% of NAV",
                ))

        # 5. Tier limits
        tier_limits = self.config.get("tier_limits", {})
        for venue in [action.short_venue, action.long_venue]:
            vc = self.venue_configs.get(venue)
            if not vc:
                continue
            tier = vc.tier.value
            limit = tier_limits.get(tier, 0.35) * 100
            current = venue_exposure.get(venue, 0.0)
            projected = current + pos_pct
            if projected > limit:
                checks.append(RiskCheck(
                    name=f"tier_{venue}",
                    status="WARNING",
                    value=projected,
                    limit=limit,
                    message=f"{venue} ({tier}) would exceed tier limit",
                ))

        # 6. Minimum yield
        min_yield = 0.10
        if action.expected_annual_yield < min_yield:
            checks.append(RiskCheck(
                name="min_yield",
                status="WARNING",
                value=action.expected_annual_yield * 100,
                limit=min_yield * 100,
                message=f"Yield {action.expected_annual_yield*100:.1f}% < {min_yield*100:.0f}%",
            ))

        # 7. In-transit capital
        max_transit = self.config.get("max_in_transit_pct", 10.0)
        transit_pct = (portfolio.in_transit / nav * 100) if nav > 0 else 0
        if transit_pct > max_transit:
            checks.append(RiskCheck(
                name="in_transit",
                status="WARNING",
                value=transit_pct,
                limit=max_transit,
                message=f"In-transit capital {transit_pct:.1f}%",
            ))

        # Decision
        critical = [c for c in checks if c.status == "CRITICAL"]
        warnings = [c for c in checks if c.status == "WARNING"]

        if critical:
            return RiskDecision.HALT, checks
        if any(c.name == "min_yield" for c in warnings):
            return RiskDecision.REJECT, checks
        if any(c.name.startswith(("venue_", "tier_", "chain_")) or c.name == "position_size"
               for c in warnings):
            return RiskDecision.RESIZE, checks

        return RiskDecision.APPROVE, checks

    def _venue_exposure(self, portfolio: PortfolioSnapshot) -> dict[str, float]:
        nav = portfolio.total_nav
        if nav <= 0:
            return {}
        exposure: dict[str, float] = {}
        for pos in portfolio.positions:
            pct = pos.net_size_usd / nav * 100
            for venue in [pos.short_leg.venue, pos.long_leg.venue]:
                exposure[venue] = exposure.get(venue, 0) + pct
        return exposure

    def _chain_exposure(self, portfolio: PortfolioSnapshot) -> dict[str, float]:
        nav = portfolio.total_nav
        if nav <= 0:
            return {}
        exposure: dict[str, float] = {}
        for pos in portfolio.positions:
            pct = pos.net_size_usd / nav * 100
            for venue in [pos.short_leg.venue, pos.long_leg.venue]:
                vc = self.venue_configs.get(venue)
                if vc:
                    chain = vc.chain
                    exposure[chain] = exposure.get(chain, 0) + pct
        return exposure
