# Lighter Connector — Full Integration Changes

Replaces the stub `LighterAdapter` in `generic_rest_adapter.py` with a proper standalone adapter built from the official Lighter API documentation (`apidocs.lighter.xyz`), Python SDK (`lighter-sdk` on PyPI), and whitepaper (`assets.lighter.xyz/whitepaper.pdf`).

---

## Why the Current Stub Is Wrong

The existing `LighterAdapter` in `generic_rest_adapter.py` inherits from `GenericRestAdapter` and overrides two endpoints:

```python
class LighterAdapter(GenericRestAdapter):
    def _funding_endpoint(self, symbol: str) -> str:
        return f"/api/v1/funding-rate?market={symbol}"
    def _orderbook_endpoint(self, symbol: str, depth: int) -> str:
        return f"/api/v1/orderbook?market={symbol}&depth={depth}"
```

Problems:

1. **Wrong endpoints.** Lighter's funding endpoint is `GET /api/v1/fundings` (with `market_index` as an integer param). The orderbook endpoint is `GET /api/v1/orderBooks` (also integer `market_index`). Neither matches the stub.
2. **Symbols are integers, not strings.** Lighter uses `market_index` (0, 1, 2...) to identify markets. The stub passes string symbols like `"BTC-USD"` — these will 400 on the actual API.
3. **Prices and sizes are scaled integers.** A price of $3100 with `price_decimals=2` is sent as `310000`. A size of 1 ETH with `size_decimals=4` is `10000`. The `GenericRestAdapter.get_funding_rate()` parser will misinterpret all values.
4. **Auth is completely different.** Lighter uses a `SignerClient` with API key private keys (separate from the L1 wallet), account indexes, per-key nonce management, and auth tokens. The generic adapter's `_build_headers()` does none of this.
5. **Trading is impossible.** Orders on Lighter are signed transactions pushed via `sendTx`/`sendTxBatch`, not simple REST POST calls. The generic `place_limit_order` stub logs a warning and returns `FAILED`.
6. **No market metadata.** Lighter requires querying `orderBookDetails` to learn decimal precision per market before any data can be correctly parsed. The stub skips this entirely.

---

## 1. New File: `adapters/lighter_adapter.py`

Drop in the full `LighterAdapter` class (provided separately). Key design decisions from the official docs:

### Connection & Auth

- **Base URL:** `https://mainnet.zklighter.elliot.ai` (not `https://api.lighter.xyz` as the old config had — `api.lighter.xyz` redirects to docs).
- **SDK package:** `lighter-sdk` (PyPI), wraps an OpenAPI-generated client + a Go-based signer binary.
- **SignerClient init:**
  ```python
  client = lighter.SignerClient(
      url=BASE_URL,
      api_private_keys={API_KEY_INDEX: PRIVATE_KEY},
      account_index=ACCOUNT_INDEX,
  )
  ```
- **API key indexes:** 0–1 reserved (desktop/mobile), 2–254 for programmatic use. Each key has its own nonce. The SDK handles nonce auto-increment, but for multi-key systems, manage nonces locally.
- **Auth tokens:** Generated via `create_auth_token_with_expiry()`, max 8h expiry. Structure: `{expiry_unix}:{account_index}:{api_key_index}:{random_hex}`. Read-only tokens can last up to 10 years.
- **Account index resolution:** Query `GET /api/v1/accountsByL1Address?l1_address=0x...` to find the integer account index from an Ethereum address.

### Market Metadata

On connect, the adapter queries `GET /api/v1/orderBookDetails` to build:
- `symbol → market_index` mapping (e.g., `"ETH" → 0`, `"BTC" → 1`)
- Per-market `size_decimals` and `price_decimals` for integer encoding
- `min_base_amount` and `min_quote_amount` for order validation

### Funding Rates

- **Endpoint:** `GET /api/v1/fundings?market_index={idx}&limit=1`
- **Cycle:** 1 hour, TWAP-based premium calculation
- **Clamp:** ±0.5% per hour (from whitepaper)
- **Annualization:** `rate × 24 × 365` (8,760 periods/year)

### Orderbook

- **Snapshot:** `GET /api/v1/orderBooks?market_index={idx}` — aggregated price levels
- **Full depth:** `GET /api/v1/orderBookOrders?market_index={idx}` — individual orders
- **WebSocket:** `order_book:{market_index}` channel streams diffs at 50ms intervals. On subscribe, sends full snapshot; thereafter only state changes. Uses `begin_nonce`/`nonce` fields for continuity verification.

### Order Placement

All orders go through `SignerClient.create_order()` which signs and submits atomically:

