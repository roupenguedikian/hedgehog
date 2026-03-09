"""
hedgehog/services/capital/execution_engine.py
Handles atomic perp-perp hedge entry, exit, and rotation.

Key DeFi challenges:
- Different chains = no cross-chain atomicity
- Must minimize time between legs
- Rollback on partial fills
- Slippage control
- Gas cost tracking
"""
from __future__ import annotations

import asyncio
import structlog

from models.core import (
    TradeAction, ActionType, HedgePosition, Position,
    OrderResult, OrderStatus, Side,
)

logger = structlog.get_logger()

MAX_SLIPPAGE_BPS = 20
MAX_LEG_IMBALANCE_PCT = 2
ORDER_TIMEOUT_SECONDS = 30


class ExecutionEngine:
    """
    Executes perp-perp hedge trades across DeFi venues.

    Entry: Short on high-funding venue + Long on low-funding venue
    Exit:  Close both legs
    Rotate: Exit old hedge, enter new one on different venues
    """

    def __init__(self, adapters: dict):
        self.adapters = adapters
        self.active_positions: dict[str, HedgePosition] = {}

    async def execute(self, action: TradeAction) -> dict:
        if action.action_type == ActionType.ENTER_HEDGE:
            return await self.enter_hedge(
                symbol=action.symbol,
                short_venue=action.short_venue,
                long_venue=action.long_venue,
                size_usd=action.size_usd,
            )
        elif action.action_type == ActionType.EXIT_HEDGE:
            return await self.exit_hedge(action.position_id)
        elif action.action_type == ActionType.ROTATE:
            return await self.rotate_hedge(
                old_position_id=action.position_id,
                new_short_venue=action.short_venue,
                new_long_venue=action.long_venue,
            )
        return {"status": "SKIPPED", "reason": "HOLD action — nothing to do"}

    async def enter_hedge(
        self,
        symbol: str,
        short_venue: str,
        long_venue: str,
        size_usd: float,
    ) -> dict:
        short_adapter = self.adapters.get(short_venue)
        long_adapter = self.adapters.get(long_venue)

        if not short_adapter or not short_adapter.connected:
            return {"status": "FAILED", "reason": f"{short_venue} not connected"}
        if not long_adapter or not long_adapter.connected:
            return {"status": "FAILED", "reason": f"{long_venue} not connected"}

        short_symbol = short_adapter.normalize_symbol(symbol)
        long_symbol = long_adapter.normalize_symbol(symbol)

        # Pre-flight: get orderbooks for pricing
        short_ob = await short_adapter.get_orderbook(short_symbol, depth=20)
        long_ob = await long_adapter.get_orderbook(long_symbol, depth=20)

        if not short_ob.bids or not long_ob.asks:
            return {"status": "ABORTED", "reason": "Empty orderbook"}

        # Check margin
        short_balance = await short_adapter.get_balance()
        long_balance = await long_adapter.get_balance()
        required_margin = size_usd * 0.1  # assume 10x leverage

        if short_balance.get("available", 0) < required_margin:
            return {"status": "ABORTED", "reason": f"Insufficient margin on {short_venue}"}
        if long_balance.get("available", 0) < required_margin:
            return {"status": "ABORTED", "reason": f"Insufficient margin on {long_venue}"}

        # Calculate quantity
        short_price = short_ob.bids[0].price
        long_price = long_ob.asks[0].price
        mid_price = (short_price + long_price) / 2
        qty = size_usd / mid_price

        logger.info("execution.entering_hedge",
                     symbol=symbol, qty=qty, short=short_venue, long=long_venue)

        # Execute both legs simultaneously
        short_task = short_adapter.place_limit_order(
            short_symbol, Side.SHORT, qty, short_price, tif="IOC")
        long_task = long_adapter.place_limit_order(
            long_symbol, Side.LONG, qty, long_price, tif="IOC")

        results = await asyncio.gather(short_task, long_task, return_exceptions=True)
        short_result, long_result = results

        short_failed = isinstance(short_result, Exception)
        long_failed = isinstance(long_result, Exception)

        # Both failed
        if short_failed and long_failed:
            logger.error("execution.both_legs_failed",
                         short_err=str(short_result), long_err=str(long_result))
            return {"status": "FAILED", "reason": "Both legs failed"}

        # Partial fill — rollback
        if short_failed or long_failed:
            logger.warning("execution.partial_fill_rollback")
            await self._rollback(
                short_result if not short_failed else None,
                long_result if not long_failed else None,
                short_adapter, long_adapter,
                short_symbol, long_symbol,
            )
            return {"status": "ROLLED_BACK", "reason": "Partial execution"}

        short_fill: OrderResult = short_result
        long_fill: OrderResult = long_result

        if short_fill.status == OrderStatus.FAILED or long_fill.status == OrderStatus.FAILED:
            logger.warning("execution.order_rejected")
            await self._rollback(short_fill, long_fill,
                                 short_adapter, long_adapter,
                                 short_symbol, long_symbol)
            return {"status": "ROLLED_BACK", "reason": "Order rejected by venue"}

        # Verify leg balance
        if short_fill.filled_qty > 0 and long_fill.filled_qty > 0:
            imbalance = (abs(short_fill.filled_qty - long_fill.filled_qty)
                         / max(short_fill.filled_qty, long_fill.filled_qty))
            if imbalance > MAX_LEG_IMBALANCE_PCT / 100:
                logger.warning("execution.leg_imbalance", pct=imbalance * 100)
                await self._rebalance_legs(
                    short_adapter, long_adapter,
                    short_symbol, long_symbol,
                    short_fill, long_fill,
                )

        # Gas costs
        short_gas = short_adapter.estimate_gas_cost("trade")
        long_gas = long_adapter.estimate_gas_cost("trade")

        # Build position record
        hedge = HedgePosition(
            symbol=symbol,
            short_leg=Position(
                venue=short_venue, symbol=symbol, side=Side.SHORT,
                size=short_fill.filled_qty,
                size_usd=short_fill.filled_qty * (short_fill.avg_price or short_price),
                entry_price=short_fill.avg_price or short_price,
                mark_price=short_fill.avg_price or short_price,
                margin=required_margin,
                leverage=size_usd / required_margin,
            ),
            long_leg=Position(
                venue=long_venue, symbol=symbol, side=Side.LONG,
                size=long_fill.filled_qty,
                size_usd=long_fill.filled_qty * (long_fill.avg_price or long_price),
                entry_price=long_fill.avg_price or long_price,
                mark_price=long_fill.avg_price or long_price,
                margin=required_margin,
                leverage=size_usd / required_margin,
            ),
            total_fees=short_fill.fee + long_fill.fee,
            total_gas=short_gas + long_gas,
        )

        self.active_positions[hedge.id] = hedge
        logger.info("execution.hedge_opened",
                     id=hedge.id,
                     basis=hedge.entry_basis,
                     fees=hedge.total_fees)

        return {
            "status": "FILLED",
            "position_id": hedge.id,
            "short_fill": {
                "venue": short_venue, "qty": short_fill.filled_qty,
                "price": short_fill.avg_price, "fees": short_fill.fee,
            },
            "long_fill": {
                "venue": long_venue, "qty": long_fill.filled_qty,
                "price": long_fill.avg_price, "fees": long_fill.fee,
            },
            "entry_basis": hedge.entry_basis,
            "total_cost": hedge.total_fees + hedge.total_gas,
        }

    async def exit_hedge(self, position_id: str) -> dict:
        hedge = self.active_positions.get(position_id)
        if not hedge:
            return {"status": "FAILED", "reason": f"Position {position_id} not found"}

        short_venue = hedge.short_leg.venue
        long_venue = hedge.long_leg.venue
        short_adapter = self.adapters.get(short_venue)
        long_adapter = self.adapters.get(long_venue)

        short_symbol = short_adapter.normalize_symbol(hedge.symbol)
        long_symbol = long_adapter.normalize_symbol(hedge.symbol)

        logger.info("execution.exiting_hedge", id=position_id)

        # Close both legs: buy to cover short, sell to close long
        short_task = short_adapter.place_market_order(
            short_symbol, Side.LONG, hedge.short_leg.size, reduce_only=True)
        long_task = long_adapter.place_market_order(
            long_symbol, Side.SHORT, hedge.long_leg.size, reduce_only=True)

        results = await asyncio.gather(short_task, long_task, return_exceptions=True)
        exit_short, exit_long = results

        exit_fees = 0.0
        if not isinstance(exit_short, Exception):
            exit_fees += exit_short.fee
        if not isinstance(exit_long, Exception):
            exit_fees += exit_long.fee

        exit_gas = (short_adapter.estimate_gas_cost("trade")
                    + long_adapter.estimate_gas_cost("trade"))

        hedge.total_fees += exit_fees
        hedge.total_gas += exit_gas

        net_pnl = (hedge.short_leg.unrealized_pnl + hedge.long_leg.unrealized_pnl
                    + hedge.total_funding_accrued - hedge.total_fees - hedge.total_gas)

        del self.active_positions[position_id]
        logger.info("execution.hedge_closed", id=position_id, net_pnl=net_pnl)

        return {
            "status": "CLOSED",
            "position_id": position_id,
            "net_pnl": net_pnl,
            "total_funding": hedge.total_funding_accrued,
            "total_fees": hedge.total_fees,
            "total_gas": hedge.total_gas,
        }

    async def rotate_hedge(
        self, old_position_id: str, new_short_venue: str, new_long_venue: str,
    ) -> dict:
        old_hedge = self.active_positions.get(old_position_id)
        if not old_hedge:
            return {"status": "FAILED", "reason": "Old position not found"}

        logger.info("execution.rotating", id=old_position_id,
                     to_short=new_short_venue, to_long=new_long_venue)

        exit_result = await self.exit_hedge(old_position_id)
        if exit_result["status"] != "CLOSED":
            return {"status": "FAILED", "reason": f"Exit failed: {exit_result}"}

        entry_result = await self.enter_hedge(
            symbol=old_hedge.symbol,
            short_venue=new_short_venue,
            long_venue=new_long_venue,
            size_usd=old_hedge.net_size_usd,
        )

        return {"status": "ROTATED", "old_exit": exit_result, "new_entry": entry_result}

    async def update_positions(self):
        for pid, hedge in self.active_positions.items():
            try:
                short_adapter = self.adapters.get(hedge.short_leg.venue)
                long_adapter = self.adapters.get(hedge.long_leg.venue)

                if short_adapter and short_adapter.connected:
                    price = await short_adapter.get_mark_price(
                        short_adapter.normalize_symbol(hedge.symbol))
                    hedge.short_leg.mark_price = price
                    hedge.short_leg.unrealized_pnl = (
                        (hedge.short_leg.entry_price - price) * hedge.short_leg.size)

                if long_adapter and long_adapter.connected:
                    price = await long_adapter.get_mark_price(
                        long_adapter.normalize_symbol(hedge.symbol))
                    hedge.long_leg.mark_price = price
                    hedge.long_leg.unrealized_pnl = (
                        (price - hedge.long_leg.entry_price) * hedge.long_leg.size)
            except Exception as e:
                logger.warning("execution.update_failed", id=pid, error=str(e))

    # ── Internal helpers ─────────────────────────────────────────────────

    async def _rollback(self, short_result, long_result,
                        short_adapter, long_adapter,
                        short_symbol, long_symbol):
        try:
            if (short_result and isinstance(short_result, OrderResult)
                    and short_result.filled_qty > 0):
                logger.info("execution.rollback_short", qty=short_result.filled_qty)
                await short_adapter.place_market_order(
                    short_symbol, Side.LONG, short_result.filled_qty, reduce_only=True)

            if (long_result and isinstance(long_result, OrderResult)
                    and long_result.filled_qty > 0):
                logger.info("execution.rollback_long", qty=long_result.filled_qty)
                await long_adapter.place_market_order(
                    long_symbol, Side.SHORT, long_result.filled_qty, reduce_only=True)
        except Exception as e:
            logger.error("execution.rollback_failed", error=str(e))

    async def _rebalance_legs(self, short_adapter, long_adapter,
                              short_symbol, long_symbol,
                              short_fill: OrderResult, long_fill: OrderResult):
        diff = abs(short_fill.filled_qty - long_fill.filled_qty)
        if short_fill.filled_qty > long_fill.filled_qty:
            await long_adapter.place_market_order(long_symbol, Side.LONG, diff)
        else:
            await short_adapter.place_market_order(short_symbol, Side.SHORT, diff)
