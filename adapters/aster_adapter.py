"""
hedgehog/adapters/aster_adapter.py
Full Aster DEX adapter — implements both V1 (HMAC) and V3 (EIP-712) auth.

Official docs:
  - REST: https://fapi.asterdex.com
  - WS:   wss://fstream.asterdex.com
  - V1 API (HMAC SHA256): https://docs.asterdex.com/product/aster-perpetuals/api/api-documentation
  - V3 API (EIP-712):     https://github.com/asterdex/api-docs/blob/master/aster-finance-futures-api-v3.md
  - Python connector:     https://github.com/asterdex/aster-connector-python

Auth overview:
  V1 — API Key + Secret → HMAC SHA256 signature (Binance-compatible)
       Header: X-MBX-APIKEY
       Endpoints: /fapi/v1/*, /fapi/v2/*

  V3 — EIP-712 typed data signing with 3-address system:
       user   = main login wallet address
       signer = API wallet address (created at asterdex.com/en/api-wallet)
       privateKey = signer's private key
       Endpoints: /fapi/v3/*
       Nonce: microsecond timestamp
       Domain: {name: "AsterSignTransaction", version: "1", chainId: 1666}
       Supports hidden orders (timeInForce='HIDDEN')

Funding: 8-hour cycle. Rates via /fapi/v1/premiumIndex and /fapi/v1/fundingRate.
Fees (Pro mode): 0.01% maker / 0.035% taker (base; 5% discount with $ASTER).
Rate limits: 2400 request weight/min (IP-based), 1200 orders/min (account-based).
WS: 24h connection limit, ping/pong every 5min, max 200 streams per connection.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import time
import asyncio
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

import httpx
import structlog

from adapters.base_adapter import BaseDefiAdapter
from models.core import (
    FundingRate, Orderbook, OrderbookLevel, Position, OrderResult,
    Side, OrderStatus, VenueConfig,
)

logger = structlog.get_logger()

# ═══════════════════════════════════════════════════════════════════
# Constants from official Aster documentation
# ═══════════════════════════════════════════════════════════════════

ASTER_BASE_URL = "https://fapi.asterdex.com"
ASTER_WS_URL = "wss://fstream.asterdex.com"

# EIP-712 domain for V3 signing (from official api-docs)
EIP712_DOMAIN = {
    "name": "AsterSignTransaction",
    "version": "1",
    "chainId": 1666,  # Aster mainnet chain ID
    "verifyingContract": "0x0000000000000000000000000000000000000000",
}

EIP712_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Message": [
        {"name": "msg", "type": "string"},
    ],
}

# Rate limits documented at /fapi/v1/exchangeInfo
RATE_LIMIT_WEIGHT_PER_MIN = 2400
RATE_LIMIT_ORDERS_PER_MIN = 1200

# Funding cycles — Aster uses 1h, 4h, or 8h depending on the symbol.
# Default to 8h (majors). Detected per-symbol via nextFundingTime spacing.
FUNDING_CYCLE_HOURS_DEFAULT = 8

# User data stream keepalive interval (listenKey expires after 60 min)
LISTEN_KEY_KEEPALIVE_SEC = 1800  # 30 min recommended


class AsterAdapter(BaseDefiAdapter):
    """
    Full Aster DEX adapter for Pro mode (orderbook perpetual contracts).

    Supports two authentication modes:
      - V1 (HMAC): For users with traditional API key/secret
      - V3 (EIP-712): For users with the 3-address wallet system

    The adapter auto-selects V3 when wallet credentials are provided,
    falling back to V1 for HMAC key/secret pairs.
    """

    def __init__(self, config: VenueConfig):
        super().__init__(config)
        self._client: Optional[httpx.AsyncClient] = None

        # V1 auth (HMAC)
        self._api_key: str = ""
        self._api_secret: str = ""

        # V3 auth (EIP-712)
        self._user_address: str = ""      # main wallet
        self._signer_address: str = ""    # API wallet
        self._signer_private_key: str = ""  # API wallet private key
        self._use_v3: bool = False

        # User data stream
        self._listen_key: Optional[str] = None
        self._listen_key_task: Optional[asyncio.Task] = None

        # Weight tracking (from X-MBX-USED-WEIGHT-1m header)
        self._used_weight: int = 0

    # ═══════════════════════════════════════════════════════════════
    # Connection
    # ═══════════════════════════════════════════════════════════════

    async def connect(self, private_key: str = "", **kwargs) -> bool:
        """
        Connect to Aster. Accepts either:
          - api_key + api_secret (V1 HMAC mode)
          - user + signer + private_key (V3 EIP-712 mode)

        kwargs:
          api_key:    V1 API key
          api_secret: V1 API secret
          user:       V3 main wallet address
          signer:     V3 API wallet address
        """
        # Detect auth mode
        if kwargs.get("user") and kwargs.get("signer") and private_key:
            self._user_address = kwargs["user"]
            self._signer_address = kwargs["signer"]
            self._signer_private_key = private_key
            self._use_v3 = True
            logger.info("aster.auth_mode", mode="v3_eip712",
                        user=self._user_address[:10] + "...",
                        signer=self._signer_address[:10] + "...")
        elif kwargs.get("api_key") and kwargs.get("api_secret"):
            self._api_key = kwargs["api_key"]
            self._api_secret = kwargs["api_secret"]
            self._use_v3 = False
            logger.info("aster.auth_mode", mode="v1_hmac")
        else:
            # Attempt legacy: private_key as api_secret, look for api_key
            self._api_key = kwargs.get("api_key", "")
            self._api_secret = private_key
            self._use_v3 = False
            logger.info("aster.auth_mode", mode="v1_hmac_legacy")

        headers = {"Content-Type": "application/json"}
        if not self._use_v3:
            # V1 requires API key in header for all authenticated requests
            headers["X-MBX-APIKEY"] = self._api_key

        self._client = httpx.AsyncClient(
            base_url=self.config.api_base_url or ASTER_BASE_URL,
            timeout=15.0,
            headers=headers,
        )

        # Verify connectivity
        try:
            resp = await self._client.get("/fapi/v1/ping")
            resp.raise_for_status()
            self.connected = True
            logger.info("aster.connected", base_url=self.config.api_base_url)
        except Exception as e:
            logger.error("aster.connection_failed", error=str(e))
            self.connected = False
            return False

        return True

    async def disconnect(self):
        """Close HTTP client and cancel keepalive tasks."""
        if self._listen_key_task:
            self._listen_key_task.cancel()
        if self._listen_key:
            try:
                await self._close_listen_key()
            except Exception:
                pass
        if self._client:
            await self._client.aclose()
        self.connected = False
        logger.info("aster.disconnected")

    # ═══════════════════════════════════════════════════════════════
    # Signing — V1 (HMAC SHA256)
    # ═══════════════════════════════════════════════════════════════

    def _sign_v1(self, params: dict) -> dict:
        """
        HMAC SHA256 signature for V1 endpoints.

        Per official docs: totalParams = query string + request body.
        Signature = HMAC-SHA256(secretKey, totalParams).
        timestamp is required; recvWindow defaults to 5000ms.
        """
        params["timestamp"] = int(time.time() * 1000)
        if "recvWindow" not in params:
            params["recvWindow"] = 5000

        query_string = urlencode(params)
        signature = hmac.new(
            self._api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        params["signature"] = signature
        return params

    # ═══════════════════════════════════════════════════════════════
    # Signing — V3 (EIP-712 Typed Data)
    # ═══════════════════════════════════════════════════════════════

    def _get_nonce_v3(self) -> int:
        """
        V3 nonce: current system time in microseconds.
        Per official docs: if nonce exceeds system time or lags behind
        by more than 5 seconds, the request is considered invalid.
        """
        return int(time.time() * 1_000_000)

    def _sign_v3(self, params: dict) -> dict:
        """
        EIP-712 typed data signature for V3 endpoints.

        Per official docs:
        1. Sort params by key in ASCII order, convert all values to strings
        2. Build param string: "key1=value1&key2=value2&..."
        3. Append nonce, user, signer to the param string
        4. Sign using EIP-712 with domain {AsterSignTransaction, v1, chainId=1666}
        5. Use signer's private key for ECDSA signature

        Requires: eth_account (pip install eth-account)
        """
        try:
            from eth_account.messages import encode_structured_data
            from eth_account import Account
        except ImportError:
            raise ImportError(
                "V3 auth requires eth-account: pip install eth-account"
            )

        nonce = self._get_nonce_v3()

        # Build sorted param string (all values as strings)
        sorted_params = sorted(params.items(), key=lambda x: x[0])
        param_str = "&".join(f"{k}={v}" for k, v in sorted_params)

        # Append auth params
        param_str += f"&nonce={nonce}"
        param_str += f"&user={self._user_address}"
        param_str += f"&signer={self._signer_address}"

        # Build EIP-712 typed data structure
        typed_data = {
            "types": EIP712_TYPES,
            "primaryType": "Message",
            "domain": EIP712_DOMAIN,
            "message": {"msg": param_str},
        }

        # Sign
        message = encode_structured_data(typed_data)
        signed = Account.sign_message(message, private_key=self._signer_private_key)

        # Add auth fields to params
        params["nonce"] = str(nonce)
        params["user"] = self._user_address
        params["signer"] = self._signer_address
        params["signature"] = "0x" + signed.signature.hex()

        return params

    # ═══════════════════════════════════════════════════════════════
    # Unified request helpers
    # ═══════════════════════════════════════════════════════════════

    def _api_version(self) -> str:
        """Return 'v3' for EIP-712, 'v1' for HMAC."""
        return "v3" if self._use_v3 else "v1"

    def _sign(self, params: dict) -> dict:
        """Sign params with the appropriate auth method."""
        if self._use_v3:
            return self._sign_v3(params)
        return self._sign_v1(params)

    async def _signed_get(self, endpoint: str, params: dict | None = None) -> dict:
        """Authenticated GET request."""
        params = params or {}
        signed = self._sign(params)

        if self._use_v3:
            # V3: params in query string, no X-MBX-APIKEY header needed
            resp = await self._client.get(endpoint, params=signed)
        else:
            resp = await self._client.get(endpoint, params=signed)

        self._track_weight(resp)
        resp.raise_for_status()
        return resp.json()

    async def _signed_post(self, endpoint: str, params: dict) -> dict:
        """Authenticated POST request."""
        signed = self._sign(params)

        if self._use_v3:
            # V3 docs specify: application/x-www-form-urlencoded for body
            resp = await self._client.post(
                endpoint,
                data=signed,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        else:
            resp = await self._client.post(endpoint, params=signed)

        self._track_weight(resp)
        resp.raise_for_status()
        return resp.json()

    async def _signed_delete(self, endpoint: str, params: dict) -> dict:
        """Authenticated DELETE request."""
        signed = self._sign(params)
        resp = await self._client.delete(endpoint, params=signed)
        self._track_weight(resp)
        resp.raise_for_status()
        return resp.json()

    def _track_weight(self, resp: httpx.Response):
        """Track rate limit weight from response headers."""
        weight = resp.headers.get("X-MBX-USED-WEIGHT-1m")
        if weight:
            self._used_weight = int(weight)
            if self._used_weight > RATE_LIMIT_WEIGHT_PER_MIN * 0.8:
                logger.warning("aster.rate_limit_approaching",
                               used=self._used_weight,
                               limit=RATE_LIMIT_WEIGHT_PER_MIN)

    # ═══════════════════════════════════════════════════════════════
    # Market Data (public — no auth required, works on V1 or V3)
    # ═══════════════════════════════════════════════════════════════

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """
        Fetch current funding rate via /fapi/v1/premiumIndex.

        Response fields (from official docs):
          - lastFundingRate: last settled funding rate
          - markPrice: current mark price
          - indexPrice: current index price
          - nextFundingTime: next funding settlement timestamp (ms)
          - interestRate: interest rate component

        Note: Aster uses variable funding cycles per symbol (1h, 4h, or 8h).
        Cycle is detected from funding history spacing.
        """
        vsymbol = self.normalize_symbol(symbol)
        resp = await self._client.get(
            "/fapi/v1/premiumIndex", params={"symbol": vsymbol}
        )
        self._track_weight(resp)
        resp.raise_for_status()
        data = resp.json()

        rate = float(data.get("lastFundingRate", 0))
        mark = float(data.get("markPrice", 0))
        index_price = float(data.get("indexPrice", 0))
        next_ts = int(data.get("nextFundingTime", 0))

        cycle_hours = await self._detect_funding_cycle(vsymbol)
        payments_per_year = 365 * (24 / cycle_hours)
        annualized = rate * payments_per_year

        return FundingRate(
            venue="aster",
            symbol=symbol,
            rate=rate,
            annualized=annualized,
            mark_price=mark,
            index_price=index_price,
            next_funding_ts=next_ts,
            cycle_hours=cycle_hours,
            predicted_rate=rate,  # Aster doesn't expose a separate predicted rate
            timestamp=datetime.now(timezone.utc),
        )

    async def get_funding_history(self, symbol: str,
                                   limit: int = 100) -> list[dict]:
        """
        Fetch funding rate history via /fapi/v1/fundingRate.

        Returns list of {symbol, fundingRate, fundingTime} in ascending order.
        Max limit: 1000.
        """
        vsymbol = self.normalize_symbol(symbol)
        resp = await self._client.get(
            "/fapi/v1/fundingRate",
            params={"symbol": vsymbol, "limit": min(limit, 1000)},
        )
        resp.raise_for_status()
        return resp.json()

    async def get_orderbook(self, symbol: str, depth: int = 20) -> Orderbook:
        """
        Fetch orderbook via /fapi/v1/depth.

        Weight varies by depth: 5/10/20/50→2, 100→5, 500→10, 1000→20.
        Valid limits: [5, 10, 20, 50, 100, 500, 1000].
        """
        vsymbol = self.normalize_symbol(symbol)
        resp = await self._client.get(
            "/fapi/v1/depth",
            params={"symbol": vsymbol, "limit": depth},
        )
        self._track_weight(resp)
        resp.raise_for_status()
        data = resp.json()

        bids = [
            OrderbookLevel(price=float(b[0]), size=float(b[1]))
            for b in data.get("bids", [])
        ]
        asks = [
            OrderbookLevel(price=float(a[0]), size=float(a[1]))
            for a in data.get("asks", [])
        ]

        return Orderbook(venue="aster", symbol=symbol, bids=bids, asks=asks)

    async def get_exchange_info(self) -> dict:
        """
        Fetch exchange info including trading rules, symbol filters,
        rate limits, and precision settings.

        Key fields per symbol:
          - pricePrecision, quantityPrecision (do NOT use as tick/step size)
          - filters: PRICE_FILTER (tickSize), LOT_SIZE (stepSize, minQty, maxQty),
                     MIN_NOTIONAL (notional), PERCENT_PRICE (multiplierUp/Down)
          - OrderType: available order types
          - timeInForce: available TIF values
          - liquidationFee, marketTakeBound
        """
        resp = await self._client.get("/fapi/v1/exchangeInfo")
        resp.raise_for_status()
        return resp.json()

    async def get_mark_price_all(self) -> list[dict]:
        """
        Fetch mark price and funding rate for ALL symbols.
        Weight: 1. Per docs, omitting symbol returns all.
        """
        resp = await self._client.get("/fapi/v1/premiumIndex")
        resp.raise_for_status()
        return resp.json()

    async def get_ticker_24h(self, symbol: str | None = None) -> dict | list:
        """
        24hr rolling window price change statistics.
        Weight: 1 for single symbol, 40 for all symbols.
        """
        params = {}
        if symbol:
            params["symbol"] = self.normalize_symbol(symbol)
        resp = await self._client.get("/fapi/v1/ticker/24hr", params=params)
        self._track_weight(resp)
        resp.raise_for_status()
        return resp.json()

    # ═══════════════════════════════════════════════════════════════
    # Trading — Orders
    # ═══════════════════════════════════════════════════════════════

    async def place_limit_order(
        self,
        symbol: str,
        side: Side,
        size: float,
        price: float,
        reduce_only: bool = False,
        tif: str = "GTC",
        position_side: str = "BOTH",
        hidden: bool = False,
    ) -> OrderResult:
        """
        Place a limit order via POST /fapi/{version}/order.

        Per official docs, order params:
          - symbol (STRING, required)
          - side (ENUM: BUY/SELL, required)
          - type (ENUM: LIMIT, required)
          - timeInForce (ENUM: GTC/IOC/FOK/GTX, required for LIMIT)
          - quantity (DECIMAL, required)
          - price (DECIMAL, required)
          - positionSide (ENUM: BOTH/LONG/SHORT, default BOTH)
          - reduceOnly (STRING: "true"/"false", default "false")
          - newOrderRespType (ENUM: ACK/RESULT, default "ACK")

        Hidden orders (V3 only): Set timeInForce='HIDDEN' to create iceberg
        orders that don't appear in the public orderbook. Provides MEV protection.
        """
        v = self._api_version()
        vsymbol = self.normalize_symbol(symbol)

        # Use HIDDEN TIF for hidden orders (V3 feature)
        effective_tif = "HIDDEN" if (hidden and self._use_v3) else tif

        params = {
            "symbol": vsymbol,
            "side": "BUY" if side == Side.LONG else "SELL",
            "type": "LIMIT",
            "timeInForce": effective_tif,
            "quantity": str(size),
            "price": str(price),
            "positionSide": position_side,
            "newOrderRespType": "RESULT",
        }
        if reduce_only and position_side == "BOTH":
            params["reduceOnly"] = "true"

        try:
            data = await self._signed_post(f"/fapi/{v}/order", params)
            return OrderResult(
                venue="aster",
                symbol=symbol,
                side=side,
                order_id=str(data.get("orderId", "")),
                client_order_id=data.get("clientOrderId", ""),
                status=self._map_order_status(data.get("status", "")),
                filled_qty=float(data.get("executedQty", 0)),
                avg_price=float(data.get("avgPrice", 0)),
                fee=float(data.get("cumQuote", 0)) * 0.0001,  # estimate
            )
        except httpx.HTTPStatusError as e:
            error_data = e.response.json() if e.response else {}
            logger.error("aster.order_failed",
                         code=error_data.get("code"),
                         msg=error_data.get("msg"),
                         symbol=vsymbol, side=side.value)
            return OrderResult(
                venue="aster", symbol=symbol, side=side,
                status=OrderStatus.FAILED,
                error=f"[{error_data.get('code')}] {error_data.get('msg', str(e))}",
            )

    async def place_market_order(
        self,
        symbol: str,
        side: Side,
        size: float,
        reduce_only: bool = False,
        position_side: str = "BOTH",
    ) -> OrderResult:
        """
        Place a market order via POST /fapi/{version}/order.

        Market orders require only: symbol, side, type=MARKET, quantity.
        No timeInForce or price needed.
        """
        v = self._api_version()
        vsymbol = self.normalize_symbol(symbol)

        params = {
            "symbol": vsymbol,
            "side": "BUY" if side == Side.LONG else "SELL",
            "type": "MARKET",
            "quantity": str(size),
            "positionSide": position_side,
            "newOrderRespType": "RESULT",
        }
        if reduce_only and position_side == "BOTH":
            params["reduceOnly"] = "true"

        try:
            data = await self._signed_post(f"/fapi/{v}/order", params)
            return OrderResult(
                venue="aster",
                symbol=symbol,
                side=side,
                order_id=str(data.get("orderId", "")),
                client_order_id=data.get("clientOrderId", ""),
                status=self._map_order_status(data.get("status", "")),
                filled_qty=float(data.get("executedQty", 0)),
                avg_price=float(data.get("avgPrice", 0)),
                fee=float(data.get("cumQuote", 0)) * 0.00035,  # taker est
            )
        except httpx.HTTPStatusError as e:
            error_data = e.response.json() if e.response else {}
            logger.error("aster.market_order_failed",
                         code=error_data.get("code"),
                         msg=error_data.get("msg"))
            return OrderResult(
                venue="aster", symbol=symbol, side=side,
                status=OrderStatus.FAILED,
                error=f"[{error_data.get('code')}] {error_data.get('msg', str(e))}",
            )

    async def place_batch_orders(self, orders: list[dict]) -> list[dict]:
        """
        Place up to 5 orders atomically via POST /fapi/{version}/batchOrders.
        Weight: 5. Orders processed concurrently, matching order not guaranteed.
        """
        v = self._api_version()
        params = {"batchOrders": json.dumps(orders[:5])}
        return await self._signed_post(f"/fapi/{v}/batchOrders", params)

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """Cancel a single order via DELETE /fapi/{version}/order."""
        v = self._api_version()
        vsymbol = self.normalize_symbol(symbol)
        try:
            await self._signed_delete(
                f"/fapi/{v}/order",
                {"symbol": vsymbol, "orderId": int(order_id)},
            )
            return True
        except Exception as e:
            logger.error("aster.cancel_failed", order_id=order_id, error=str(e))
            return False

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        """
        Cancel all open orders for a symbol via DELETE /fapi/{version}/allOpenOrders.
        Symbol is required by the Aster API.
        Returns estimated count (API returns success/fail, not count).
        """
        if not symbol:
            logger.warning("aster.cancel_all_requires_symbol")
            return 0

        v = self._api_version()
        vsymbol = self.normalize_symbol(symbol)
        try:
            await self._signed_delete(
                f"/fapi/{v}/allOpenOrders",
                {"symbol": vsymbol},
            )
            return 1  # API returns 200 on success, no count
        except Exception as e:
            logger.error("aster.cancel_all_failed", error=str(e))
            return 0

    async def set_countdown_cancel(self, symbol: str,
                                    countdown_ms: int = 120000) -> dict:
        """
        Auto-cancel all open orders after countdown.
        POST /fapi/{version}/countdownCancelAll.

        Per docs: Call repeatedly as heartbeat. Set countdown_ms=0 to cancel timer.
        System checks every ~10ms, so don't set too precise.
        Weight: 10.
        """
        v = self._api_version()
        vsymbol = self.normalize_symbol(symbol)
        return await self._signed_post(
            f"/fapi/{v}/countdownCancelAll",
            {"symbol": vsymbol, "countdownTime": countdown_ms},
        )

    # ═══════════════════════════════════════════════════════════════
    # Account & Position Management
    # ═══════════════════════════════════════════════════════════════

    async def get_positions(self) -> list[Position]:
        """
        Fetch all positions via GET /fapi/v2/positionRisk (V1)
        or GET /fapi/v3/positionRisk (V3).

        Per docs, returns positionAmt, entryPrice, markPrice,
        unRealizedProfit, liquidationPrice, leverage, marginType,
        isolatedMargin, positionSide, notional, isolatedWallet.
        """
        v = "v2" if not self._use_v3 else "v3"
        data = await self._signed_get(f"/fapi/{v}/positionRisk", {})

        positions = []
        for p in data:
            amt = float(p.get("positionAmt", 0))
            if amt == 0:
                continue

            positions.append(Position(
                venue="aster",
                symbol=self._denormalize_symbol(p["symbol"]),
                side=Side.LONG if amt > 0 else Side.SHORT,
                size=abs(amt),
                entry_price=float(p.get("entryPrice", 0)),
                mark_price=float(p.get("markPrice", 0)),
                unrealized_pnl=float(p.get("unRealizedProfit", 0)),
                liquidation_price=float(p.get("liquidationPrice", 0)),
                leverage=int(float(p.get("leverage", 1))),
                margin_type=p.get("marginType", "cross"),
                position_side=p.get("positionSide", "BOTH"),
            ))

        return positions

    async def get_balance(self) -> dict:
        """
        Fetch account balance via GET /fapi/v2/balance (V1)
        or GET /fapi/v3/balance (V3).

        Returns per-asset: walletBalance, crossWalletBalance,
        crossUnPnl, availableBalance, maxWithdrawAmount, marginAvailable.
        """
        v = "v2" if not self._use_v3 else "v3"
        data = await self._signed_get(f"/fapi/{v}/balance", {})

        # Aggregate USDT balance (primary collateral)
        for asset in data:
            if asset.get("asset") == "USDT":
                return {
                    "available": float(asset.get("availableBalance", 0)),
                    "total": float(asset.get("walletBalance", 0)),
                    "margin_used": (
                        float(asset.get("walletBalance", 0))
                        - float(asset.get("availableBalance", 0))
                    ),
                    "unrealized_pnl": float(asset.get("crossUnPnl", 0)),
                }

        return {"available": 0, "total": 0, "margin_used": 0}

    async def get_account_info(self) -> dict:
        """
        Full account info via GET /fapi/v4/account (V1) or /fapi/v3/account (V3).
        Includes assets, positions, margin type, and multi-asset mode.
        Weight: 5.
        """
        v = "v4" if not self._use_v3 else "v3"
        return await self._signed_get(f"/fapi/{v}/account", {})

    async def set_leverage(self, symbol: str, leverage: int) -> dict:
        """
        Change leverage via POST /fapi/{version}/leverage.
        Leverage range: 1 to 125 (per symbol; check /exchangeInfo for max).
        """
        v = self._api_version()
        vsymbol = self.normalize_symbol(symbol)
        return await self._signed_post(
            f"/fapi/{v}/leverage",
            {"symbol": vsymbol, "leverage": leverage},
        )

    async def set_margin_type(self, symbol: str,
                               margin_type: str = "CROSSED") -> dict:
        """
        Change margin type: ISOLATED or CROSSED.
        Cannot change if open orders or positions exist on the symbol.
        """
        v = self._api_version()
        vsymbol = self.normalize_symbol(symbol)
        return await self._signed_post(
            f"/fapi/{v}/marginType",
            {"symbol": vsymbol, "marginType": margin_type},
        )

    async def set_position_mode(self, hedge_mode: bool) -> dict:
        """
        Toggle between Hedge Mode (LONG/SHORT) and One-Way Mode (BOTH).
        Applies to ALL symbols on the account.
        """
        v = self._api_version()
        return await self._signed_post(
            f"/fapi/{v}/positionSide/dual",
            {"dualSidePosition": "true" if hedge_mode else "false"},
        )

    async def set_multi_asset_mode(self, enabled: bool) -> dict:
        """
        Toggle Multi-Asset Mode. When enabled, allows using multiple
        assets as margin (USDT, BTC, ETH, etc.). Applies globally.
        """
        v = self._api_version()
        return await self._signed_post(
            f"/fapi/{v}/multiAssetsMargin",
            {"multiAssetsMargin": "true" if enabled else "false"},
        )

    async def get_income_history(self, symbol: str | None = None,
                                  income_type: str | None = None,
                                  limit: int = 100) -> list[dict]:
        """
        Fetch income history (funding fees, PnL, commissions, etc.).
        GET /fapi/{version}/income. Weight: 30.

        income_type options: TRANSFER, WELCOME_BONUS, REALIZED_PNL,
          FUNDING_FEE, COMMISSION, INSURANCE_CLEAR, MARKET_MERCHANT_RETURN_REWARD
        """
        v = self._api_version()
        params: dict = {"limit": min(limit, 1000)}
        if symbol:
            params["symbol"] = self.normalize_symbol(symbol)
        if income_type:
            params["incomeType"] = income_type
        return await self._signed_get(f"/fapi/{v}/income", params)

    async def get_commission_rate(self, symbol: str) -> dict:
        """
        Fetch user's commission rate for a symbol.
        GET /fapi/{version}/commissionRate. Weight: 20.
        Returns makerCommissionRate and takerCommissionRate.
        """
        v = self._api_version()
        vsymbol = self.normalize_symbol(symbol)
        return await self._signed_get(
            f"/fapi/{v}/commissionRate", {"symbol": vsymbol}
        )

    # ═══════════════════════════════════════════════════════════════
    # User Data Stream (WebSocket listen key management)
    # ═══════════════════════════════════════════════════════════════

    async def start_user_data_stream(self) -> str:
        """
        Start or resume user data stream.
        POST /fapi/v1/listenKey — returns a listenKey valid for 60 minutes.
        If an active key exists, returns the same key and extends validity.

        Connect to: wss://fstream.asterdex.com/ws/{listenKey}
        Events: ACCOUNT_UPDATE, ORDER_TRADE_UPDATE, ACCOUNT_CONFIG_UPDATE,
                MARGIN_CALL, listenKeyExpired.
        """
        if self._use_v3:
            params = self._sign({})
            resp = await self._client.post("/fapi/v1/listenKey", data=params)
        else:
            resp = await self._client.post("/fapi/v1/listenKey")
        resp.raise_for_status()
        self._listen_key = resp.json().get("listenKey", "")

        # Start keepalive task
        if self._listen_key_task is None or self._listen_key_task.done():
            self._listen_key_task = asyncio.create_task(
                self._keepalive_loop()
            )

        logger.info("aster.user_stream_started",
                     listen_key=self._listen_key[:12] + "...")
        return self._listen_key

    async def _keepalive_loop(self):
        """Send keepalive PUT every 30 minutes (key expires at 60 min)."""
        while True:
            await asyncio.sleep(LISTEN_KEY_KEEPALIVE_SEC)
            try:
                if self._use_v3:
                    params = self._sign({})
                    await self._client.put("/fapi/v1/listenKey", data=params)
                else:
                    await self._client.put("/fapi/v1/listenKey")
                logger.debug("aster.listen_key_keepalive")
            except Exception as e:
                logger.warning("aster.keepalive_failed", error=str(e))

    async def _close_listen_key(self):
        """Close user data stream."""
        if self._use_v3:
            params = self._sign({})
            await self._client.delete("/fapi/v1/listenKey", data=params)
        else:
            await self._client.delete("/fapi/v1/listenKey")

    # ═══════════════════════════════════════════════════════════════
    # Symbol normalization
    # ═══════════════════════════════════════════════════════════════

    def normalize_symbol(self, symbol: str) -> str:
        """
        Convert internal symbol to Aster format.
        Config: symbol_format = "{symbol}USDT" → BTC → BTCUSDT
        """
        fmt = self.config.symbol_format or "{symbol}USDT"
        clean = symbol.replace("-PERP", "").replace("-USD", "").replace("USDT", "")
        return fmt.replace("{symbol}", clean)

    def _denormalize_symbol(self, venue_symbol: str) -> str:
        """Convert Aster symbol back to internal. BTCUSDT → BTC"""
        return venue_symbol.replace("USDT", "").replace("BUSD", "")

    # ═══════════════════════════════════════════════════════════════
    # Funding cycle detection
    # ═══════════════════════════════════════════════════════════════

    _cycle_cache: dict[str, int] = {}  # symbol → cycle_hours

    async def _detect_funding_cycle(self, venue_symbol: str) -> int:
        """
        Detect funding cycle (1h, 4h, or 8h) for a symbol by checking
        the spacing between the two most recent funding rate entries.
        Results are cached for the lifetime of the adapter.
        """
        if venue_symbol in self._cycle_cache:
            return self._cycle_cache[venue_symbol]

        try:
            resp = await self._client.get(
                "/fapi/v1/fundingRate",
                params={"symbol": venue_symbol, "limit": 2},
            )
            resp.raise_for_status()
            history = resp.json()
            if len(history) >= 2:
                t0 = int(history[0]["fundingTime"])
                t1 = int(history[1]["fundingTime"])
                diff_hours = round((t1 - t0) / (1000 * 3600))
                if diff_hours in (1, 4, 8):
                    self._cycle_cache[venue_symbol] = diff_hours
                    return diff_hours
        except Exception as e:
            logger.debug("aster.cycle_detect_failed",
                         symbol=venue_symbol, error=str(e))

        self._cycle_cache[venue_symbol] = FUNDING_CYCLE_HOURS_DEFAULT
        return FUNDING_CYCLE_HOURS_DEFAULT

    # ═══════════════════════════════════════════════════════════════
    # Helpers
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def _map_order_status(status_str: str) -> OrderStatus:
        """Map Aster order status strings to internal enum."""
        mapping = {
            "NEW": OrderStatus.OPEN,
            "PARTIALLY_FILLED": OrderStatus.PARTIAL,
            "FILLED": OrderStatus.FILLED,
            "CANCELED": OrderStatus.CANCELLED,
            "REJECTED": OrderStatus.FAILED,
            "EXPIRED": OrderStatus.CANCELLED,
            "NEW_INSURANCE": OrderStatus.FILLED,
            "NEW_ADL": OrderStatus.FILLED,
        }
        return mapping.get(status_str, OrderStatus.UNKNOWN)

    def estimate_gas_cost(self, operation: str = "trade") -> float:
        """
        Aster Pro mode has zero on-chain gas for trading (off-chain matching).
        Deposits/withdrawals may incur BNB Chain gas (~$0.01-0.05).
        """
        if operation in ("trade", "cancel", "fund_claim"):
            return 0.0  # zero_gas: true in config
        return 0.02  # deposit/withdraw gas estimate

    @property
    def rate_limit_remaining(self) -> int:
        """Approximate remaining weight this minute."""
        return max(0, RATE_LIMIT_WEIGHT_PER_MIN - self._used_weight)