```python
tx, tx_hash, err = await client.create_order(
    market_index=0,
    client_order_index=1234,      # uint48, your local order ID
    base_amount=10,               # integer, scaled by size_decimals
    price=3100_00,                # integer, scaled by price_decimals
    is_ask=False,                 # False=buy, True=sell
    order_type=ORDER_TYPE_LIMIT,  # 0=limit, 1=market, 2=SL, 4=TP, 6=TWAP
    time_in_force=TIF_GTT,        # 0=IOC, 1=GTT, 2=PostOnly
    reduce_only=False,
    order_expiry=DEFAULT_28_DAY,
)
```

**Market orders:** Use `ORDER_TYPE_MARKET` + `TIF_IOC`. The `price` parameter is the worst acceptable price (slippage guard), not the desired execution price.

**Cancel:** `cancel_order(market_index, order_index)` where `order_index` = your `client_order_index`.

**Cancel all:** `cancel_all_orders(time_in_force=TIF_IOC)` for immediate; `TIF_GTT` for scheduled.

**Modify:** `modify_order(market_index, order_index, base_amount, price)` — atomic in-place modification.

### Fee Model

- **Standard accounts (default):** Zero maker, zero taker. No fees whatsoever.
- **Premium accounts (opt-in):** 0.2 bps maker, 2 bps taker. Get lower latency (0ms maker, ~150ms taker).
- **Gas:** Zero. Rollup transactions don't incur gas. Only L1 priority transactions (Desert Mode exit) cost ETH gas.

### Rate Limits

| Account Type | Data Endpoints | sendTx/sendTxBatch |
|---|---|---|
| Standard | 60 req/min (flat) | 60 req/min (flat) |
| Premium | 24,000 weighted req/min | 4,000–40,000/min (scales with staked LIT) |

WebSocket limits per IP: 100 connections, 100 subscriptions/connection, 1,000 total subscriptions, 200 client messages/min. Connections drop after 24h.

### Desert Mode (Escape Hatch)

If the sequencer fails to process priority transactions submitted on Ethereum within 14 days, the L1 contracts freeze exchange state and users can force-exit via ZK proofs verified on-chain. This is the `has_escape_hatch: true` flag in `venues.yaml`.

---

## 2. Update `config/venues.yaml`

```yaml
lighter:
    name: Lighter
    chain: lighter_zk
    chain_type: evm
    settlement_chain: ethereum
    funding_cycle_hours: 1
    maker_fee_bps: 0.0           # Standard account — zero fees
    taker_fee_bps: 0.0           # Standard account — zero fees
    max_leverage: 50
    collateral_token: USDC
    api_base_url: https://mainnet.zklighter.elliot.ai   # ← CHANGED from api.lighter.xyz
    ws_url: wss://mainnet.zklighter.elliot.ai/stream    # ← CHANGED from stream.lighter.xyz
    deposit_chain: ethereum                              # Also: arbitrum, base, avalanche
    tier: tier_2
    zero_gas: true
    has_escape_hatch: true
    has_privacy: false
    has_anti_mev: true
    yield_bearing_collateral: false
    symbol_format: "{symbol}-USD"
    # Premium account fees (if opted in):
    # maker_fee_bps: 0.2
    # taker_fee_bps: 2.0
```

**Changes from current config:**
- `api_base_url`: `https://api.lighter.xyz` → `https://mainnet.zklighter.elliot.ai` (the actual API host)
- `ws_url`: `wss://stream.lighter.xyz` → `wss://mainnet.zklighter.elliot.ai/stream`

---

## 3. Update `adapters/generic_rest_adapter.py`

Remove the `LighterAdapter` stub class entirely. It's now a standalone module.

```python
# DELETE this class from generic_rest_adapter.py:
# class LighterAdapter(GenericRestAdapter):
#     """Lighter — ZK-rollup with custom REST API."""
#     def _funding_endpoint(self, symbol: str) -> str:
#         return f"/api/v1/funding-rate?market={symbol}"
#     def _orderbook_endpoint(self, symbol: str, depth: int) -> str:
#         return f"/api/v1/orderbook?market={symbol}&depth={depth}"
```

---

## 4. Update `main.py` Imports

```python
# Change import:
# FROM:
from adapters.generic_rest_adapter import (
    GenericRestAdapter, AsterAdapter, LighterAdapter, EtherealAdapter, ApexAdapter,
)

# TO:
from adapters.generic_rest_adapter import (
    GenericRestAdapter, AsterAdapter, EtherealAdapter, ApexAdapter,
)
from adapters.lighter_adapter import LighterAdapter
```

The `ADAPTER_MAP` entry stays the same:
```python
ADAPTER_MAP = {
    ...
    "lighter": LighterAdapter,
    ...
}
```

---

