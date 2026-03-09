# DeFi Perpetual Funding Rate Hedge Bot — Architecture v2
## Tailored for: Hyperliquid · Lighter · Aster · Drift · dYdX · ApeX Omni · Paradex · Ethereal · Injective

---

## Executive Summary

This architecture is purpose-built for **DeFi-native perpetual exchanges only** — no centralized exchange dependencies. Every venue the bot touches is non-custodial, wallet-based, and settles on-chain. This fundamentally changes the hedge bot design compared to a CEX-based system:

- **No API keys** — wallet private keys + message signing replace traditional auth
- **No custodial risk** — funds live in smart contracts, not exchange hot wallets
- **Bridge routing becomes critical** — capital moves between 7+ chains
- **Gas/tx costs are a real P&L factor** — every entry, exit, and funding claim costs gas
- **Settlement finality varies wildly** — from ~200ms (Hyperliquid) to minutes (Ethereum L1)
- **Funding mechanics differ per venue** — 1h, 4h, 8h cycles with different calculation methods

The agentic layer now reasons not just about funding rates but also about chain-specific gas costs, bridge latency, ZK proof settlement times, and wallet nonce management across 7+ chains simultaneously.

---

## 1. Venue Technical Profiles

### 1.1 Hyperliquid
| Property | Detail |
|----------|--------|
| Chain | Hyperliquid L1 (custom, HyperBFT consensus) |
| Settlement | Native on-chain CLOB |
| Finality | ~200ms sub-second |
| Throughput | 200,000 orders/sec |
| Auth | Wallet signature (EVM-compatible) |
| Collateral | USDC (bridged via native Hyperliquid bridge) |
| Funding Cycle | 1 hour |
| API | REST + WebSocket, Python SDK (`hyperliquid-python-sdk`) |
| Deposit Chain | Arbitrum → Hyperliquid bridge |
| Key Feature | Deepest DeFi perp liquidity, HLP vault, fully on-chain orderbook |
| Max Leverage | Up to 50x on majors |

**Adapter Notes:** Hyperliquid uses a unique info/exchange API split. Orders are signed with an EVM wallet and submitted via the exchange endpoint. The SDK handles L1 action signing natively. Funding rate data is available via `info.funding_history()`.

### 1.2 Lighter
| Property | Detail |
|----------|--------|
| Chain | Custom ZK-rollup on Ethereum |
| Settlement | ZK-SNARK verified on Ethereum L1 |
| Finality | Batched ZK proofs posted to Ethereum |
| Auth | Ethereum wallet signing |
| Collateral | USDC (deposited on Ethereum, held in L1 contracts) |
| Funding Cycle | 1 hour |
| API | REST + WebSocket |
| Key Feature | Verifiable matching via ZK proofs, zero fees for retail, "Desert Mode" emergency exit |
| Max Leverage | Up to 50x |

**Adapter Notes:** Lighter's architecture means user funds custody on Ethereum L1 even while trading on the rollup. If the sequencer goes down, users can force-exit via L1 priority operations. The zero-fee model means execution costs approach zero — highly favorable for funding rate capture.

### 1.3 Aster
| Property | Detail |
|----------|--------|
| Chain | Multi-chain (BNB Chain primary, Ethereum, Solana, Arbitrum) |
| Settlement | On-chain liquidity pools + orderbook (Pro mode) |
| Finality | Chain-dependent (~3s BNB, ~12s Ethereum) |
| Auth | Web3 wallet signing (3-address system: main wallet → API wallet → trade wallet) |
| Collateral | USDT, multi-asset |
| Funding Cycle | 8 hours |
| API | Binance-compatible REST + WebSocket (`fstream.asterdex.com`) |
| Key Feature | 1001x leverage (Simple mode), hidden orders, Binance-compatible API |
| Max Leverage | 1001x Simple / 200x Pro |

**Adapter Notes:** Aster uses a Binance-compatible API for Pro mode, making it straightforward to adapt existing Binance futures adapters. The 3-address auth system (main wallet → API wallet → trade wallet) requires careful key management. Hidden orders provide MEV protection.

### 1.4 Drift
| Property | Detail |
|----------|--------|
| Chain | Solana |
| Settlement | On-chain (Solana program) |
| Finality | ~400ms (Solana slot time) |
| Auth | Solana wallet (Ed25519 keypair) |
| Collateral | USDC, multi-asset with margin |
| Funding Cycle | 1 hour |
| API | REST + WebSocket, `driftpy` Python SDK |
| Key Feature | Hybrid AMM + orderbook (DLOB), JIT liquidity, insurance fund |
| Max Leverage | Up to 20x |

