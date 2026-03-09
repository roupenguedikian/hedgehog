"""
hedgehog/services/risk/circuit_breaker.py
Non-AI safety layer. Runs independently of all agents.
Can force-close all positions when hard limits are breached.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import structlog
from models.core import PortfolioSnapshot

logger = structlog.get_logger()


class CircuitBreaker:
    def __init__(self, config: dict, adapters: dict, alerter=None):
        self.max_loss_usd = config.get("max_loss_usd", 10000)
        self.max_drawdown_pct = config.get("max_drawdown_pct", 7.0)
        self.adapters = adapters
        self.alerter = alerter
        self.triggered = False
        self.initial_nav: float = 0

    def set_initial_nav(self, nav: float):
        self.initial_nav = nav

    async def check(self, portfolio: PortfolioSnapshot) -> bool:
        """Returns True if circuit breaker triggers."""
        if self.triggered:
            return True

        # Absolute loss check
        if self.initial_nav > 0:
            loss = self.initial_nav - portfolio.total_nav
            if loss > self.max_loss_usd:
                await self._trigger(f"Absolute loss ${loss:.2f} exceeds ${self.max_loss_usd}")
                return True

        # Drawdown check
        if portfolio.drawdown_pct > self.max_drawdown_pct:
            await self._trigger(f"Drawdown {portfolio.drawdown_pct:.2f}% exceeds {self.max_drawdown_pct}%")
            return True

        return False

    async def _trigger(self, reason: str):
        self.triggered = True
        logger.critical("CIRCUIT_BREAKER_TRIGGERED", reason=reason)
        if self.alerter:
            await self.alerter.send_alert(f"🚨 CIRCUIT BREAKER: {reason}")
        await self.emergency_close_all()

    async def emergency_close_all(self):
        """Force-close all positions across all venues."""
        logger.critical("circuit_breaker.closing_all_positions")
        for venue_name, adapter in self.adapters.items():
            try:
                cancelled = await adapter.cancel_all_orders()
                positions = await adapter.get_positions()
                for pos in positions:
                    from models.core import Side
                    close_side = Side.LONG if pos.side == Side.SHORT else Side.SHORT
                    await adapter.place_market_order(pos.symbol, close_side, pos.size, reduce_only=True)
                logger.info("circuit_breaker.venue_closed", venue=venue_name,
                            cancelled=cancelled, positions=len(positions))
            except Exception as e:
                logger.error("circuit_breaker.close_failed", venue=venue_name, error=str(e))

    async def monitor_loop(self, get_portfolio, interval: int = 5):
        """Continuous monitoring loop."""
        while not self.triggered:
            try:
                portfolio = get_portfolio()
                await self.check(portfolio)
            except Exception as e:
                logger.error("circuit_breaker.monitor_error", error=str(e))
            await asyncio.sleep(interval)

    def reset(self):
        """Manual reset after human review."""
        self.triggered = False
        logger.warning("circuit_breaker.reset")
