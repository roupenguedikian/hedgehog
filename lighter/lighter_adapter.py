"""
hedgehog/adapters/lighter_adapter.py
Lighter ZK-rollup adapter using the official lighter-sdk Python package.

Lighter is fundamentally different from generic REST exchanges:
  - Uses account indexes (integers) instead of traditional wallet addresses
  - API keys are separate from L1 wallet keys — up to 253 keys per account (indices 2–254)
  - Transactions are signed with a SignerClient that wraps a Go-based signer binary
  - Nonces are per-API-key and must be incremented for each signed transaction
  - Prices and sizes are integers scaled by market-specific decimal precision
  - Markets are identified by integer index (0 = ETH, etc.), not string symbols
  - Standard accounts: zero maker + zero taker fees
  - Premium accounts: 0.2 bps maker / 2 bps taker, with lower latency
  - Settlement on Ethereum L1 via ZK-SNARK proofs (Plonky2 → Plonk wrapper)
  - Desert Mode escape hatch: if sequencer is down >14d, users force-exit via L1

Official SDK:  pip install lighter-sdk
API base URL:  https://mainnet.zklighter.elliot.ai
WebSocket URL: wss://mainnet.zklighter.elliot.ai/stream
API docs:      https://apidocs.lighter.xyz
Python SDK:    https://github.com/elliottech/lighter-python
"""
from __future__ import annotations
import asyncio
import time
from datetime import datetime, timezone
from typing import Optional

import httpx
import structlog

from adapters.base_adapter import BaseDefiAdapter
from models.core import (
    FundingRate, Orderbook, OrderbookLevel, Position, OrderResult,
    Side, OrderStatus, VenueConfig,
)

logger = structlog.get_logger()

# ── Constants ────────────────────────────────────────────────────────────────

LIGHTER_MAINNET_URL = "https://mainnet.zklighter.elliot.ai"
LIGHTER_WS_URL = "wss://mainnet.zklighter.elliot.ai/stream"
LIGHTER_TESTNET_URL = "https://testnet.zklighter.elliot.ai"

# Rate limits (standard accounts): 60 requests/min across all endpoints.
# Premium accounts: 24,000 weighted requests/min for data; separate bucket
# for sendTx/sendTxBatch (4,000–40,000/min depending on staked LIT).
STANDARD_RATE_LIMIT_PER_MIN = 60
PREMIUM_RATE_LIMIT_WEIGHT_PER_MIN = 24_000

# Endpoint weights (for premium rate limit budgeting)
ENDPOINT_WEIGHTS = {
    "sendTx": 6,
    "sendTxBatch": 6,
    "nextNonce": 6,
    "orderBooks": 300,
    "orderBookDetails": 300,
    "orderBookOrders": 300,
    "account": 300,
    "fundings": 300,
    "recentTrades": 600,
    "trades": 600,
    "apikeys": 150,
}

# Order types (from SignerClient constants)
ORDER_TYPE_LIMIT = 0
ORDER_TYPE_MARKET = 1
ORDER_TYPE_STOP_LOSS = 2
ORDER_TYPE_STOP_LOSS_LIMIT = 3
ORDER_TYPE_TAKE_PROFIT = 4
ORDER_TYPE_TAKE_PROFIT_LIMIT = 5
ORDER_TYPE_TWAP = 6

# Time-in-force
TIF_IOC = 0   # Immediate-Or-Cancel
TIF_GTT = 1   # Good-Till-Time
TIF_POST_ONLY = 2


