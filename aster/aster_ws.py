"""
hedgehog/adapters/aster_ws.py
WebSocket client for Aster DEX real-time data streams.

Official docs: https://docs.asterdex.com/product/aster-perpetuals/api/api-documentation

Connection rules (from docs):
  - Base URL: wss://fstream.asterdex.com
  - Raw stream: /ws/<streamName>
  - Combined stream: /stream?streams=<stream1>/<stream2>/<stream3>
  - All stream names MUST be lowercase (including symbols)
  - Max 200 streams per connection
  - Max 10 incoming messages/second
  - Connection valid for 24 hours, auto-disconnect at the 24h mark
  - Server sends ping every 5 min; must reply pong within 15 min
  - Unsolicited pong frames are allowed

Available market streams:
  - <symbol>@aggTrade          (100ms)  — Aggregated trades
  - <symbol>@markPrice         (3000ms) — Mark price + funding rate
  - <symbol>@markPrice@1s      (1000ms) — Mark price (fast)
  - !markPrice@arr             (3000ms) — All symbols mark price
  - !markPrice@arr@1s          (1000ms) — All symbols mark price (fast)
  - <symbol>@kline_<interval>  (250ms)  — Kline/candlestick
  - <symbol>@miniTicker        (500ms)  — Mini ticker
  - !miniTicker@arr            (1000ms) — All mini tickers
  - <symbol>@ticker            (500ms)  — Full ticker
  - !ticker@arr                (1000ms) — All tickers
  - <symbol>@bookTicker        (realtime) — Best bid/ask
  - !bookTicker                (realtime) — All book tickers
  - <symbol>@forceOrder        (1000ms) — Liquidation events
  - !forceOrder@arr            (1000ms) — All liquidations
  - <symbol>@depth<levels>     (250ms)  — Partial book (5/10/20 levels)
  - <symbol>@depth<levels>@100ms        — Partial book (fast)
  - <symbol>@depth             (250ms)  — Diff depth stream

User data stream:
  - /ws/<listenKey>  — requires listenKey from POST /fapi/v1/listenKey
  - Events: ACCOUNT_UPDATE, ORDER_TRADE_UPDATE, ACCOUNT_CONFIG_UPDATE,
            MARGIN_CALL, listenKeyExpired
  - Payloads NOT guaranteed in order during heavy periods; sort by E field
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Callable, Optional

import structlog

logger = structlog.get_logger()

ASTER_WS_BASE = "wss://fstream.asterdex.com"
MAX_STREAMS_PER_CONNECTION = 200
CONNECTION_LIFETIME_SEC = 23 * 3600  # reconnect before 24h hard limit


class AsterWebSocket:
    """
    Async WebSocket client for Aster market and user data streams.

    Usage:
        ws = AsterWebSocket()
        await ws.connect()
        await ws.subscribe_mark_price("btcusdt", callback=on_funding)
        await ws.subscribe_book_ticker("ethusdt", callback=on_book)
        await ws.subscribe_user_stream(listen_key, callback=on_account)
        await ws.run_forever()
    """

    def __init__(self, base_url: str = ASTER_WS_BASE):
        self.base_url = base_url
        self._ws = None
        self._callbacks: dict[str, Callable] = {}
        self._subscribed_streams: set[str] = set()
        self._running = False
        self._connect_time: float = 0
        self._msg_id = 0

    async def connect(self, streams: list[str] | None = None):
        """
        Open a WebSocket connection.

        If streams provided, connects to combined stream URL.
        Otherwise, connects to base and subscribes later.
        """
        try:
            import websockets
        except ImportError:
            raise ImportError("pip install websockets")

        if streams:
            url = f"{self.base_url}/stream?streams={'/'.join(streams)}"
        else:
            url = f"{self.base_url}/ws"

        self._ws = await websockets.connect(url, ping_interval=None)
        self._connect_time = time.time()
        self._running = True
        logger.info("aster_ws.connected", url=url)

    async def subscribe(self, streams: list[str], callback: Callable):
        """
        Live-subscribe to streams on an existing connection.

        Per docs, send:
          {"method": "SUBSCRIBE", "params": [...streams], "id": N}
        Response: {"result": null, "id": N} on success.
        """
        if len(self._subscribed_streams) + len(streams) > MAX_STREAMS_PER_CONNECTION:
            logger.error("aster_ws.max_streams_exceeded",
                         current=len(self._subscribed_streams),
                         requested=len(streams))
            return

        self._msg_id += 1
        msg = {
            "method": "SUBSCRIBE",
            "params": streams,
            "id": self._msg_id,
        }
        await self._ws.send(json.dumps(msg))

        for s in streams:
            self._callbacks[s] = callback
            self._subscribed_streams.add(s)

        logger.info("aster_ws.subscribed", streams=streams)

    async def unsubscribe(self, streams: list[str]):
        """Live-unsubscribe from streams."""
        self._msg_id += 1
        msg = {
            "method": "UNSUBSCRIBE",
            "params": streams,
            "id": self._msg_id,
        }
        await self._ws.send(json.dumps(msg))

        for s in streams:
            self._callbacks.pop(s, None)
            self._subscribed_streams.discard(s)

    # ── Convenience subscription methods ─────────────────────────

    async def subscribe_mark_price(self, symbol: str, callback: Callable,
                                    fast: bool = True):
        """
        Mark price + funding rate stream.
        fast=True → 1s updates, False → 3s updates.

        Payload includes: markPrice, indexPrice, estimatedSettlePrice,
        lastFundingRate, nextFundingTime, interestRate.
        Critical for real-time funding rate monitoring.
        """
        stream = f"{symbol.lower()}@markPrice"
        if fast:
            stream += "@1s"
        await self.subscribe([stream], callback)

    async def subscribe_all_mark_prices(self, callback: Callable,
                                         fast: bool = True):
        """All symbols mark price + funding. Ideal for cross-venue scanning."""
        stream = "!markPrice@arr"
        if fast:
            stream += "@1s"
        await self.subscribe([stream], callback)

    async def subscribe_book_ticker(self, symbol: str, callback: Callable):
        """
        Real-time best bid/ask. Zero latency — pushed on every change.
        Payload: {s: symbol, b: bestBidPrice, B: bestBidQty,
                  a: bestAskPrice, A: bestAskQty, T: timestamp}
        """
        await self.subscribe([f"{symbol.lower()}@bookTicker"], callback)

    async def subscribe_depth(self, symbol: str, callback: Callable,
                               levels: int = 20, speed_ms: int = 100):
        """
        Partial book depth. Valid levels: 5, 10, 20.
        Valid speeds: 100, 250, 500 ms.
        """
        stream = f"{symbol.lower()}@depth{levels}"
        if speed_ms != 250:
            stream += f"@{speed_ms}ms"
        await self.subscribe([stream], callback)

    async def subscribe_agg_trades(self, symbol: str, callback: Callable):
        """Aggregated trades, pushed every 100ms."""
        await self.subscribe([f"{symbol.lower()}@aggTrade"], callback)

    async def subscribe_liquidations(self, callback: Callable,
                                      symbol: str | None = None):
        """
        Liquidation events. Per symbol or all markets.
        Only the latest liquidation per symbol within 1000ms is pushed.
        """
        if symbol:
            stream = f"{symbol.lower()}@forceOrder"
        else:
            stream = "!forceOrder@arr"
        await self.subscribe([stream], callback)

    async def subscribe_user_stream(self, listen_key: str, callback: Callable):
        """
        Connect to user data stream.

        Events:
          - ACCOUNT_UPDATE: balance/position changes, funding fee events
            FUNDING_FEE in crossed → only balance B, no position P
            FUNDING_FEE in isolated → balance B + affected position P
          - ORDER_TRADE_UPDATE: order created, filled, cancelled, expired
          - ACCOUNT_CONFIG_UPDATE: leverage change, multi-asset toggle
          - MARGIN_CALL: position risk ratio too high
          - listenKeyExpired: must re-create the key

        Important: payloads NOT in order during volatile periods; use E field.
        """
        try:
            import websockets
        except ImportError:
            raise ImportError("pip install websockets")

        url = f"{self.base_url}/ws/{listen_key}"
        self._ws = await websockets.connect(url, ping_interval=None)
        self._callbacks["__user_stream__"] = callback
        self._connect_time = time.time()
        self._running = True
        logger.info("aster_ws.user_stream_connected")

    # ── Event loop ───────────────────────────────────────────────

    async def run_forever(self):
        """
        Main receive loop. Handles:
          - Dispatching events to registered callbacks
          - Pong replies to server pings
          - Auto-reconnect before 24h connection timeout
        """
        while self._running:
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=300)
                data = json.loads(raw)

                # Combined stream wraps payloads in {"stream": ..., "data": ...}
                if "stream" in data:
                    stream_name = data["stream"]
                    payload = data["data"]
                else:
                    # Raw stream or user data
                    stream_name = data.get("e", "__user_stream__")
                    payload = data

                # Dispatch to callback
                callback = self._callbacks.get(
                    stream_name,
                    self._callbacks.get("__user_stream__"),
                )
                if callback:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(payload)
                    else:
                        callback(payload)

                # Check connection age — reconnect before 24h limit
                if time.time() - self._connect_time > CONNECTION_LIFETIME_SEC:
                    logger.info("aster_ws.preemptive_reconnect")
                    await self._reconnect()

            except asyncio.TimeoutError:
                # Send unsolicited pong to keep connection alive
                try:
                    await self._ws.pong()
                except Exception:
                    await self._reconnect()
            except Exception as e:
                logger.error("aster_ws.error", error=str(e))
                if self._running:
                    await asyncio.sleep(1)
                    await self._reconnect()

    async def _reconnect(self):
        """Re-establish connection and resubscribe."""
        try:
            if self._ws:
                await self._ws.close()
        except Exception:
            pass

        streams = list(self._subscribed_streams)
        if streams:
            await self.connect(streams)
        else:
            await self.connect()

    async def close(self):
        """Gracefully close the connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
        logger.info("aster_ws.closed")
