"""
tests/exchange_verification/venue_params.py

Per-venue parameters for safe testing.
Minimum sizes, price offsets, preferred test symbols, and quirks.
"""

# Minimum order sizes per venue per symbol (in base units)
# These are the smallest orders the venue will accept.
# When in doubt, err on the side of larger minimums — a rejected order
# is better than an accidentally large fill.

VENUE_TEST_PARAMS = {
    "hyperliquid": {
        "test_symbols": ["BTC", "ETH", "SOL"],
        "min_sizes": {
            "BTC": 0.001,     # ~$100 at $100k
            "ETH": 0.01,     # ~$35 at $3.5k
            "SOL": 0.1,      # ~$20 at $200
        },
        "price_offset_pct": 5.0,   # 5% from mid = won't fill
        "supports_market": True,
        "supports_cancel_all": True,
        "has_open_orders_query": True,
        "notes": "Fully implemented. Zero gas. Use IOC for market orders.",
    },
    "lighter": {
        "test_symbols": ["BTC", "ETH"],
        "min_sizes": {
            "BTC": 0.001,
            "ETH": 0.01,
        },
        "price_offset_pct": 5.0,
        "supports_market": True,
        "supports_cancel_all": True,
        "has_open_orders_query": False,
        "notes": "ZK-rollup. Zero fees. Signing not yet implemented.",
    },
    "aster": {
        "test_symbols": ["BTC", "ETH"],
        "min_sizes": {
            "BTC": 0.001,
            "ETH": 0.01,
        },
        "price_offset_pct": 5.0,
        "supports_market": True,
        "supports_cancel_all": True,
        "has_open_orders_query": False,
        "notes": "Binance-compatible API. 3-address auth. Trading stubs only.",
    },
    "drift": {
        "test_symbols": ["BTC", "ETH", "SOL"],
        "min_sizes": {
            "BTC": 0.001,
            "ETH": 0.01,
            "SOL": 0.1,
        },
        "price_offset_pct": 5.0,
        "supports_market": True,
        "supports_cancel_all": True,
        "has_open_orders_query": False,
        "notes": "Solana. Requires driftpy SDK. Ed25519 signing.",
    },
    "dydx": {
        "test_symbols": ["BTC", "ETH"],
        "min_sizes": {
            "BTC": 0.001,
            "ETH": 0.01,
        },
        "price_offset_pct": 5.0,
        "supports_market": True,
        "supports_cancel_all": True,
        "has_open_orders_query": False,
        "notes": "Cosmos chain. Subaccount system. Key derived from ETH mnemonic.",
    },
    "apex": {
        "test_symbols": ["BTC", "ETH"],
        "min_sizes": {
            "BTC": 0.001,
            "ETH": 0.01,
        },
        "price_offset_pct": 5.0,
        "supports_market": True,
        "supports_cancel_all": True,
        "has_open_orders_query": False,
        "notes": "zkLink multi-chain. Trading stubs only.",
    },
    "paradex": {
        "test_symbols": ["BTC", "ETH"],
        "min_sizes": {
            "BTC": 0.001,
            "ETH": 0.01,
        },
        "price_offset_pct": 5.0,
        "supports_market": True,
        "supports_cancel_all": True,
        "has_open_orders_query": False,
        "notes": "Starknet appchain. STARK curve signing. Privacy features.",
    },
    "ethereal": {
        "test_symbols": ["BTC", "ETH"],
        "min_sizes": {
            "BTC": 0.001,
            "ETH": 0.01,
        },
        "price_offset_pct": 5.0,
        "supports_market": True,
        "supports_cancel_all": True,
        "has_open_orders_query": False,
        "notes": "Converge appchain. USDe collateral. Trading stubs only.",
    },
    "injective": {
        "test_symbols": ["BTC", "ETH"],
        "min_sizes": {
            "BTC": 0.001,
            "ETH": 0.01,
        },
        "price_offset_pct": 5.0,
        "supports_market": True,
        "supports_cancel_all": True,
        "has_open_orders_query": False,
        "notes": "Cosmos L1. FBA anti-MEV. injective-py SDK.",
    },
}


def get_venue_params(venue: str) -> dict:
    """Get test parameters for a venue, with sensible defaults."""
    defaults = {
        "test_symbols": ["BTC", "ETH"],
        "min_sizes": {"BTC": 0.001, "ETH": 0.01},
        "price_offset_pct": 5.0,
        "supports_market": False,
        "supports_cancel_all": False,
        "has_open_orders_query": False,
        "notes": "No venue-specific params configured.",
    }
    return VENUE_TEST_PARAMS.get(venue.lower(), defaults)


def get_min_size(venue: str, symbol: str) -> float:
    """Get minimum order size for a venue+symbol pair."""
    params = get_venue_params(venue)
    return params["min_sizes"].get(symbol, 0.001)