## 5. Update `connect_adapters()` in `main.py`

The Lighter adapter needs additional kwargs beyond just a private key:

```python
async def connect_adapters(adapters: dict, venue_configs: dict) -> dict:
    connected = {}
    for name, adapter in adapters.items():
        pk = os.getenv(f"{name.upper()}_PRIVATE_KEY", os.getenv("EVM_PRIVATE_KEY", ""))

        # Lighter needs API key credentials, not just an EVM private key
        extra_kwargs = {}
        if name == "lighter":
            pk = os.getenv("LIGHTER_API_KEY_PRIVATE", "")
            extra_kwargs = {
                "account_index": int(os.getenv("LIGHTER_ACCOUNT_INDEX", "0")) or None,
                "api_key_index": int(os.getenv("LIGHTER_API_KEY_INDEX", "2")) or None,
                "l1_address": os.getenv("LIGHTER_L1_ADDRESS", ""),
            }

        if not pk:
            logger.info(f"No private key for {name} — read-only mode")
            try:
                await adapter.connect("", **extra_kwargs)
                connected[name] = adapter
            except Exception:
                pass
            continue

        try:
            success = await adapter.connect(pk, **extra_kwargs)
            if success:
                connected[name] = adapter
        except Exception as e:
            logger.warning(f"Connection error for {name}: {e}")

    return connected
```

---

## 6. Update `.env.example`

```bash
# ── Lighter (ZK-rollup on Ethereum) ──
# API key private key (NOT your L1 ETH wallet key)
# Generate via lighter-sdk: see apidocs.lighter.xyz/docs/api-keys
LIGHTER_API_KEY_PRIVATE=
# Your Lighter account index (integer, query via L1 address)
LIGHTER_ACCOUNT_INDEX=
# API key slot index (2-254; 0-1 reserved for web/mobile)
LIGHTER_API_KEY_INDEX=2
# Your Ethereum L1 address (for account lookup if index unknown)
LIGHTER_L1_ADDRESS=
```

---

## 7. Update `requirements.txt` / `pyproject.toml`

```
lighter-sdk>=1.0.3    # Official Lighter Python SDK (includes signer binary)
```

The SDK is auto-generated from OpenAPI and includes the Go-based signer compiled for the host platform. It requires Python 3.8+.

---

## 8. Update ARCHITECTURE.md §1.2 Lighter Section

Replace the Adapter Notes with accurate information:

```markdown
**Adapter Notes:** Lighter uses integer `market_index` values (not string symbols) for
all API calls. Query `orderBookDetails` on connect to build the symbol mapping and learn
per-market decimal precision for prices and sizes. The official Python SDK (`lighter-sdk`
on PyPI) provides a `SignerClient` that handles transaction signing via an embedded Go
binary, automatic nonce management, and auth token generation. The `ApiClient` wraps the
OpenAPI-generated REST client for data endpoints. Trading goes through `sendTx`/
`sendTxBatch` endpoints — the `create_order` convenience method signs and submits
atomically. Standard accounts have zero fees; premium accounts pay 0.2 bps maker / 2 bps
taker for lower latency. WebSocket at `wss://mainnet.zklighter.elliot.ai/stream` provides
50ms orderbook snapshots and account update streams.
```

---

## 9. Add to `adapters/__init__.py`

```python
from adapters.lighter_adapter import LighterAdapter
```

---

## Summary

| What | Before | After |
|------|--------|-------|
| Adapter location | Stub in `generic_rest_adapter.py` | Standalone `lighter_adapter.py` |
| API base URL | `https://api.lighter.xyz` (docs redirect) | `https://mainnet.zklighter.elliot.ai` |
| WS URL | `wss://stream.lighter.xyz` | `wss://mainnet.zklighter.elliot.ai/stream` |
| Market identification | String symbols | Integer `market_index` from `orderBookDetails` |
| Price/size encoding | Float passthrough | Integer scaled by per-market decimal precision |
| Auth | None (inherits generic headers) | `SignerClient` + API key + nonce + auth tokens |
| Funding endpoint | `/api/v1/funding-rate?market=` (wrong) | `GET /api/v1/fundings?market_index=` |
| Orderbook endpoint | `/api/v1/orderbook?market=` (wrong) | `GET /api/v1/orderBooks?market_index=` |
| Trading | Stub returning `FAILED` | Full `create_order`/`cancel_order`/`modify_order` via SDK |
| Fee awareness | `0 bps` in config only | Adapter documents Standard (0/0) vs Premium (0.2/2) |
| Gas cost | Inherited generic (wrong) | Returns `0.0` — rollup txs are gasless |
| SDK dependency | None | `lighter-sdk>=1.0.3` |