class LighterAdapter(BaseDefiAdapter):
    """
    Lighter ZK-rollup adapter.

    Uses the official lighter-sdk for authenticated operations (signing via
    SignerClient) and raw httpx for public data endpoints. This avoids pulling
    the full SDK as a hard dependency for read-only mode while supporting full
    trading when API keys are configured.

    Connection modes:
      1. Read-only: No API key → public data only (funding, orderbook, trades).
      2. Full trading: Requires LIGHTER_API_KEY_PRIVATE + account_index +
         api_key_index → sign and send orders via SignerClient.

    Key architectural differences vs other venues:
      - Lighter uses integer market_index, not string symbols. We maintain a
        mapping from our canonical symbols (BTC, ETH) to market indexes by
        querying orderBookDetails on connect.
      - Prices and sizes are integers. Each market has its own decimal precision
        (e.g. size_decimals=4 means 1 ETH = 10000 in the API). We query this
        from orderBookDetails and handle conversion transparently.
      - Auth uses API keys (not the L1 wallet key directly). The L1 ETH key is
        only needed for initial key registration and non-secure withdrawals.
    """

    def __init__(self, config: VenueConfig):
        super().__init__(config)
        self._client: Optional[httpx.AsyncClient] = None
        self._signer = None  # lighter.SignerClient when SDK is available
        self._api_key_private: str = ""
        self._account_index: Optional[int] = None
        self._api_key_index: Optional[int] = None
        self._l1_address: str = ""

        # Market metadata — populated on connect from orderBookDetails
        self._market_map: dict[str, int] = {}       # symbol → market_index
        self._market_details: dict[int, dict] = {}   # market_index → detail blob
        self._size_decimals: dict[int, int] = {}     # market_index → size decimal places
        self._price_decimals: dict[int, int] = {}    # market_index → price decimal places
        self._min_base_amount: dict[int, int] = {}   # market_index → min order size (int)
        self._min_quote_amount: dict[int, float] = {}  # market_index → min in USDC

        # Nonce tracking (SDK handles this, but we track for observability)
        self._last_nonce: Optional[int] = None

    # ── Connection ───────────────────────────────────────────────────────

    async def connect(self, private_key: str, **kwargs) -> bool:
        """
        Connect to Lighter.

        Args:
            private_key: The API key private key (NOT the L1 ETH wallet key).
                         Pass empty string for read-only data access.
            **kwargs:
                account_index (int): Lighter account index (query via L1 address)
                api_key_index (int): API key slot index (2–254 for user keys)
                l1_address (str): Ethereum L1 address (for account lookup)
                use_testnet (bool): Connect to testnet instead of mainnet
        """
        try:
            base_url = (
                LIGHTER_TESTNET_URL
                if kwargs.get("use_testnet", False)
                else self.config.api_base_url or LIGHTER_MAINNET_URL
            )

            self._client = httpx.AsyncClient(base_url=base_url, timeout=15.0)
            self._api_key_private = private_key
            self._account_index = kwargs.get("account_index")
            self._api_key_index = kwargs.get("api_key_index")
            self._l1_address = kwargs.get("l1_address", "")

            # ── Fetch market metadata (public, no auth required) ─────────
            await self._load_market_metadata()

            # ── Initialize SignerClient for trading if credentials provided
            if private_key and self._account_index is not None:
                await self._init_signer(base_url)

            # ── Resolve account index from L1 address if needed ──────────
            if (
                not self._account_index
                and self._l1_address
                and not private_key
            ):
                await self._resolve_account_index()

            self.connected = True
            logger.info(
                "lighter.connected",
                account_index=self._account_index or "read-only",
                markets=len(self._market_map),
                mode="trading" if self._signer else "read-only",
            )
            return True

        except Exception as e:
            logger.error("lighter.connect_failed", error=str(e))
            self.connected = False
            return False

    async def _load_market_metadata(self):
        """
        Fetch orderBookDetails to build symbol→market_index mapping and
        learn decimal precision for each market.

        GET /api/v1/orderBookDetails
        Returns per-market: market_index, symbol, size_decimals, price_decimals,
        min_base_amount, min_quote_amount, etc.
        """
        resp = await self._client.get("/api/v1/orderBookDetails")
        resp.raise_for_status()
        data = resp.json()

        for detail in data.get("order_book_details", []):
            idx = int(detail["market_index"])
            raw_symbol = detail.get("symbol", "")

            # Lighter symbols are like "ETH-USD", "BTC-USD" — extract base
            base_symbol = raw_symbol.split("-")[0].upper() if "-" in raw_symbol else raw_symbol.upper()

            self._market_map[base_symbol] = idx
            self._market_details[idx] = detail
            self._size_decimals[idx] = int(detail.get("supported_size_decimals", 4))
            self._price_decimals[idx] = int(detail.get("supported_price_decimals", 2))
            self._min_base_amount[idx] = int(detail.get("min_base_amount", 0))
            self._min_quote_amount[idx] = float(detail.get("min_quote_amount", 0))

        logger.info(
            "lighter.markets_loaded",
            count=len(self._market_map),
            symbols=list(self._market_map.keys()),
        )

    async def _init_signer(self, base_url: str):
        """
        Initialize the lighter-sdk SignerClient for transaction signing.

        The SDK handles:
          - Transaction serialization and signing via an embedded Go binary
          - Automatic nonce management (incrementing per API key)
          - Auth token generation for authenticated REST/WS endpoints
        """
        try:
            import lighter

            self._signer = lighter.SignerClient(
                url=base_url,
                api_private_keys={self._api_key_index: self._api_key_private},
                account_index=self._account_index,
            )
            err = self._signer.check_client()
            if err is not None:
                logger.error("lighter.signer_init_failed", error=str(err))
                self._signer = None
            else:
                logger.info(
                    "lighter.signer_ready",
                    account_index=self._account_index,
                    api_key_index=self._api_key_index,
                )
        except ImportError:
            logger.warning(
                "lighter.sdk_not_installed",
                msg="pip install lighter-sdk for trading support",
            )
            self._signer = None

    async def _resolve_account_index(self):
        """Look up account index from L1 Ethereum address."""
        try:
            resp = await self._client.get(
                "/api/v1/accountsByL1Address",
                params={"l1_address": self._l1_address},
            )
            resp.raise_for_status()
            data = resp.json()
            sub_accounts = data.get("sub_accounts", [])
            if sub_accounts:
                self._account_index = int(sub_accounts[0]["index"])
                logger.info(
                    "lighter.account_resolved",
                    l1_address=self._l1_address[:10] + "...",
                    account_index=self._account_index,
                )
        except Exception as e:
            logger.warning("lighter.account_resolve_failed", error=str(e))

    # ── Symbol / Precision Helpers ───────────────────────────────────────

    def normalize_symbol(self, symbol: str) -> str:
        """Convert canonical symbol to Lighter format: BTC → BTC-USD."""
        fmt = self.config.symbol_format or "{symbol}-USD"
        return fmt.replace("{symbol}", symbol.upper())

    def _market_index(self, symbol: str) -> int:
        """Resolve canonical symbol (BTC, ETH) to Lighter market_index."""
        base = symbol.upper().split("-")[0]
        if base not in self._market_map:
            raise ValueError(
                f"Unknown symbol '{symbol}' on Lighter. "
                f"Available: {list(self._market_map.keys())}"
            )
        return self._market_map[base]

    def _encode_size(self, market_index: int, size: float) -> int:
        """Convert human-readable size to Lighter integer representation."""
        decimals = self._size_decimals.get(market_index, 4)
        return int(round(size * (10 ** decimals)))

    def _decode_size(self, market_index: int, raw_size: int | str) -> float:
        """Convert Lighter integer size to human-readable float."""
        decimals = self._size_decimals.get(market_index, 4)
        return int(raw_size) / (10 ** decimals)

    def _encode_price(self, market_index: int, price: float) -> int:
        """Convert human-readable price to Lighter integer representation."""
        decimals = self._price_decimals.get(market_index, 2)
        return int(round(price * (10 ** decimals)))

    def _decode_price(self, market_index: int, raw_price: int | str) -> float:
        """Convert Lighter integer price to human-readable float."""
        decimals = self._price_decimals.get(market_index, 2)
        return int(raw_price) / (10 ** decimals)

    # ── Market Data ──────────────────────────────────────────────────────

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        """
        Fetch current funding rate for a symbol.

        Uses GET /api/v1/fundings with the market_index.
        Lighter uses hourly funding with TWAP premium calculation,
        clamped between -0.5% and +0.5% per hour.
        """
        market_idx = self._market_index(symbol)

        resp = await self._client.get(
            "/api/v1/fundings",
            params={"market_index": market_idx, "limit": 1},
        )
        resp.raise_for_status()
        data = resp.json()

        fundings = data.get("fundings", [])
        if not fundings:
            return FundingRate(
                venue="lighter",
                symbol=symbol,
                rate=0.0,
                annualized=0.0,
                next_funding_ts=self._next_hour(),
                cycle_hours=1,
                predicted_rate=None,
            )

        latest = fundings[0]
        rate = float(latest.get("funding_rate", 0))

        return FundingRate(
            venue="lighter",
            symbol=symbol,
            rate=rate,
            annualized=rate * 24 * 365,  # 1h cycle → 8760 periods/year
            next_funding_ts=self._next_hour(),
            cycle_hours=1,
            predicted_rate=None,  # Lighter doesn't expose predicted rate via REST
        )

    async def get_funding_history(self, symbol: str, limit: int = 100) -> list[FundingRate]:
        """Fetch historical funding rates."""
        market_idx = self._market_index(symbol)

        resp = await self._client.get(
            "/api/v1/fundings",
            params={"market_index": market_idx, "limit": min(limit, 500)},
        )
        resp.raise_for_status()
        data = resp.json()

        rates = []
        for f in data.get("fundings", []):
            rate = float(f.get("funding_rate", 0))
            ts = int(f.get("timestamp", 0))
            rates.append(FundingRate(
                venue="lighter",
                symbol=symbol,
                rate=rate,
                annualized=rate * 24 * 365,
                next_funding_ts=datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                if ts > 0
                else self._next_hour(),
                cycle_hours=1,
                predicted_rate=None,
            ))
        return rates

    async def get_orderbook(self, symbol: str, depth: int = 20) -> Orderbook:
        """
        Fetch orderbook snapshot.

        Uses GET /api/v1/orderBooks which returns aggregated price levels.
        For full order-level depth, use GET /api/v1/orderBookOrders.
        WS channel order_book:{market_index} streams incremental updates at 50ms.
        """
        market_idx = self._market_index(symbol)

        resp = await self._client.get(
            "/api/v1/orderBooks",
            params={"market_index": market_idx},
        )
        resp.raise_for_status()
        data = resp.json()

        ob_data = data.get("order_books", [{}])[0] if data.get("order_books") else data

        bids = [
            OrderbookLevel(
                price=float(level.get("price", 0)),
                size=float(level.get("size", 0)),
            )
            for level in ob_data.get("bids", [])[:depth]
        ]
        asks = [
            OrderbookLevel(
                price=float(level.get("price", 0)),
                size=float(level.get("size", 0)),
            )
            for level in ob_data.get("asks", [])[:depth]
        ]

        return Orderbook(venue="lighter", symbol=symbol, bids=bids, asks=asks)

    async def get_exchange_stats(self) -> dict:
        """
        GET /api/v1/exchangeStats — global exchange statistics.
        Useful for monitoring overall Lighter health and volume.
        """
        resp = await self._client.get("/api/v1/exchangeStats")
        resp.raise_for_status()
        return resp.json()

    # ── Trading ──────────────────────────────────────────────────────────

    def _require_signer(self):
        """Guard: raise if trading is attempted without a signer."""
        if self._signer is None:
            raise RuntimeError(
                "Lighter trading requires SignerClient. "
                "Provide api_key_private, account_index, and api_key_index "
                "to connect(), and install lighter-sdk."
            )

    async def place_limit_order(
        self,
        symbol: str,
        side: Side,
        size: float,
        price: float,
        reduce_only: bool = False,
        tif: str = "GTC",
        client_order_index: Optional[int] = None,
    ) -> OrderResult:
        """
        Place a limit order on Lighter.

        Uses SignerClient.create_order() which signs and submits the transaction.
        The client_order_index (uint48) is your local ID to reference the order
        for cancels/modifies. Must be unique across all markets.
        """
        self._require_signer()
        market_idx = self._market_index(symbol)

        is_ask = side == Side.SHORT
        encoded_size = self._encode_size(market_idx, size)
        encoded_price = self._encode_price(market_idx, price)

        # Map TIF string to Lighter constant
        tif_map = {"GTC": TIF_GTT, "IOC": TIF_IOC, "POST_ONLY": TIF_POST_ONLY}
        lighter_tif = tif_map.get(tif.upper(), TIF_GTT)

        # Generate unique client_order_index if not provided
        if client_order_index is None:
            client_order_index = int(time.time() * 1000) % (2**48)

        try:
            tx, tx_hash, err = await self._signer.create_order(
                market_index=market_idx,
                client_order_index=client_order_index,
                base_amount=encoded_size,
                price=encoded_price,
                is_ask=is_ask,
                order_type=ORDER_TYPE_LIMIT,
                time_in_force=lighter_tif,
                reduce_only=reduce_only,
                order_expiry=self._signer.DEFAULT_28_DAY_ORDER_EXPIRY,
            )

            if err:
                logger.warning(
                    "lighter.limit_order_error",
                    symbol=symbol, side=side, error=str(err),
                )
                return OrderResult(
                    venue="lighter", symbol=symbol, side=side,
                    status=OrderStatus.FAILED, error=str(err),
                )

            logger.info(
                "lighter.limit_order_placed",
                symbol=symbol, side=side, size=size, price=price,
                tx_hash=tx_hash, client_order_index=client_order_index,
            )
            return OrderResult(
                venue="lighter", symbol=symbol, side=side,
                status=OrderStatus.SUBMITTED,
                order_id=str(client_order_index),
                tx_hash=tx_hash,
            )

        except Exception as e:
            logger.error("lighter.limit_order_exception", error=str(e))
            return OrderResult(
                venue="lighter", symbol=symbol, side=side,
                status=OrderStatus.FAILED, error=str(e),
            )

    async def place_market_order(
        self,
        symbol: str,
        side: Side,
        size: float,
        reduce_only: bool = False,
        slippage_bps: int = 50,
    ) -> OrderResult:
        """
        Place a market order on Lighter.

        Market orders use ORDER_TYPE_MARKET with IOC time-in-force.
        The price parameter acts as worst acceptable price (slippage guard) —
        if the sequencer can't fill at this price or better, the order cancels.
        """
        self._require_signer()
        market_idx = self._market_index(symbol)

        is_ask = side == Side.SHORT
        encoded_size = self._encode_size(market_idx, size)

        # Fetch current best price for slippage calculation
        ob = await self.get_orderbook(symbol, depth=1)
        if is_ask and ob.bids:
            worst_price = ob.bids[0].price * (1 - slippage_bps / 10_000)
        elif not is_ask and ob.asks:
            worst_price = ob.asks[0].price * (1 + slippage_bps / 10_000)
        else:
            return OrderResult(
                venue="lighter", symbol=symbol, side=side,
                status=OrderStatus.FAILED,
                error="Cannot determine price — empty orderbook",
            )

        encoded_price = self._encode_price(market_idx, worst_price)
        client_order_index = int(time.time() * 1000) % (2**48)

        try:
            tx, tx_hash, err = await self._signer.create_order(
                market_index=market_idx,
                client_order_index=client_order_index,
                base_amount=encoded_size,
                price=encoded_price,
                is_ask=is_ask,
                order_type=ORDER_TYPE_MARKET,
                time_in_force=TIF_IOC,
                reduce_only=reduce_only,
                order_expiry=self._signer.DEFAULT_IOC_EXPIRY,
            )

            if err:
                logger.warning("lighter.market_order_error", error=str(err))
                return OrderResult(
                    venue="lighter", symbol=symbol, side=side,
                    status=OrderStatus.FAILED, error=str(err),
                )

            logger.info(
                "lighter.market_order_placed",
                symbol=symbol, side=side, size=size,
                worst_price=worst_price, tx_hash=tx_hash,
            )
            return OrderResult(
                venue="lighter", symbol=symbol, side=side,
                status=OrderStatus.SUBMITTED,
                order_id=str(client_order_index),
                tx_hash=tx_hash,
            )

        except Exception as e:
            logger.error("lighter.market_order_exception", error=str(e))
            return OrderResult(
                venue="lighter", symbol=symbol, side=side,
                status=OrderStatus.FAILED, error=str(e),
            )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """
        Cancel a single order by client_order_index.

        Uses SignerClient.cancel_order(market_index, order_index).
        Note: order_index here equals the client_order_index used at creation.
        """
        self._require_signer()
        market_idx = self._market_index(symbol)

        try:
            tx, tx_hash, err = await self._signer.cancel_order(
                market_index=market_idx,
                order_index=int(order_id),
            )
            if err:
                logger.warning("lighter.cancel_error", order_id=order_id, error=str(err))
                return False

            logger.info("lighter.order_cancelled", order_id=order_id, tx_hash=tx_hash)
            return True

        except Exception as e:
            logger.error("lighter.cancel_exception", error=str(e))
            return False

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> int:
        """
        Cancel all open orders.

        Uses SignerClient.cancel_all_orders() with GTT time-in-force for
        ScheduledCancelAll behavior. For immediate cancel, use IOC.

        Note on Lighter cancel semantics:
          - TIF_IOC → ImmediateCancelAll
          - TIF_GTT → ScheduledCancelAll (cancels after sequencer processes)
          - TIF_POST_ONLY → AbortScheduledCancelAll
        """
        self._require_signer()

        try:
            tx, tx_hash, err = await self._signer.cancel_all_orders(
                time_in_force=TIF_IOC,  # immediate cancel
            )
            if err:
                logger.warning("lighter.cancel_all_error", error=str(err))
                return 0

            logger.info("lighter.all_orders_cancelled", tx_hash=tx_hash)
            return -1  # Lighter doesn't report count; return sentinel

        except Exception as e:
            logger.error("lighter.cancel_all_exception", error=str(e))
            return 0

    async def modify_order(
        self,
        symbol: str,
        order_id: str,
        new_size: Optional[float] = None,
        new_price: Optional[float] = None,
    ) -> bool:
        """
        Modify an existing order in-place (atomic modify, not cancel+replace).

        Uses SignerClient.modify_order(market_index, order_index, base_amount, price).
        """
        self._require_signer()
        market_idx = self._market_index(symbol)

        kwargs = {"market_index": market_idx, "order_index": int(order_id)}
        if new_size is not None:
            kwargs["base_amount"] = self._encode_size(market_idx, new_size)
        if new_price is not None:
            kwargs["price"] = self._encode_price(market_idx, new_price)

        try:
            tx, tx_hash, err = await self._signer.modify_order(**kwargs)
            if err:
                logger.warning("lighter.modify_error", order_id=order_id, error=str(err))
                return False

            logger.info("lighter.order_modified", order_id=order_id, tx_hash=tx_hash)
            return True

        except Exception as e:
            logger.error("lighter.modify_exception", error=str(e))
            return False

    # ── Account / Position Data ──────────────────────────────────────────

    async def get_positions(self) -> list[Position]:
        """
        Fetch current positions for this account.

        GET /api/v1/account?by=index&value={account_index}
        Returns account details including open positions per market.
        Requires auth token for private data.
        """
        if self._account_index is None:
            logger.warning("lighter.no_account_index", msg="Cannot fetch positions")
            return []

        headers = await self._auth_headers()
        resp = await self._client.get(
            "/api/v1/account",
            params={"by": "index", "value": str(self._account_index)},
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

        positions = []
        for pos in data.get("positions", []):
            market_idx = int(pos.get("market_index", 0))
            symbol = self._reverse_market_lookup(market_idx)
            size = float(pos.get("size", 0))
            if abs(size) < 1e-12:
                continue

            positions.append(Position(
                venue="lighter",
                symbol=symbol,
                side=Side.LONG if size > 0 else Side.SHORT,
                size=abs(size),
                entry_price=float(pos.get("entry_price", 0)),
                mark_price=float(pos.get("mark_price", 0)),
                unrealized_pnl=float(pos.get("unrealized_pnl", 0)),
                margin=float(pos.get("margin", 0)),
                leverage=float(pos.get("leverage", 1)),
                funding_accrued=float(pos.get("funding_accrued", 0)),
                liquidation_price=float(pos.get("liquidation_price", 0)),
            ))

        return positions

    async def get_balance(self) -> dict:
        """Fetch account balance (USDC collateral)."""
        if self._account_index is None:
            return {"available": 0, "total": 0, "margin_used": 0}

        headers = await self._auth_headers()
        resp = await self._client.get(
            "/api/v1/account",
            params={"by": "index", "value": str(self._account_index)},
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()

        return {
            "available": float(data.get("available_balance", 0)),
            "total": float(data.get("total_equity", 0)),
            "margin_used": float(data.get("margin_used", 0)),
            "collateral_token": "USDC",
        }

    async def get_pnl_history(self, limit: int = 100) -> list[dict]:
        """
        GET /api/v1/pnl — historical PnL snapshots.
        Requires auth. Useful for NAV tracking.
        """
        if self._account_index is None:
            return []

        headers = await self._auth_headers()
        resp = await self._client.get(
            "/api/v1/pnl",
            params={"account_index": self._account_index, "limit": limit},
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json().get("pnl_entries", [])

    # ── Auth ─────────────────────────────────────────────────────────────

    async def _auth_headers(self) -> dict:
        """
        Generate Authorization header using the SDK's auth token mechanism.

        Auth tokens have max 8h expiry. Structure:
        {expiry_unix}:{account_index}:{api_key_index}:{random_hex}

        Read-only tokens (max 10yr expiry) can also be used for data-only
        access. Structure: ro:{account_index}:{single|all}:{expiry_unix}:{random_hex}
        """
        if self._signer is None:
            return {}

        try:
            auth_token, err = self._signer.create_auth_token_with_expiry(
                deadline=3600,  # 1 hour
                api_key_index=self._api_key_index,
            )
            if err:
                logger.warning("lighter.auth_token_error", error=str(err))
                return {}
            return {"Authorization": auth_token}

        except Exception as e:
            logger.warning("lighter.auth_token_exception", error=str(e))
            return {}

    # ── Gas / Cost ───────────────────────────────────────────────────────

    async def estimate_gas_cost(self, operation: str) -> float:
        """
        Lighter rollup transactions incur zero gas fees.

        All operations (orders, cancels, modifications) are processed by the
        sequencer as rollup transactions with no on-chain gas cost. Only
        priority transactions (L1 force-exit via Desert Mode) require ETH gas.

        Standard accounts: 0 maker, 0 taker fees.
        Premium accounts: 0.2 bps maker, 2 bps taker.
        """
        return 0.0

    # ── Deposit / Withdrawal (informational) ─────────────────────────────

    async def get_deposit_info(self) -> dict:
        """
        Lighter deposits go through the L1 smart contract on Ethereum.
        Supported deposit networks: Ethereum, Arbitrum, Base, Avalanche.
        Minimum deposit: 5 USDC.

        Tokens supported in escrow: USDC, ETH, LIT, LINK, AAVE, UNI, SKY, LDO, AZTEC.
        """
        return {
            "chain": "ethereum",
            "method": "Smart contract deposit on Ethereum L1",
            "supported_networks": ["ethereum", "arbitrum", "base", "avalanche"],
            "collateral": "USDC",
            "min_deposit": 5.0,
            "note": (
                "Deposits are processed by the sequencer after L1 confirmation. "
                "Funds are held in ZK-verified escrow on Ethereum."
            ),
        }

    async def get_withdrawal_info(self) -> dict:
        """
        Two withdrawal modes:
          1. Secure withdrawal: Goes to the same L1 address that created the account.
             Does NOT require L1 private key (only API key signature).
          2. Fast withdrawal / Transfer: Can go to ANY L1 address.
             Requires the account's Ethereum private key.

        Priority (censorship-resistant) withdrawal: Submit directly to L1 contract.
        If sequencer doesn't process within 14 days → Desert Mode activates.
        """
        return {
            "secure_withdrawal": "Same L1 address only; API key sufficient",
            "fast_withdrawal": "Any L1 address; requires ETH private key",
            "priority_withdrawal": "Submit via L1 contract; censorship-resistant",
            "desert_mode_timeout": "14 days of sequencer inactivity",
            "rate_limit": "2 L2Withdraw transactions per minute",
        }

    # ── Helpers ──────────────────────────────────────────────────────────

    def _reverse_market_lookup(self, market_index: int) -> str:
        """Look up canonical symbol from market_index."""
        for symbol, idx in self._market_map.items():
            if idx == market_index:
                return symbol
        return f"UNKNOWN_{market_index}"

    @staticmethod
    def _next_hour() -> datetime:
        """Next top-of-hour timestamp (Lighter funding settles hourly)."""
        now = datetime.now(timezone.utc)
        return now.replace(minute=0, second=0, microsecond=0).replace(
            hour=now.hour + 1 if now.minute > 0 or now.second > 0 else now.hour
        )

    async def disconnect(self):
        """Clean up HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None
        self._signer = None
        self.connected = False
        logger.info("lighter.disconnected")