**Adapter Notes:** Drift runs entirely on Solana — different signing scheme (Ed25519 vs EVM's secp256k1). The `driftpy` SDK handles account setup, margin calculations, and order placement. Drift's Just-In-Time (JIT) auction mechanism means fills can be more favorable. Compute unit budgets on Solana require careful transaction construction.

### 1.5 dYdX (v4)
| Property | Detail |
|----------|--------|
| Chain | dYdX Chain (Cosmos SDK / CometBFT) |
| Settlement | On-chain orderbook matching |
| Finality | ~1-2 seconds |
| Auth | Cosmos wallet (derived from Ethereum mnemonic) |
| Collateral | USDC |
| Funding Cycle | 1 hour |
| API | REST + WebSocket + gRPC (Cosmos native) |
| Key Feature | Fully decentralized orderbook (validators run matching engine), sovereign chain |
| Max Leverage | Up to 20x |

**Adapter Notes:** dYdX v4 is a sovereign Cosmos chain. Key derivation: Ethereum private key → BIP44 derivation → Cosmos address. The matching engine runs inside the validator consensus — truly decentralized. Uses `@dydxprotocol/v4-client-js` TypeScript SDK or Python client. Subaccount system allows isolated margin positions.

### 1.6 ApeX Omni
| Property | Detail |
|----------|--------|
| Chain | Multi-chain via zkLink X (Ethereum, BNB, Arbitrum, Base, Mantle, Solana) |
| Settlement | ZK-proof validated |
| Finality | Near-instant on L2, ZK batch to L1 |
| Auth | EVM wallet + ZK key derivation |
| Collateral | USDT, USDC, cross-collateral (ETH, BNB) |
| Funding Cycle | 8 hours |
| API | REST + WebSocket, Python SDK (`apexomni`) |
| Key Feature | Chain-agnostic deposits, zero gas, 10K TPS, cross-collateral |
| Max Leverage | Up to 100x |

**Adapter Notes:** ApeX Omni's ZK key derivation is unique — you derive `l2Key` and `pubKeyHash` from your Ethereum private key via their SDK. API authentication uses `apiKey`, `secret`, and `passphrase` obtained during registration. The multi-chain deposit system means you can fund from any supported chain without manual bridging.

### 1.7 Paradex
| Property | Detail |
|----------|--------|
| Chain | Starknet Appchain (dedicated Paradex chain) |
| Settlement | Validity proofs verified on Ethereum via Starknet |
| Finality | Batched proofs to Ethereum |
| Auth | Starknet account + STARK key signing |
| Collateral | USDC |
| Funding Cycle | 1 hour |
| API | REST + WebSocket, `paradex-py` Python SDK, `paradex.js` JS SDK |
| Key Feature | Zero fees for retail UI, privacy (hidden positions/orders/trades), 600+ markets |
| Max Leverage | Up to 50x |
| Token | $DIME (launched March 2026) |

**Adapter Notes:** Paradex uses Cairo/Starknet cryptography — different curve (Stark curve vs secp256k1). The `paradex-py` SDK handles signing. Deposits flow through Starknet bridging. Zero taker fees for UI traders; 0.02% taker for API traders. The privacy features mean competitor bots cannot see your positions.

### 1.8 Ethereal
| Property | Detail |
|----------|--------|
| Chain | EVM Appchain (Converge, built on Arbitrum execution layer) |
| Settlement | On Arbitrum One, data availability via Celestia |
| Finality | Appchain-speed (~1s), final on Arbitrum |
| Auth | EVM wallet signing |
| Collateral | USDe (Ethena synthetic dollar) |
| Funding Cycle | 8 hours |
| API | REST + WebSocket |
| Key Feature | USDe-native collateral (yield-bearing), Ethena ecosystem integration, sub-20ms latency |
| Max Leverage | TBD (mainnet alpha stage) |

**Adapter Notes:** Ethereal is the youngest venue on this list — still in mainnet alpha. USDe as collateral means your margin is earning Ethena yield while being used as trading collateral — effectively "double-dipping" on yield. The Converge chain uses Conduit for sequencing and Pyth for price feeds. 15% of Ethereal tokens allocated to ENA holders.

### 1.9 Injective
| Property | Detail |
|----------|--------|
| Chain | Injective L1 (Cosmos SDK / Tendermint) |
| Settlement | On-chain orderbook (exchange module) |
| Finality | ~1 second (instant finality, Tendermint consensus) |
| Auth | Cosmos wallet (derived from Ethereum key, Injective-specific prefix) |
| Collateral | USDT, multi-asset |
| Funding Cycle | 1 hour |
| API | REST + gRPC + WebSocket (Exchange API + Chain API) |
| Key Feature | Built-in exchange module at chain level, MEV-resistant FBA (Frequent Batch Auction), zero gas fees for trading |
| Max Leverage | Up to 20x |

**Adapter Notes:** Injective's exchange is a native chain module, not a smart contract — the orderbook matching is part of consensus. Two API layers: Chain API (for on-chain operations) and Exchange API (indexer for fast queries). Uses the `injective-py` SDK. Frequent Batch Auctions (FBA) protect against MEV. Subaccount system similar to dYdX.

---

## 2. DeFi-Specific Architecture

### 2.1 The Fundamental Shift: Wallet-Centric Operations

In a CEX bot, you authenticate with API keys. In DeFi, **the wallet IS your identity**. This has cascading implications:

```
┌─────────────────────────────────────────────────────────────────┐
│                     WALLET INFRASTRUCTURE                       │
│                                                                 │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────┐  │
│  │ EVM Wallets  │  │ Solana Wallet│  │ Cosmos Wallets       │  │
│  │ (secp256k1)  │  │ (Ed25519)    │  │ (secp256k1+bech32)   │  │
│  │              │  │              │  │                      │  │
│  │ Hyperliquid  │  │ Drift        │  │ dYdX v4              │  │
│  │ Lighter      │  │              │  │ Injective             │  │
│  │ Aster        │  │              │  │                      │  │
│  │ ApeX Omni    │  │              │  │                      │  │
│  │ Ethereal     │  │              │  │                      │  │
│  └──────┬───────┘  └──────┬───────┘  └──────────┬───────────┘  │
│         │                 │                     │               │
│  ┌──────┴─────────────────┴─────────────────────┴───────────┐  │
│  │              Starknet Wallet (STARK curve)                │  │
│  │              Paradex                                      │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ALL derived from a single master mnemonic via BIP44 paths      │
│  Stored in HashiCorp Vault, never in code or env vars           │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 Key Derivation Strategy

```python
# wallet_manager.py — unified key derivation for all venues
from eth_account import Account
from solders.keypair import Keypair
from cosmpy.crypto.keypairs import PrivateKey as CosmosPrivateKey

class WalletManager:
    """
    Derives all venue-specific keys from a single master mnemonic.
    Each venue gets a unique derivation path for isolation.
    """
    def __init__(self, mnemonic: str):
        self.mnemonic = mnemonic

    def get_evm_wallet(self, index: int = 0) -> dict:
        """For Hyperliquid, Lighter, Aster, ApeX, Ethereal"""
        Account.enable_unaudited_hdwallet_features()
        acct = Account.from_mnemonic(self.mnemonic, account_path=f"m/44'/60'/0'/0/{index}")
        return {"address": acct.address, "private_key": acct.key.hex()}

    def get_solana_wallet(self, index: int = 0) -> dict:
        """For Drift"""
        # Derive Solana keypair from mnemonic via BIP44 path m/44'/501'/index'
        seed = mnemonic_to_seed(self.mnemonic)
        keypair = Keypair.from_seed(derive_path(seed, f"m/44'/501'/{index}'/0'"))
        return {"pubkey": str(keypair.pubkey()), "keypair": keypair}

    def get_cosmos_wallet(self, chain: str, index: int = 0) -> dict:
        """For dYdX (dydx prefix) and Injective (inj prefix)"""
        # Both use secp256k1 but different bech32 prefixes
        evm = self.get_evm_wallet(index)
        if chain == "dydx":
            return derive_dydx_address(evm["private_key"])
        elif chain == "injective":
            return derive_injective_address(evm["private_key"])

    def get_stark_wallet(self, index: int = 0) -> dict:
        """For Paradex — derives STARK key from EVM key"""
        evm = self.get_evm_wallet(index)
        stark_key = derive_stark_key_from_eth(evm["private_key"])
        return {"stark_key": stark_key, "eth_address": evm["address"]}

    def get_apex_zk_keys(self, index: int = 0) -> dict:
        """For ApeX Omni — derives l2Key and pubKeyHash"""
        evm = self.get_evm_wallet(index)
        # ApeX SDK derives ZK keys from ETH private key
        from apexomni import HttpPrivate_v3
        client = HttpPrivate_v3(endpoint, eth_private_key=evm["private_key"])
        zk_keys = client.derive_zk_key(evm["address"])
        return zk_keys  # {l2Key, pubKeyHash, seeds}
```

### 2.3 System Architecture — DeFi Adjusted

```
┌────────────────────────────────────────────────────────────────────┐
│                        AGENTIC BRAIN                               │
│                                                                    │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────────┐   │
│  │ Strategist   │  │ Risk Agent   │  │ Execution Agent       │   │
│  │ Agent        │  │              │  │ (per-venue workers)   │   │
│  └──────┬───────┘  └──────┬───────┘  └───────────┬───────────┘   │
│         │                 │                      │                │
│  ┌──────┴─────────────────┴──────────────────────┴──────────┐    │
│  │                  LangGraph Orchestrator                    │    │
│  │  + Bridge/Gas Agent   + Wallet Nonce Manager              │    │
│  └──────────────────────────┬────────────────────────────────┘    │
└─────────────────────────────┼─────────────────────────────────────┘
                              │
          ┌───────────────────┼───────────────────┐
          ▼                   ▼                   ▼
   ┌────────────┐     ┌────────────┐     ┌─────────────┐
   │ Data       │     │ Bridge     │     │ Gas         │
   │ Pipeline   │     │ Router     │     │ Manager     │
   └────────────┘     └────────────┘     └─────────────┘
          │                   │                   │
          ▼                   ▼                   ▼
   ┌─────────────────────────────────────────────────────┐
   │              DeFi Venue Adapters                     │
   │                                                      │
   │  ┌────────┐ ┌────────┐ ┌────────┐ ┌──────────┐     │
   │  │Hyper-  │ │Lighter │ │Aster   │ │Drift     │     │
   │  │liquid  │ │(ZK-ETH)│ │(BNB/   │ │(Solana)  │     │
   │  │(HL L1) │ │        │ │Multi)  │ │          │     │
   │  └────────┘ └────────┘ └────────┘ └──────────┘     │
   │  ┌────────┐ ┌────────┐ ┌────────┐ ┌──────────┐     │
   │  │dYdX v4 │ │ApeX    │ │Paradex │ │Ethereal  │     │
   │  │(Cosmos)│ │Omni    │ │(Stark- │ │(Converge)│     │
   │  │        │ │(zkLink)│ │net)    │ │          │     │
   │  └────────┘ └────────┘ └────────┘ └──────────┘     │
   │  ┌──────────┐                                       │
   │  │Injective │                                       │
   │  │(Cosmos)  │                                       │
   │  └──────────┘                                       │
   └─────────────────────────────────────────────────────┘
```

---

## 3. New DeFi-Specific Components

### 3.1 Bridge Router (`/services/bridge/`)

Capital rotation between DeFi venues requires cross-chain bridging. This is one of the highest-risk operations.

```python
# bridge_router.py — cross-chain capital movement
class BridgeRouter:
    """
    Routes capital between chains based on:
    - Bridge fees (Li.Fi / Socket API for aggregated quotes)
    - Expected transit time
    - Funding rate differential (must exceed bridge cost)
    - Route safety (prefer canonical bridges over third-party)
    """

    VENUE_CHAINS = {
        "hyperliquid": {"chain": "hyperliquid_l1", "deposit_via": "arbitrum", "token": "USDC"},
        "lighter":     {"chain": "lighter_zk",     "deposit_via": "ethereum", "token": "USDC"},
        "aster":       {"chain": "bnb_chain",      "deposit_via": "bnb",      "token": "USDT"},
        "drift":       {"chain": "solana",          "deposit_via": "solana",   "token": "USDC"},
        "dydx":        {"chain": "dydx_cosmos",     "deposit_via": "noble",    "token": "USDC"},
        "apex":        {"chain": "multi",           "deposit_via": "arbitrum", "token": "USDT"},
        "paradex":     {"chain": "paradex_stark",   "deposit_via": "ethereum", "token": "USDC"},
        "ethereal":    {"chain": "converge",        "deposit_via": "arbitrum", "token": "USDe"},
        "injective":   {"chain": "injective_l1",    "deposit_via": "ethereum", "token": "USDT"},
    }

    CANONICAL_BRIDGES = {
        ("arbitrum", "hyperliquid_l1"): "hyperliquid_native_bridge",
        ("ethereum", "lighter_zk"):     "lighter_l1_deposit",
        ("ethereum", "paradex_stark"):  "starknet_bridge",
        ("ethereum", "dydx_cosmos"):    "noble_ibc",          # ETH → Noble → dYdX via IBC
        ("ethereum", "injective_l1"):   "injective_bridge",
        ("arbitrum", "converge"):       "layerzero",           # Ethereal uses LayerZero
    }

    async def find_optimal_route(self, from_venue: str, to_venue: str, amount_usd: float) -> Route:
        from_chain = self.VENUE_CHAINS[from_venue]["deposit_via"]
        to_chain = self.VENUE_CHAINS[to_venue]["deposit_via"]

        # Check canonical bridge first (safer, but sometimes slower)
        canonical = self.CANONICAL_BRIDGES.get((from_chain, self.VENUE_CHAINS[to_venue]["chain"]))

        # Get aggregated quotes from Li.Fi
        lifi_quote = await self.get_lifi_quote(from_chain, to_chain, amount_usd)

        # Compare: safety vs speed vs cost
        routes = []
        if canonical:
            routes.append(await self.estimate_canonical(canonical, amount_usd))
        routes.append(lifi_quote)

        return self.select_best_route(routes, urgency="normal")

    async def estimate_transit_time(self, route: Route) -> int:
        """Returns estimated seconds for capital to be available on destination."""
        TRANSIT_ESTIMATES = {
            "hyperliquid_native_bridge": 300,    # ~5 min
            "lighter_l1_deposit":        600,    # ~10 min (Ethereum L1 confirmation)
            "starknet_bridge":           1800,   # ~30 min (Starknet proof generation)
            "noble_ibc":                 120,    # ~2 min (IBC is fast)
            "injective_bridge":          300,    # ~5 min
            "layerzero":                 180,    # ~3 min
            "wormhole":                  300,    # ~5 min
            "lifi_aggregated":           600,    # varies, ~10 min average
        }
        return TRANSIT_ESTIMATES.get(route.bridge_id, 900)
```

### 3.2 Gas Manager (`/services/gas/`)

Every on-chain operation costs gas. The bot must maintain gas balances on every chain.

```python
# gas_manager.py — multi-chain gas budget management
class GasManager:
    """
    Maintains gas token balances across all chains the bot operates on.
    Alerts when balances drop below thresholds.
    Auto-refills from a designated gas treasury wallet.
    """

    GAS_TOKENS = {
        "arbitrum":        {"token": "ETH",  "min_balance": 0.05,   "refill_amount": 0.1},
        "ethereum":        {"token": "ETH",  "min_balance": 0.1,    "refill_amount": 0.2},
        "bnb_chain":       {"token": "BNB",  "min_balance": 0.05,   "refill_amount": 0.1},
        "solana":          {"token": "SOL",  "min_balance": 1.0,    "refill_amount": 2.0},
        "hyperliquid_l1":  {"token": "None", "min_balance": 0,      "refill_amount": 0},  # no gas fees
        "dydx_cosmos":     {"token": "USDC", "min_balance": 5.0,    "refill_amount": 10.0},
        "injective_l1":    {"token": "INJ",  "min_balance": 1.0,    "refill_amount": 2.0},
        "paradex_stark":   {"token": "DIME", "min_balance": 50,     "refill_amount": 100},
    }

    # Venues with zero/negligible gas costs (huge advantage for funding rate capture)
    ZERO_GAS_VENUES = ["hyperliquid", "lighter", "apex", "aster"]

    async def check_all_balances(self) -> dict:
        balances = {}
        for chain, config in self.GAS_TOKENS.items():
            if config["min_balance"] == 0:
                continue
            balance = await self.get_native_balance(chain)
            balances[chain] = {
                "balance": balance,
                "token": config["token"],
                "healthy": balance >= config["min_balance"],
            }
            if balance < config["min_balance"]:
                await self.trigger_refill(chain)
        return balances

    def estimate_operation_cost(self, venue: str, operation: str) -> float:
        """Estimate gas cost in USD for a specific operation."""
        COST_ESTIMATES_USD = {
            # venue: {operation: estimated_cost_usd}
            "hyperliquid": {"open": 0, "close": 0, "funding_claim": 0},
            "lighter":     {"open": 0, "close": 0, "funding_claim": 0},
            "aster":       {"open": 0.02, "close": 0.02, "funding_claim": 0},
            "drift":       {"open": 0.01, "close": 0.01, "funding_claim": 0.005},
            "dydx":        {"open": 0.01, "close": 0.01, "funding_claim": 0},
            "apex":        {"open": 0, "close": 0, "funding_claim": 0},
            "paradex":     {"open": 0.001, "close": 0.001, "funding_claim": 0},
            "ethereal":    {"open": 0.005, "close": 0.005, "funding_claim": 0},
            "injective":   {"open": 0, "close": 0, "funding_claim": 0},  # zero gas for trading
        }
        return COST_ESTIMATES_USD.get(venue, {}).get(operation, 0.05)
```

### 3.3 Nonce Manager (`/services/nonce/`)

Multiple chains = multiple nonce trackers. Race conditions are deadly.

```python
# nonce_manager.py — thread-safe nonce tracking per chain
import asyncio

class NonceManager:
    """
    Prevents nonce collisions when sending rapid transactions.
    Critical for atomic hedge entries across venues on the same chain.
    """
    def __init__(self):
        self._locks = {}    # chain -> asyncio.Lock
        self._nonces = {}   # chain -> int

    async def get_and_increment(self, chain: str, wallet_address: str) -> int:
        if chain not in self._locks:
            self._locks[chain] = asyncio.Lock()

        async with self._locks[chain]:
            if chain not in self._nonces:
                self._nonces[chain] = await self._fetch_onchain_nonce(chain, wallet_address)
            nonce = self._nonces[chain]
            self._nonces[chain] += 1
            return nonce

    async def reset(self, chain: str, wallet_address: str):
        """Reset nonce from on-chain state (use after stuck tx)."""
        async with self._locks[chain]:
            self._nonces[chain] = await self._fetch_onchain_nonce(chain, wallet_address)
```

---

## 4. DeFi Venue Adapters — Unified Interface

### 4.1 Base Adapter (DeFi-specific)

```python
# base_defi_adapter.py
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

@dataclass
class FundingRate:
    venue: str
    symbol: str
    rate: float                 # as decimal (0.0001 = 0.01%)
    annualized: float           # annualized rate for comparison
    next_funding_ts: datetime   # when funding next settles
    cycle_hours: int            # 1, 4, or 8
    predicted_rate: float | None  # some venues provide prediction

@dataclass
class DefiPosition:
    venue: str
    symbol: str
    side: str                   # "long" or "short"
    size: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    margin: float
    leverage: float
    funding_accrued: float
    liquidation_price: float

class BaseDefiAdapter(ABC):
    """Base interface for all DeFi perpetual venue adapters."""

    @abstractmethod
    async def connect(self, wallet_manager, wallet_index: int = 0):
        """Initialize connection with wallet credentials."""
        ...

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> FundingRate:
        ...

    @abstractmethod
    async def get_funding_history(self, symbol: str, limit: int = 100) -> list[FundingRate]:
        ...

    @abstractmethod
    async def get_orderbook(self, symbol: str, depth: int = 20) -> dict:
        ...

    @abstractmethod
    async def place_limit_order(self, symbol: str, side: str, size: float, price: float) -> dict:
        ...

    @abstractmethod
    async def place_market_order(self, symbol: str, side: str, size: float) -> dict:
        ...

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        ...

    @abstractmethod
    async def get_positions(self) -> list[DefiPosition]:
        ...

    @abstractmethod
    async def get_margin_info(self) -> dict:
        ...

    @abstractmethod
    async def estimate_gas_cost(self, operation: str) -> float:
        """Return estimated gas cost in USD for operation."""
        ...

    @abstractmethod
    async def get_deposit_address(self) -> dict:
        """Return chain + address for deposits."""
        ...

    @abstractmethod
    async def withdraw(self, amount: float, destination_chain: str, destination_address: str) -> dict:
        ...

    def normalize_symbol(self, symbol: str) -> str:
        """Normalize symbol to venue-specific format."""
        raise NotImplementedError
```

### 4.2 Hyperliquid Adapter

```python
# hyperliquid_adapter.py
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from hyperliquid.utils import constants

class HyperliquidAdapter(BaseDefiAdapter):
    def __init__(self):
        self.info = None
        self.exchange = None

    async def connect(self, wallet_manager, wallet_index=0):
        wallet = wallet_manager.get_evm_wallet(wallet_index)
        self.info = Info(constants.MAINNET_API_URL, skip_ws=False)
        self.exchange = Exchange(
            wallet=wallet["private_key"],
            base_url=constants.MAINNET_API_URL,
        )
        self.address = wallet["address"]

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        meta = self.info.meta()
        # Find the asset index for the symbol
        asset_idx = next(i for i, a in enumerate(meta["universe"]) if a["name"] == symbol)
        ctx = self.info.meta_and_asset_ctxs()
        asset_ctx = ctx[1][asset_idx]

        rate = float(asset_ctx["funding"])
        return FundingRate(
            venue="hyperliquid",
            symbol=symbol,
            rate=rate,
            annualized=rate * 24 * 365,  # 1h funding = 8760 periods/year
            next_funding_ts=self._next_hour(),
            cycle_hours=1,
            predicted_rate=float(asset_ctx.get("premiumRate", 0)),
        )

    async def place_limit_order(self, symbol: str, side: str, size: float, price: float):
        is_buy = side.lower() == "buy"
        result = self.exchange.order(
            symbol, is_buy, size, price,
            {"limit": {"tif": "Gtc"}},
        )
        return result

    async def place_market_order(self, symbol: str, side: str, size: float):
        is_buy = side.lower() == "buy"
        # Hyperliquid uses aggressive limit as "market"
        ob = self.info.l2_snapshot(symbol)
        if is_buy:
            price = float(ob["levels"][1][0]["px"]) * 1.005  # 0.5% above best ask
        else:
            price = float(ob["levels"][0][0]["px"]) * 0.995  # 0.5% below best bid

        result = self.exchange.order(
            symbol, is_buy, size, price,
            {"limit": {"tif": "Ioc"}},  # IOC = immediate-or-cancel
        )
        return result

    async def get_positions(self) -> list[DefiPosition]:
        user_state = self.info.user_state(self.address)
        positions = []
        for pos in user_state.get("assetPositions", []):
            p = pos["position"]
            positions.append(DefiPosition(
                venue="hyperliquid",
                symbol=p["coin"],
                side="long" if float(p["szi"]) > 0 else "short",
                size=abs(float(p["szi"])),
                entry_price=float(p["entryPx"]),
                mark_price=float(p.get("markPx", 0)),
                unrealized_pnl=float(p["unrealizedPnl"]),
                margin=float(p.get("marginUsed", 0)),
                leverage=float(p.get("leverage", {}).get("value", 1)),
                funding_accrued=float(p.get("cumFunding", {}).get("sinceOpen", 0)),
                liquidation_price=float(p.get("liquidationPx", 0)),
            ))
        return positions

    async def estimate_gas_cost(self, operation: str) -> float:
        return 0.0  # Hyperliquid has zero gas fees
```

### 4.3 Drift Adapter (Solana-specific)

```python
# drift_adapter.py
from driftpy.drift_client import DriftClient
from driftpy.accounts import get_perp_market_account
from solana.rpc.async_api import AsyncClient
from solders.keypair import Keypair

class DriftAdapter(BaseDefiAdapter):
    def __init__(self):
        self.client = None

    async def connect(self, wallet_manager, wallet_index=0):
        wallet = wallet_manager.get_solana_wallet(wallet_index)
        self.keypair = wallet["keypair"]

        connection = AsyncClient("https://api.mainnet-beta.solana.com")
        self.client = DriftClient(
            connection=connection,
            wallet=self.keypair,
            env="mainnet-beta",
        )
        await self.client.subscribe()

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        market_index = self._symbol_to_market_index(symbol)
        market = await get_perp_market_account(self.client.program, market_index)

        # Drift stores funding as cumulative — we need the rate
        last_rate = market.amm.last_funding_rate / 1e9  # PRICE_PRECISION
        predicted = market.amm.last24h_avg_funding_rate / 1e9

        return FundingRate(
            venue="drift",
            symbol=symbol,
            rate=last_rate,
            annualized=last_rate * 24 * 365,
            next_funding_ts=self._next_hour(),
            cycle_hours=1,
            predicted_rate=predicted,
        )

    async def place_limit_order(self, symbol: str, side: str, size: float, price: float):
        from driftpy.types import OrderParams, OrderType, PositionDirection
        market_index = self._symbol_to_market_index(symbol)

        order_params = OrderParams(
            order_type=OrderType.LIMIT(),
            market_index=market_index,
            direction=PositionDirection.LONG() if side == "buy" else PositionDirection.SHORT(),
            base_asset_amount=int(size * 1e9),
            price=int(price * 1e6),
        )
        tx = await self.client.place_perp_order(order_params)
        return {"tx_sig": str(tx), "status": "submitted"}

    async def estimate_gas_cost(self, operation: str) -> float:
        # Solana tx fee: ~5000 lamports = ~$0.001 at current SOL price
        # But CU budget can increase this for complex operations
        return 0.01  # ~$0.01 per operation
```

### 4.4 dYdX v4 Adapter (Cosmos-specific)

```python
# dydx_adapter.py
from dydx_v4_client import NodeClient, Wallet, OrderFlags
from dydx_v4_client.indexer.rest import IndexerClient

class DydxAdapter(BaseDefiAdapter):
    def __init__(self):
        self.node_client = None
        self.indexer = None

    async def connect(self, wallet_manager, wallet_index=0):
        cosmos_wallet = wallet_manager.get_cosmos_wallet("dydx", wallet_index)
        self.wallet = Wallet.from_mnemonic(wallet_manager.mnemonic)
        self.subaccount = self.wallet.subaccount(0)

        self.node_client = await NodeClient.connect("dydx-mainnet-1")
        self.indexer = IndexerClient("https://indexer.dydx.trade")

    async def get_funding_rate(self, symbol: str) -> FundingRate:
        markets = await self.indexer.markets.get_perpetual_markets()
        market = markets["markets"][symbol]

        rate = float(market["nextFundingRate"])
        return FundingRate(
            venue="dydx",
            symbol=symbol,
            rate=rate,
            annualized=rate * 24 * 365,
            next_funding_ts=self._parse_ts(market["nextFundingAt"]),
            cycle_hours=1,
            predicted_rate=rate,  # dYdX provides predicted as "next"
        )

    async def place_limit_order(self, symbol: str, side: str, size: float, price: float):
        market_info = await self._get_market_info(symbol)
        order = await self.node_client.place_order(
            subaccount=self.subaccount,
            market=symbol,
            side=side.upper(),
            price=price,
            size=size,
            client_id=self._generate_client_id(),
            order_flags=OrderFlags.LONG_TERM,
            good_til_block_time=self._gtbt(minutes=5),
        )
        return order
```

---

## 5. DeFi-Specific Strategist Agent

The strategist must now factor in DeFi-specific costs and constraints:

```python
# strategist_agent.py — DeFi-aware version
STRATEGIST_SYSTEM_PROMPT = """
You are the Strategist Agent for a DeFi-only funding rate arbitrage system.

VENUES YOU MANAGE:
- Hyperliquid (HL L1, 1h funding, zero gas, deepest liquidity)
- Lighter (ETH ZK-rollup, 1h funding, zero fees, ZK-verified)
- Aster (BNB/Multi, 8h funding, Binance-compatible API, hidden orders)
- Drift (Solana, 1h funding, hybrid AMM+CLOB, JIT liquidity)
- dYdX v4 (Cosmos chain, 1h funding, decentralized orderbook)
- ApeX Omni (Multi-chain zkLink, 8h funding, cross-collateral)
- Paradex (Starknet appchain, 1h funding, zero fees, privacy)
- Ethereal (Converge/Arbitrum, 8h funding, USDe collateral = yield-bearing margin)
- Injective (Cosmos L1, 1h funding, FBA anti-MEV, native exchange module)

DEFI-SPECIFIC DECISION FACTORS:
1. Gas costs per trade — factor into minimum viable funding rate
2. Bridge transit time — capital locked during transfer cannot earn funding
3. Settlement finality — affects when you can confirm positions are open
4. Collateral type differences:
   - Most venues: USDC or USDT
   - Ethereal: USDe (earns Ethena yield ON TOP of funding = double yield)
   - Some venues: cross-collateral (ETH, BNB as margin)
5. Funding cycle length differences:
   - 1h venues (Hyperliquid, Lighter, Drift, dYdX, Paradex, Injective): 8760 payments/yr
   - 8h venues (Aster, ApeX, Ethereal): 1095 payments/yr
   - Compare ANNUALIZED rates, not raw rates
6. Fee structures:
   - Zero-fee venues (Hyperliquid, Lighter, Paradex, Injective): no execution drag
   - Low-fee venues (Drift ~0.03%, dYdX ~0.02%): minimal drag
   - Standard venues (Aster, ApeX ~0.025%): factor into threshold
7. Liquidity depth varies hugely — Hyperliquid >> others for BTC/ETH
8. Privacy: Paradex hidden positions mean competitors cannot front-run your entries

HEDGING STRATEGY IN DEFI:
Since we're DeFi-only (no CEX spot leg), our delta-neutral hedge pairs are:
- VENUE A (short perp) + VENUE B (long perp) — "perp-perp basis"
- One leg collects funding, other leg pays — net positive when rate spread is wide
- Alternative: short perp on high-rate venue + spot on DEX (Uniswap/Jupiter)

MINIMUM VIABLE FUNDING RATE CALCULATION:
min_annual_rate = (entry_fee + exit_fee + gas_entry + gas_exit + bridge_cost) / hold_period_years
Only recommend entry when annualized rate exceeds this by >2x.

ROTATION RULES:
When rotating capital between venues:
- Bridge transit time means 0 funding earned during transfer
- Opportunity cost = (transit_hours / 8760) * current_rate * position_size
- Only rotate if new_rate - old_rate > opportunity_cost_annualized + bridge_fee

OUTPUT FORMAT:
{
  "actions": [
    {
      "type": "ENTER_HEDGE" | "EXIT_HEDGE" | "ROTATE" | "HOLD",
      "symbol": "BTC",
      "short_venue": "hyperliquid",
      "long_venue": "drift",
      "size_usd": 50000,
      "expected_annual_yield": 0.22,
      "reasoning": "..."
    }
  ]
}
"""

# DeFi-specific tools the strategist can call
@tool
def get_all_venue_funding_rates() -> dict:
    """Fetch current funding rates across all 9 DeFi venues."""
    return {
        venue: adapter.get_funding_rate(symbol)
        for venue, adapter in venue_adapters.items()
        for symbol in MONITORED_SYMBOLS
    }

@tool
def estimate_bridge_cost(from_venue: str, to_venue: str, amount_usd: float) -> dict:
    """Estimate cost and time to bridge capital between venues."""
    return bridge_router.find_optimal_route(from_venue, to_venue, amount_usd)

@tool
def calculate_net_yield(
    short_venue: str, long_venue: str, symbol: str, hold_days: int
) -> dict:
    """
    Calculate net yield after ALL costs for a perp-perp hedge.
    Includes: funding collected, funding paid, gas, trading fees, bridge costs.
    """
    short_rate = get_funding_rate(short_venue, symbol)
    long_rate = get_funding_rate(long_venue, symbol)
    short_fees = get_fee_schedule(short_venue)
    long_fees = get_fee_schedule(long_venue)
    short_gas = estimate_gas_cost(short_venue, "open") + estimate_gas_cost(short_venue, "close")
    long_gas = estimate_gas_cost(long_venue, "open") + estimate_gas_cost(long_venue, "close")

    gross_annual = (short_rate.annualized - long_rate.annualized)  # net funding
    cost_drag = (short_fees.taker + long_fees.taker) * 2  # entry + exit on both legs
    gas_drag = (short_gas + long_gas) / (hold_days / 365)  # amortized gas

    return {
        "gross_annual_yield": gross_annual,
        "net_annual_yield": gross_annual - cost_drag - gas_drag,
        "breakeven_days": calculate_breakeven(cost_drag + gas_drag, gross_annual),
    }

@tool
def get_ethereal_double_yield(symbol: str) -> dict:
    """
    Ethereal-specific: Calculate combined yield from
    funding rate + USDe staking yield on collateral.
    """
    funding = get_funding_rate("ethereal", symbol)
    usde_yield = get_current_usde_apy()  # typically 15-25% APY
    return {
        "funding_yield": funding.annualized,
        "usde_collateral_yield": usde_yield,
        "combined_yield": funding.annualized + usde_yield,
    }
```

---

## 6. DeFi Hedge Execution — Perp-Perp Pairs

Since we have no CEX spot leg, the hedge is **perp vs. perp across venues**:

```python
# execution_agent.py — DeFi perp-perp hedge execution
async def execute_defi_hedge_entry(
    symbol: str,
    short_venue: str,  # venue where we SHORT (collecting funding)
    long_venue: str,    # venue where we LONG (paying funding, but less)
    size_usd: float,
) -> dict:
    """
    Open a delta-neutral perp-perp hedge across two DeFi venues.

    Strategy: Short on high-funding venue, Long on low-funding venue.
    Net funding = (short venue rate) - (long venue rate) > 0

    Unlike CEX, we must handle:
    - Different signing schemes (EVM vs Solana vs Cosmos vs Stark)
    - No guaranteed simultaneous execution (different chains)
    - Gas costs on each chain
    """
    short_adapter = venue_adapters[short_venue]
    long_adapter = venue_adapters[long_venue]

    # 1. Pre-flight checks
    short_ob = await short_adapter.get_orderbook(symbol)
    long_ob = await long_adapter.get_orderbook(symbol)

    short_slippage = estimate_slippage(short_ob, size_usd, "sell")
    long_slippage = estimate_slippage(long_ob, size_usd, "buy")
    total_slippage = short_slippage + long_slippage

    if total_slippage > 0.20:  # 20 bps combined max
        return {"status": "ABORTED", "reason": f"slippage {total_slippage:.2f}% too high"}

    # 2. Check gas/margin on both venues
    short_margin = await short_adapter.get_margin_info()
    long_margin = await long_adapter.get_margin_info()

    if short_margin["available"] < size_usd * 0.1:  # need 10% margin minimum
        return {"status": "ABORTED", "reason": f"insufficient margin on {short_venue}"}

    # 3. Execute BOTH legs as fast as possible
    # Note: these are on DIFFERENT chains, so we can't guarantee atomicity
    # We use asyncio.gather to minimize time delta between legs
    short_price = float(short_ob["bids"][1]["price"])   # sell into bids
    long_price = float(long_ob["asks"][1]["price"])      # buy from asks
    qty = size_usd / ((short_price + long_price) / 2)

    short_task = short_adapter.place_limit_order(symbol, "sell", qty, short_price)
    long_task = long_adapter.place_limit_order(symbol, "buy", qty, long_price)

    results = await asyncio.gather(short_task, long_task, return_exceptions=True)
    short_result, long_result = results

    # 4. Handle partial execution
    if isinstance(short_result, Exception) or isinstance(long_result, Exception):
        # One leg failed — must unwind the other
        await rollback_defi_hedge(short_result, long_result, short_adapter, long_adapter, symbol)
        return {"status": "ROLLED_BACK", "reason": "partial_execution_failure"}

    # 5. Verify fills
    short_filled = await verify_fill(short_adapter, short_result)
    long_filled = await verify_fill(long_adapter, long_result)

    if abs(short_filled["qty"] - long_filled["qty"]) / short_filled["qty"] > 0.02:
        # Legs are >2% imbalanced — need to rebalance
        await rebalance_legs(short_adapter, long_adapter, short_filled, long_filled, symbol)

    return {
        "status": "FILLED",
        "position_id": generate_position_id(),
        "short_leg": {"venue": short_venue, **short_filled},
        "long_leg": {"venue": long_venue, **long_filled},
        "entry_basis": short_filled["avg_price"] - long_filled["avg_price"],
        "total_gas_cost": (
            await short_adapter.estimate_gas_cost("open") +
            await long_adapter.estimate_gas_cost("open")
        ),
    }
```

---

## 7. DeFi-Specific Risk Considerations

### 7.1 Risk Matrix

| Risk | CEX Impact | DeFi Impact | Mitigation |
|------|-----------|-------------|------------|
| Exchange hack | Lose all custodied funds | Smart contract exploit (partial risk) | Spread across venues, use proven contracts |
| Withdrawal freeze | Cannot access funds | Can force-withdraw on ZK venues (Lighter, Paradex) | Prefer venues with escape hatches |
| Funding flip | Lose money on position | Same + gas cost to exit | Faster detection via 1h cycles |
| Bridge exploit | N/A | Total loss of bridged funds | Use canonical bridges only |
| Smart contract bug | N/A | Loss of deposited margin | Max allocation per venue |
| Oracle manipulation | Price manipulation | Same — Pyth/Chainlink feed manipulation | Venues with multi-oracle (dYdX, Drift) |
| Chain downtime | N/A | Cannot exit positions | Diversify across chains (Cosmos, Solana, EVM, Starknet) |
| MEV/front-running | N/A | Entry slippage from MEV bots | Paradex (privacy), Injective (FBA), Aster (hidden orders) |
| Nonce collision | N/A | Stuck transactions | Nonce manager with reset capability |
| Gas spike | N/A | Operations become expensive | Zero-gas venues as primary, gas reserves for others |

### 7.2 Updated Risk Agent

```python
RISK_SYSTEM_PROMPT_DEFI = """
You are the Risk Agent for a DeFi-only funding rate hedge system.

ADDITIONAL DEFI RISK RULES:
- Max allocation per venue: 20% of NAV (9 venues = high diversification)
- Max allocation per CHAIN: 35% (e.g., no more than 35% on Cosmos-based venues)
- Bridge exposure: never have more than 10% of NAV in-transit via bridges
- Smart contract age: flag any venue whose contracts are <6 months old
- Oracle check: verify mark prices across venues diverge <0.5% (oracle manipulation signal)
- Chain liveness: if a chain misses blocks for >30s, reduce exposure on that chain's venues
- Gas reserve check: ensure all chains have sufficient gas before approving trades
- Stale funding: if funding rate data is >5 minutes old on any venue, do NOT trade there

VENUE TRUST TIERS:
- Tier 1 (higher allocation ok): Hyperliquid, dYdX, Drift — battle-tested, high TVL
- Tier 2 (moderate allocation): Lighter, Paradex, Injective — strong tech, growing
- Tier 3 (conservative allocation): Aster, ApeX, Ethereal — newer or less proven
"""
```

---

## 8. Funding Rate Data Pipeline — All 9 Venues

```python
# funding_rate_collector.py — unified multi-venue collector
class DeFiFundingCollector:
    """
    Collects funding rates from all 9 DeFi venues every 30 seconds.
    Normalizes to annualized rates for comparison.
    Stores in TimescaleDB.
    """

    SYMBOLS = ["BTC", "ETH", "SOL", "ARB", "DOGE", "AVAX", "LINK", "WIF", "PEPE"]

    def __init__(self, adapters: dict[str, BaseDefiAdapter]):
        self.adapters = adapters

    async def collect_cycle(self):
        """One collection cycle across all venues + symbols."""
        tasks = []
        for venue_name, adapter in self.adapters.items():
            for symbol in self.SYMBOLS:
                normalized_symbol = adapter.normalize_symbol(symbol)
                if normalized_symbol:  # venue supports this pair
                    tasks.append(self._collect_one(venue_name, adapter, symbol, normalized_symbol))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Build funding rate matrix
        matrix = {}
        for result in results:
            if isinstance(result, Exception):
                continue
            key = (result.venue, result.symbol)
            matrix[key] = result

        # Detect cross-venue opportunities
        opportunities = self._find_opportunities(matrix)
        return matrix, opportunities

    def _find_opportunities(self, matrix: dict) -> list:
        """
        Find perp-perp hedge opportunities where:
        short_venue_rate - long_venue_rate > minimum_threshold
        """
        opps = []
        for symbol in self.SYMBOLS:
            rates_for_symbol = [
                (venue, rate) for (venue, sym), rate in matrix.items()
                if sym == symbol
            ]
            if len(rates_for_symbol) < 2:
                continue

            # Sort by annualized rate descending
            rates_for_symbol.sort(key=lambda x: x[1].annualized, reverse=True)

            best_short = rates_for_symbol[0]   # highest rate = best to short
            best_long = rates_for_symbol[-1]    # lowest rate = cheapest to long

            spread = best_short[1].annualized - best_long[1].annualized

            if spread > 0.10:  # >10% annualized spread
                opps.append({
                    "symbol": symbol,
                    "short_venue": best_short[0],
                    "short_rate_annual": best_short[1].annualized,
                    "long_venue": best_long[0],
                    "long_rate_annual": best_long[1].annualized,
                    "spread_annual": spread,
                    "short_cycle": best_short[1].cycle_hours,
                    "long_cycle": best_long[1].cycle_hours,
                })
        return opps
```

---

## 9. Ethereal Double-Yield Strategy

Ethereal is unique: USDe collateral earns Ethena staking yield while simultaneously serving as margin for perp positions. The strategist should always evaluate Ethereal with a yield boost.

```python
# ethereal_yield_calculator.py
class EtherealYieldCalculator:
    """
    Ethereal's edge: collateral earns yield even while being used as margin.
    This effectively adds 15-25% APY to any funding rate capture.
    """

    async def calculate_total_yield(self, symbol: str, position_size_usd: float) -> dict:
        # Layer 1: Funding rate on the perp position
        funding_rate = await ethereal_adapter.get_funding_rate(symbol)

        # Layer 2: USDe yield on the collateral
        # USDe earns from Ethena's delta-neutral strategy (stETH yield + funding)
        usde_apy = await self.get_current_usde_apy()  # typically 15-25%

        # Layer 3: Potential ETHREAL token incentives (Season 0 points)
        incentive_boost = await self.estimate_token_incentives(position_size_usd)

        return {
            "funding_yield_annual": funding_rate.annualized,
            "usde_collateral_yield": usde_apy,
            "incentive_yield_estimate": incentive_boost,
            "total_yield_annual": funding_rate.annualized + usde_apy + incentive_boost,
            "note": "USDe yield is earned on FULL margin, not just the notional position"
        }
```

---

## 10. Directory Structure — DeFi Version

```
hedgehog/
├── agents/
│   ├── orchestrator.py              # LangGraph master graph
│   ├── strategist_agent.py          # DeFi-aware alpha generation
│   ├── risk_agent.py                # DeFi risk gatekeeper
│   ├── execution_agent.py           # Perp-perp hedge execution
│   ├── bridge_agent.py              # Cross-chain capital rotation reasoning
│   └── tools/
│       ├── funding_tools.py         # All venue funding rate tools
│       ├── orderbook_tools.py       # Cross-venue orderbook tools
│       ├── bridge_tools.py          # Bridge cost/time estimation
│       ├── gas_tools.py             # Gas cost estimation
│       ├── yield_tools.py           # Ethereal double-yield, incentive calc
│       └── risk_tools.py            # VaR, margin, concentration tools
├── adapters/
│   ├── base_defi_adapter.py         # Abstract interface
│   ├── hyperliquid_adapter.py       # Hyperliquid L1 (Python SDK)
│   ├── lighter_adapter.py           # Lighter ZK-rollup (REST/WS)
│   ├── aster_adapter.py             # Aster (Binance-compatible API)
│   ├── drift_adapter.py             # Drift on Solana (driftpy SDK)
│   ├── dydx_adapter.py              # dYdX v4 Cosmos (v4-client)
│   ├── apex_adapter.py              # ApeX Omni (apexomni SDK)
│   ├── paradex_adapter.py           # Paradex on Starknet (paradex-py)
│   ├── ethereal_adapter.py          # Ethereal on Converge (REST/WS)
│   └── injective_adapter.py         # Injective L1 (injective-py)
├── services/
│   ├── data/
│   │   ├── funding_rate_collector.py    # All 9 venues, normalized
│   │   ├── orderbook_streamer.py        # WebSocket streams per venue
│   │   ├── open_interest_tracker.py     # OI across all venues
│   │   └── liquidation_feed.py          # Liquidation events
│   ├── bridge/
│   │   ├── bridge_router.py             # Li.Fi/Socket + canonical bridges
│   │   ├── bridge_monitor.py            # Track in-transit capital
│   │   └── canonical_bridges.py         # Direct bridge integrations
│   ├── wallet/
│   │   ├── wallet_manager.py            # Multi-chain key derivation
│   │   ├── nonce_manager.py             # Per-chain nonce tracking
│   │   └── gas_manager.py              # Gas balance monitoring + refill
│   ├── capital/
│   │   ├── portfolio_tracker.py         # Real-time NAV across all venues
│   │   ├── capital_allocator.py         # Kelly + venue tier weighting
│   │   └── rebalancer.py               # Cross-venue capital optimization
│   ├── risk/
│   │   ├── risk_engine.py               # DeFi-aware risk checks
│   │   ├── circuit_breaker.py           # Emergency close all positions
│   │   ├── oracle_monitor.py            # Cross-venue price divergence detection
│   │   └── chain_health_monitor.py      # Block production monitoring
│   └── monitoring/
│       ├── dashboard.py                 # Grafana dashboard
│       ├── alerter.py                   # Telegram/Discord alerts
│       └── metrics_exporter.py          # Prometheus metrics
├── models/
│   ├── position.py
│   ├── order.py
│   ├── funding_rate.py
│   ├── bridge_route.py
│   └── risk_report.py
├── config/
│   ├── venues.yaml                  # Venue configs, symbol mappings
│   ├── strategy.yaml                # Thresholds, pair universe
│   ├── risk.yaml                    # Risk parameters (read-only at runtime)
│   ├── bridges.yaml                 # Bridge configs, canonical routes
│   └── agents.yaml                  # LLM config, prompts
├── scripts/
│   ├── backtest.py
│   ├── paper_trade.py
│   └── deploy.py
├── tests/
├── docker-compose.yml
├── Dockerfile
└── README.md
```

---

## 11. Deployment & Infrastructure

| Component | Technology |
|-----------|-----------|
| Language | Python 3.12 (async), TypeScript for some SDKs |
| Agent Framework | LangGraph + LangChain |
| LLM | Claude Sonnet 4 (Anthropic API) |
| Venue SDKs | `hyperliquid-python-sdk`, `driftpy`, `@dydxprotocol/v4-client`, `apexomni`, `paradex-py`, `injective-py`, custom REST for Lighter/Aster/Ethereal |
| Database | TimescaleDB (time series) + PostgreSQL (state) |
| Cache | Redis (orderbooks, pub/sub, nonce cache) |
| Vector Store | Qdrant (agent memory) |
| Bridge Aggregator | Li.Fi API + canonical bridge contracts |
| Price Feeds | Pyth (primary), Chainlink (secondary cross-check) |
| Monitoring | Prometheus + Grafana |
| Alerting | Telegram Bot + Discord Webhooks |
| Deployment | Docker Compose → Kubernetes |
| Secrets | HashiCorp Vault (all private keys + mnemonics) |

---

## 12. Phased Rollout

```
Phase 1: INFRASTRUCTURE (Week 1-2)
├── Deploy wallet manager, derive keys for all 9 venues
├── Build and test each adapter individually
├── Verify deposit/withdraw on each venue with small amounts ($100)
└── Deploy funding rate collector, validate data quality

Phase 2: DATA COLLECTION (Week 3-4)
├── Run collector 24/7 across all venues
├── Build historical dataset: funding rates, spreads, OI
├── Identify consistent high-spread pairs across venues
└── Map bridge routes, measure actual transit times

Phase 3: BACKTEST (Week 5-6)
├── Replay historical data through agent graph
├── Optimize: minimum rate thresholds, venue preferences, sizing
├── Model bridge costs and transit opportunity cost
└── Target: Sharpe >2.0, max drawdown <3%

Phase 4: PAPER TRADE — OBSERVE_ONLY (Week 7-8)
├── Full agentic loop, live data, no real orders
├── Validate agent reasoning on DeFi-specific decisions
├── Stress test: what happens when a chain goes down?
└── Verify gas management, nonce handling

Phase 5: LIVE — SUPERVISED (Week 9-10)
├── Small capital ($5K-$15K split across 3-4 best venues)
├── Human approves every trade via Telegram
├── Focus on highest-conviction pairs: BTC on Hyperliquid vs Drift
└── Monitor execution quality, slippage, gas costs

Phase 6: LIVE — SEMI_AUTO (Week 11-14)
├── Scale to $25K-$100K across 6+ venues
├── Auto-execute small trades, human approval for >$10K or new pairs
├── Enable bridge rotation between venues
└── Run Ethereal double-yield strategy

Phase 7: LIVE — FULL_AUTO (When Ready)
├── Full autonomy within risk parameters
├── All 9 venues active, bridge rotation enabled
├── Circuit breaker + monitoring as safety net
├── Target: 20-40% APY net of all costs
└── Scale capital as confidence grows
```
