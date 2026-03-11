#!/usr/bin/env python3
"""Hedge Scanner — funding rate arbitrage opportunity finder across all venues.

Scans 6 venues (HL, Aster, Lighter, Apex, dYdX, Drift) for the best
short+long funding rate pairs per symbol, calculates DPY/NDPY/breakeven,
and filters opportunities for entry, rotation, or exit decisions.

Usage:
    python3 scripts/hedge_scanner.py [scan|entry|rotation|exit] [--symbols BTC,ETH,SOL]

Modes:
    scan      — Full rate matrix + ranked opportunities (default)
    entry     — Opportunities meeting entry thresholds
    rotation  — Current positions with better alternatives available
    exit      — Current positions that should be closed

Environment thresholds (.env):
    ENTRY_NDPY         — Min net daily yield for entry (default 0.0008)
    ENTRY_BREAKEVEN    — Max breakeven hours for entry (default 12)
    ROTATION_BREAKEVEN — Max breakeven hours for rotation (default 6)
    EXIT_NDPY          — Min net daily yield to keep position (default 0.0003)
"""
import asyncio
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import httpx


# ── Load .env ────────────────────────────────────────────────────────
def load_env(path):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"'))


load_env(os.path.join(os.path.dirname(__file__), "..", ".env"))

# ── Thresholds from .env ─────────────────────────────────────────────
ENTRY_NDPY = float(os.environ.get("ENTRY_NDPY", "0.0008"))
ENTRY_BREAKEVEN = float(os.environ.get("ENTRY_BREAKEVEN", "12"))
ROTATION_BREAKEVEN = float(os.environ.get("ROTATION_BREAKEVEN", "6"))
EXIT_NDPY = float(os.environ.get("EXIT_NDPY", "0.0003"))

# ── Venue config ─────────────────────────────────────────────────────
# Taker fees: bps / 10000 → decimal (all orders are taker for now)
VENUES = {
    "HL":      {"taker_fee": 0.00045},   # 4.5 bps
    "Aster":   {"taker_fee": 0.00035},   # 3.5 bps
    "Lighter": {"taker_fee": 0.0},       # 0 bps
    "Drift":   {"taker_fee": 0.0003},    # 3.0 bps
    "dYdX":    {"taker_fee": 0.0005},    # 5.0 bps
    "Apex":    {"taker_fee": 0.00025},   # 2.5 bps
}

# All known symbols (superset — used as fallback if ACTIVE_SYMBOLS not set)
ALL_SYMBOLS = [
    "BTC", "ETH", "SOL", "ARB", "DOGE", "AVAX",
    "LINK", "SUI", "WIF", "PEPE", "AAVE", "NEAR",
]

# Active symbols the bot can act on (from .env, comma-separated)
_active_raw = os.environ.get("ACTIVE_SYMBOLS", "")
TARGET_SYMBOLS = (
    [s.strip().upper() for s in _active_raw.split(",") if s.strip()]
    if _active_raw
    else ALL_SYMBOLS
)

TARGET_SET = set(TARGET_SYMBOLS)


# ═══════════════════════════════════════════════════════════════
# FUNDING RATE FETCHERS — each returns {symbol: daily_rate_decimal}
# ═══════════════════════════════════════════════════════════════

async def fetch_hl_rates(client: httpx.AsyncClient) -> dict[str, float]:
    """Hyperliquid: 1h cycle. metaAndAssetCtxs → rate/hr."""
    resp = await client.post(
        "https://api.hyperliquid.xyz/info",
        json={"type": "metaAndAssetCtxs"},
    )
    resp.raise_for_status()
    data = resp.json()
    universe, ctxs = data[0]["universe"], data[1]
    rates = {}
    for u, c in zip(universe, ctxs):
        symbol = u["name"].upper()
        if symbol in TARGET_SET:
            rates[symbol] = float(c.get("funding") or 0) * 24
    return rates


async def fetch_aster_rates(client: httpx.AsyncClient) -> dict[str, float]:
    """Aster: mixed cycles (1h/4h/8h). premiumIndex + cycle detection."""
    base = "https://fapi.asterdex.com"
    resp = await client.get(f"{base}/fapi/v1/premiumIndex")
    resp.raise_for_status()
    all_premium = resp.json()

    # Build cycle map: group by nextFundingTime, sample one per group
    nft_groups: dict[int, list] = defaultdict(list)
    for p in all_premium:
        nft_groups[int(p.get("nextFundingTime", 0))].append(p)

    group_cycle: dict[int, int] = {}
    for nft, items in nft_groups.items():
        try:
            r2 = await client.get(
                f"{base}/fapi/v1/fundingRate",
                params={"symbol": items[0]["symbol"], "limit": 2},
            )
            r2.raise_for_status()
            history = r2.json()
            if len(history) >= 2:
                diff_h = round(
                    (int(history[1]["fundingTime"]) - int(history[0]["fundingTime"]))
                    / (1000 * 3600)
                )
                group_cycle[nft] = diff_h if diff_h in (1, 4, 8) else 8
            else:
                group_cycle[nft] = 8
        except Exception:
            group_cycle[nft] = 8

    rates = {}
    for p in all_premium:
        sym = p.get("symbol", "").replace("USDT", "").replace("USDC", "").upper()
        if sym not in TARGET_SET:
            continue
        rate = float(p.get("lastFundingRate") or 0)
        cycle_h = group_cycle.get(int(p.get("nextFundingTime", 0)), 8)
        rates[sym] = rate * (24 / cycle_h)
    return rates


async def fetch_lighter_rates(client: httpx.AsyncClient) -> dict[str, float]:
    """Lighter: 1h cycle. Bulk funding-rates endpoint."""
    base = "https://mainnet.zklighter.elliot.ai"

    # Fetch all funding rates in one call
    fr_resp = await client.get(f"{base}/api/v1/funding-rates")
    fr_resp.raise_for_status()
    rate_by_mid: dict[int, float] = {}
    for fr in fr_resp.json().get("funding_rates", []):
        rate_by_mid[fr["market_id"]] = float(fr.get("rate", 0))

    # Map market_id → symbol for active markets in our target set
    resp = await client.get(f"{base}/api/v1/orderBookDetails")
    resp.raise_for_status()
    details = resp.json().get("order_book_details", [])

    rates: dict[str, float] = {}
    for d in details:
        if d.get("status") != "active":
            continue
        sym = d.get("symbol", "").replace("-USD", "").upper()
        if sym in TARGET_SET:
            mid = d["market_id"]
            rate = rate_by_mid.get(mid, 0.0)
            rates[sym] = rate * 24  # annualize from 1h to daily
    return rates


async def fetch_apex_rates(client: httpx.AsyncClient) -> dict[str, float]:
    """Apex Omni: 8h cycle. Ticker per symbol."""
    base = "https://omni.apex.exchange"
    symbol_map = {s + "USDT": s for s in TARGET_SYMBOLS}

    async def _get_rate(apex_sym, base_sym):
        try:
            r = await client.get(f"{base}/api/v3/ticker", params={"symbol": apex_sym})
            items = r.json().get("data", [])
            if items and isinstance(items, list) and items[0]:
                return base_sym, float(items[0].get("fundingRate") or 0) * 3
        except Exception:
            pass
        return base_sym, None

    results = await asyncio.gather(
        *[_get_rate(a, b) for a, b in symbol_map.items()]
    )
    return {sym: rate for sym, rate in results if rate is not None}


async def fetch_dydx_rates(client: httpx.AsyncClient) -> dict[str, float]:
    """dYdX v4: 1h cycle. perpetualMarkets → nextFundingRate."""
    resp = await client.get("https://indexer.dydx.trade/v4/perpetualMarkets")
    resp.raise_for_status()
    markets = resp.json().get("markets", {})
    rates = {}
    for ticker, m in markets.items():
        if m.get("status") != "ACTIVE":
            continue
        sym = ticker.replace("-USD", "").upper()
        if sym in TARGET_SET:
            rates[sym] = float(m.get("nextFundingRate", 0)) * 24
    return rates


async def fetch_drift_rates(client: httpx.AsyncClient) -> dict[str, float]:
    """Drift: 1h cycle. Rate is percentage/hr → convert to decimal/day."""
    resp = await client.get("https://data.api.drift.trade/stats/markets")
    resp.raise_for_status()
    rates = {}
    for m in resp.json().get("markets", []):
        if m.get("marketType") != "perp":
            continue
        sym = m.get("symbol", "").replace("-PERP", "").upper()
        if sym not in TARGET_SET:
            continue
        fr = m.get("fundingRate", {})
        rate_pct_hr = float(
            fr.get("long", fr.get("short", 0)) if isinstance(fr, dict) else (fr or 0)
        )
        rates[sym] = (rate_pct_hr / 100) * 24  # pct/hr → decimal/day
    return rates


# ═══════════════════════════════════════════════════════════════
# POSITION FETCHERS (for rotation / exit)
# ═══════════════════════════════════════════════════════════════

async def _fetch_hl_positions(client):
    addr = os.environ.get("HYPERLIQUID_WALLET_ADDRESS", "")
    if not addr:
        return []
    resp = await client.post(
        "https://api.hyperliquid.xyz/info",
        json={"type": "clearinghouseState", "user": addr},
    )
    resp.raise_for_status()
    positions = []
    for pos in resp.json().get("assetPositions", []):
        p = pos.get("position", pos)
        size = float(p.get("szi", 0))
        if size == 0:
            continue
        positions.append({
            "venue": "HL",
            "symbol": p.get("coin", "").upper(),
            "side": "LONG" if size > 0 else "SHORT",
            "size_usd": abs(float(p.get("positionValue", 0))),
        })
    return positions


async def _fetch_lighter_positions(client):
    idx = os.environ.get("LIGHTER_ACCOUNT_INDEX", "")
    if not idx:
        return []
    resp = await client.get(
        "https://mainnet.zklighter.elliot.ai/api/v1/account",
        params={"by": "index", "value": idx},
    )
    resp.raise_for_status()
    acct = resp.json()["accounts"][0]
    positions = []
    for p in acct["positions"]:
        size = float(p["position"])
        if abs(size) < 1e-12:
            continue
        positions.append({
            "venue": "Lighter",
            "symbol": p.get("symbol", "").replace("-USD", "").upper(),
            "side": "LONG" if p["sign"] == 1 else "SHORT",
            "size_usd": float(p["position_value"]),
        })
    return positions


async def _fetch_dydx_positions(client):
    addr = os.environ.get("DYDX_WALLET_ADDRESS", "")
    if not addr:
        return []
    resp = await client.get(f"https://indexer.dydx.trade/v4/addresses/{addr}")
    resp.raise_for_status()
    positions = []
    for sa in resp.json().get("subaccounts", []):
        for market, pp in sa.get("openPerpetualPositions", {}).items():
            size = float(pp.get("size", 0))
            if size == 0:
                continue
            entry = float(pp.get("entryPrice", 0))
            positions.append({
                "venue": "dYdX",
                "symbol": market.replace("-USD", "").upper(),
                "side": pp.get("side", "").upper(),
                "size_usd": abs(size) * entry,
            })
    return positions


async def _fetch_drift_positions(client):
    addr = os.environ.get("DRIFT_WALLET_ADDRESS", "")
    if not addr:
        return []
    r1 = await client.get(
        f"https://data.api.drift.trade/authority/{addr}/accounts"
    )
    r1.raise_for_status()
    accounts = r1.json().get("accounts", [])
    if not accounts:
        return []
    acct_id = accounts[0]["accountId"]
    r2 = await client.get(f"https://data.api.drift.trade/user/{acct_id}")
    r2.raise_for_status()
    positions = []
    for p in r2.json().get("positions", []):
        size = float(p.get("baseAssetAmount", p.get("size", 0)))
        if size == 0:
            continue
        mark = float(p.get("markPrice", p.get("oraclePrice", 0)))
        positions.append({
            "venue": "Drift",
            "symbol": p.get("symbol", "").replace("-PERP", "").upper(),
            "side": "LONG" if size > 0 else "SHORT",
            "size_usd": abs(size) * mark,
        })
    return positions


async def _fetch_aster_positions(client):
    key = os.environ.get("ASTER_API_KEY", "")
    secret = os.environ.get("ASTER_API_SECRET", "")
    if not key or not secret:
        return []
    import hashlib, hmac, time
    from urllib.parse import urlencode

    params = {"timestamp": int(time.time() * 1000), "recvWindow": 5000}
    qs = urlencode(params)
    sig = hmac.new(secret.encode(), qs.encode(), hashlib.sha256).hexdigest()
    params["signature"] = sig
    resp = await client.get(
        "https://fapi.asterdex.com/fapi/v4/account",
        params=params,
        headers={"X-MBX-APIKEY": key},
    )
    resp.raise_for_status()
    positions = []
    for p in resp.json().get("positions", []):
        amt = float(p.get("positionAmt", 0))
        if amt == 0:
            continue
        positions.append({
            "venue": "Aster",
            "symbol": p.get("symbol", "").replace("USDT", "").replace("USDC", "").upper(),
            "side": "LONG" if amt > 0 else "SHORT",
            "size_usd": abs(float(p.get("notional", 0))),
        })
    return positions


async def _fetch_apex_positions(client):
    import base64, hashlib, hmac, time

    key = os.environ.get("APEX_OMNI_API_KEY", "")
    secret = os.environ.get("APEX_OMNI_API_SECRET", "")
    passphrase = os.environ.get("APEX_OMNI_PASSPHRASE", "")
    if not key or not secret or not passphrase:
        return []

    ts_str = str(int(round(time.time() * 1000)))
    path = "/api/v3/account"
    msg = ts_str + "GET" + path
    hmac_key = base64.standard_b64encode(secret.encode("utf-8"))
    sig = base64.standard_b64encode(
        hmac.new(hmac_key, msg.encode("utf-8"), hashlib.sha256).digest()
    ).decode()
    headers = {
        "APEX-SIGNATURE": sig,
        "APEX-API-KEY": key,
        "APEX-TIMESTAMP": ts_str,
        "APEX-PASSPHRASE": passphrase,
    }
    resp = await client.get(f"https://omni.apex.exchange{path}", headers=headers)
    resp.raise_for_status()
    data = resp.json().get("data", {})
    acct = data.get("account", data.get("accounts", {}))
    open_pos = acct.get("openPositions", {}) if isinstance(acct, dict) else {}
    positions = []
    for sym, pos in open_pos.items():
        size = float(pos.get("size", 0))
        if size == 0:
            continue
        symbol = sym.replace("USDT", "").replace("USDC", "").replace("-", "").upper()
        entry = float(pos.get("entryPrice", 0))
        positions.append({
            "venue": "Apex",
            "symbol": symbol,
            "side": pos.get("side", "").upper(),
            "size_usd": abs(size) * entry,
        })
    return positions


async def fetch_all_positions(client: httpx.AsyncClient) -> list[dict]:
    """Fetch open positions from all 6 venues in parallel."""
    results = await asyncio.gather(
        _fetch_hl_positions(client),
        _fetch_aster_positions(client),
        _fetch_lighter_positions(client),
        _fetch_apex_positions(client),
        _fetch_dydx_positions(client),
        _fetch_drift_positions(client),
        return_exceptions=True,
    )
    positions = []
    for r in results:
        if isinstance(r, list):
            positions.extend(r)
    return positions


# ═══════════════════════════════════════════════════════════════
# OPPORTUNITY CALCULATION
# ═══════════════════════════════════════════════════════════════

def calculate_opportunities(
    rates: dict[str, dict[str, float]],
    symbols: list[str] | None = None,
) -> list[dict]:
    """
    For each symbol, find the best short venue (highest rate) and
    best long venue (lowest rate). Calculate DPY, NDPY, breakeven, APY, MPY.

    Funding sign convention:
      positive rate → longs pay shorts  (good for shorting)
      negative rate → shorts pay longs  (good for longing)

    Hedge yield = short_venue_rate - long_venue_rate
    """
    target = symbols or TARGET_SYMBOLS
    opportunities = []

    for symbol in target:
        # Collect all venue rates for this symbol
        venue_rates = {}
        for venue, rd in rates.items():
            if symbol in rd:
                venue_rates[venue] = rd[symbol]

        if len(venue_rates) < 2:
            continue

        # Best short = highest rate (maximize what shorts receive)
        best_short_venue = max(venue_rates, key=venue_rates.get)
        best_short_rate = venue_rates[best_short_venue]

        # Best long = lowest rate (maximize what longs receive)
        best_long_venue = min(venue_rates, key=venue_rates.get)
        best_long_rate = venue_rates[best_long_venue]

        if best_short_venue == best_long_venue:
            continue

        dpy = best_short_rate - best_long_rate
        if dpy <= 0:
            continue

        # Fees (all taker): entry = open both legs, exit = close both legs
        short_taker = VENUES[best_short_venue]["taker_fee"]
        long_taker = VENUES[best_long_venue]["taker_fee"]
        entry_fee = short_taker + long_taker
        exit_fee = short_taker + long_taker
        total_fee = entry_fee + exit_fee

        # Net daily yield = DPY minus round-trip taker fees
        ndpy = dpy - total_fee

        # Breakeven hours = time to recoup round-trip fees from daily yield
        breakeven_hours = 24 * total_fee / dpy if dpy > 0 else float("inf")

        opportunities.append({
            "symbol": symbol,
            "short_venue": best_short_venue,
            "long_venue": best_long_venue,
            "short_rate": best_short_rate,
            "long_rate": best_long_rate,
            "dpy": dpy,
            "ndpy": ndpy,
            "entry_fee": entry_fee,
            "exit_fee": exit_fee,
            "total_fee": total_fee,
            "breakeven_hours": breakeven_hours,
            "apy": dpy * 365,
            "mpy": dpy * 30,
            "all_rates": venue_rates,
        })

    opportunities.sort(key=lambda x: x["dpy"], reverse=True)
    return opportunities


# ═══════════════════════════════════════════════════════════════
# DISPLAY HELPERS
# ═══════════════════════════════════════════════════════════════

G = "\033[92m"   # green
R = "\033[91m"   # red
Y = "\033[93m"   # yellow
D = "\033[2m"    # dim
X = "\033[0m"    # reset


def _color_rate(val):
    """Color a rate value: green if positive, red if negative."""
    if val > 0:
        return f"{G}{val * 100:>+9.4f}%{X}"
    elif val < 0:
        return f"{R}{val * 100:>+9.4f}%{X}"
    return f"{val * 100:>+9.4f}%"


def display_rate_matrix(rates: dict[str, dict[str, float]]):
    """Print rate matrix: rows = symbols, cols = venues, values = DPY."""
    venue_names = list(rates.keys())
    all_symbols = sorted(
        set(s for vr in rates.values() for s in vr if s in TARGET_SET),
        key=lambda s: TARGET_SYMBOLS.index(s) if s in TARGET_SYMBOLS else 999,
    )

    # Header
    print(f"\n  DAILY FUNDING RATES (positive = shorts receive, negative = longs receive)")
    print(f"\n  {'SYMBOL':<8}", end="")
    for v in venue_names:
        print(f" {v:>12}", end="")
    print(f" {'BEST SHORT':>14} {'BEST LONG':>14} {'SPREAD':>10}")
    print("  " + "-" * (8 + 13 * len(venue_names) + 40))

    for symbol in all_symbols:
        print(f"  {symbol:<8}", end="")
        sym_rates = {}
        for v in venue_names:
            rate = rates[v].get(symbol)
            if rate is not None:
                sym_rates[v] = rate
                print(f" {_color_rate(rate)}", end="")
            else:
                print(f" {'—':>12}", end="")

        if len(sym_rates) >= 2:
            best_s = max(sym_rates, key=sym_rates.get)
            best_l = min(sym_rates, key=sym_rates.get)
            spread = sym_rates[best_s] - sym_rates[best_l]
            print(f" {G}{best_s:>14}{X} {R}{best_l:>14}{X} {_color_rate(spread)}", end="")
        print()


def display_opportunities(opps: list[dict], mode: str = "scan"):
    """Print ranked opportunities table."""
    if not opps:
        print(f"\n  {Y}No opportunities found matching criteria.{X}")
        return

    print(
        f"\n  {'#':>3} {'SYMBOL':<6} {'SHORT@VENUE':>16} {'LONG@VENUE':>16} "
        f"{'DPY':>9} {'NDPY':>9} {'FEES':>8} {'BE(h)':>7} "
        f"{'APY':>8} {'MPY':>8}"
    )
    print("  " + "-" * 97)

    for i, o in enumerate(opps, 1):
        short_str = f"{o['short_venue']}({o['short_rate'] * 100:+.3f}%)"
        long_str = f"{o['long_venue']}({o['long_rate'] * 100:+.3f}%)"
        be_str = f"{o['breakeven_hours']:.1f}" if o["breakeven_hours"] < 9999 else "inf"

        # Color NDPY
        ndpy_c = G if o["ndpy"] >= ENTRY_NDPY else (Y if o["ndpy"] > 0 else R)

        print(
            f"  {i:>3} {o['symbol']:<6} "
            f"{short_str:>16} {long_str:>16} "
            f"{o['dpy'] * 100:>8.4f}% "
            f"{ndpy_c}{o['ndpy'] * 100:>8.4f}%{X} "
            f"{o['total_fee'] * 100:>7.3f}% "
            f"{be_str:>7} "
            f"{o['apy'] * 100:>7.1f}% "
            f"{o['mpy'] * 100:>7.2f}%"
        )


# ═══════════════════════════════════════════════════════════════
# MODE HANDLERS
# ═══════════════════════════════════════════════════════════════

async def mode_scan(rates):
    print("=" * 100)
    print(f"  HEDGE SCANNER — Full Rate Matrix + Opportunities")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 100)

    display_rate_matrix(rates)

    opps = calculate_opportunities(rates)
    print(f"\n  TOP HEDGE OPPORTUNITIES (ranked by DPY)")
    display_opportunities(opps, "scan")

    # Summary
    qualifying = [o for o in opps if o["ndpy"] >= ENTRY_NDPY and o["breakeven_hours"] <= ENTRY_BREAKEVEN]
    print(
        f"\n  {len(qualifying)}/{len(opps)} opportunities pass entry thresholds "
        f"(NDPY >= {ENTRY_NDPY * 100:.4f}%, BE <= {ENTRY_BREAKEVEN:.0f}h)"
    )
    print(
        f"  Thresholds: Entry NDPY >= {ENTRY_NDPY * 100:.4f}%  |  "
        f"Entry BE <= {ENTRY_BREAKEVEN:.0f}h  |  "
        f"Rotation BE <= {ROTATION_BREAKEVEN:.0f}h  |  "
        f"Exit NDPY >= {EXIT_NDPY * 100:.4f}%"
    )


async def mode_entry(rates):
    print("=" * 100)
    print(f"  HEDGE ENTRY SCANNER — New opportunities meeting entry thresholds")
    print(f"  NDPY >= {ENTRY_NDPY * 100:.4f}%  |  Breakeven <= {ENTRY_BREAKEVEN:.0f}h")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 100)

    display_rate_matrix(rates)

    opps = calculate_opportunities(rates)
    qualified = [
        o for o in opps
        if o["ndpy"] >= ENTRY_NDPY and o["breakeven_hours"] <= ENTRY_BREAKEVEN
    ]

    if qualified:
        print(f"\n  {G}QUALIFYING OPPORTUNITIES ({len(qualified)}){X}")
        display_opportunities(qualified, "entry")
    else:
        print(f"\n  {Y}No opportunities currently meet entry thresholds.{X}")

    # Show near-misses
    near_misses = [o for o in opps if o not in qualified][:5]
    if near_misses:
        print(f"\n  Near misses (top {len(near_misses)}):")
        for i, o in enumerate(near_misses, 1):
            ndpy_ok = f"{G}pass{X}" if o["ndpy"] >= ENTRY_NDPY else f"{R}fail{X}"
            be_ok = f"{G}pass{X}" if o["breakeven_hours"] <= ENTRY_BREAKEVEN else f"{R}fail{X}"
            print(
                f"    {i}. {o['symbol']:<6} "
                f"SHORT@{o['short_venue']} LONG@{o['long_venue']}  "
                f"NDPY={o['ndpy'] * 100:.4f}% [{ndpy_ok}]  "
                f"BE={o['breakeven_hours']:.1f}h [{be_ok}]"
            )

    print(f"\n  {len(qualified)} of {len(opps)} opportunities qualify for entry")


async def mode_rotation(rates, client):
    print("=" * 100)
    print(f"  HEDGE ROTATION SCANNER — Better alternatives for current positions")
    print(f"  Rotation must improve NDPY  |  Rotation BE <= {ROTATION_BREAKEVEN:.0f}h")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 100)

    print("\n  Fetching positions from all venues...", flush=True)
    positions = await fetch_all_positions(client)
    if not positions:
        print(f"\n  {Y}No open positions found across any venue.{X}")
        return

    # Display current positions
    print(f"\n  Current Positions ({len(positions)}):")
    print(f"  {'VENUE':<10} {'SYMBOL':<6} {'SIDE':<6} {'SIZE (USD)':>12}")
    print("  " + "-" * 36)
    for p in positions:
        print(f"  {p['venue']:<10} {p['symbol']:<6} {p['side']:<6} ${p['size_usd']:>11,.2f}")

    # Find hedge pairs and check rotations
    by_symbol = defaultdict(list)
    for p in positions:
        by_symbol[p["symbol"]].append(p)

    rotations = []
    hedges_checked = 0

    for symbol, pos_list in by_symbol.items():
        shorts = [p for p in pos_list if p["side"] == "SHORT"]
        longs = [p for p in pos_list if p["side"] == "LONG"]

        for s in shorts:
            for l in longs:
                if s["venue"] == l["venue"]:
                    continue
                hedges_checked += 1

                # Current hedge metrics
                cur_short_rate = rates.get(s["venue"], {}).get(symbol, 0)
                cur_long_rate = rates.get(l["venue"], {}).get(symbol, 0)
                cur_dpy = cur_short_rate - cur_long_rate
                s_taker = VENUES[s["venue"]]["taker_fee"]
                l_taker = VENUES[l["venue"]]["taker_fee"]
                cur_total_fee = 2 * (s_taker + l_taker)
                cur_ndpy = cur_dpy - cur_total_fee

                # Check rotating the SHORT leg to a better venue
                for venue in rates:
                    if venue == s["venue"] or venue == l["venue"]:
                        continue
                    new_rate = rates[venue].get(symbol)
                    if new_rate is None or new_rate <= cur_short_rate:
                        continue
                    new_dpy = new_rate - cur_long_rate
                    new_taker = VENUES[venue]["taker_fee"]
                    rot_fee = s_taker + new_taker  # close old + open new
                    dpy_improvement = new_dpy - cur_dpy
                    rot_be = 24 * rot_fee / dpy_improvement if dpy_improvement > 0 else float("inf")
                    new_total_fee = 2 * (new_taker + l_taker)
                    new_ndpy = new_dpy - new_total_fee
                    if new_ndpy > cur_ndpy and rot_be <= ROTATION_BREAKEVEN:
                        rotations.append({
                            "symbol": symbol,
                            "leg": "SHORT",
                            "from_venue": s["venue"],
                            "to_venue": venue,
                            "cur_dpy": cur_dpy,
                            "new_dpy": new_dpy,
                            "cur_ndpy": cur_ndpy,
                            "new_ndpy": new_ndpy,
                            "rot_fee": rot_fee,
                            "rot_be": rot_be,
                            "improvement": new_ndpy - cur_ndpy,
                        })

                # Check rotating the LONG leg to a better venue
                for venue in rates:
                    if venue == l["venue"] or venue == s["venue"]:
                        continue
                    new_rate = rates[venue].get(symbol)
                    if new_rate is None or new_rate >= cur_long_rate:
                        continue
                    new_dpy = cur_short_rate - new_rate
                    new_taker = VENUES[venue]["taker_fee"]
                    rot_fee = l_taker + new_taker
                    dpy_improvement = new_dpy - cur_dpy
                    rot_be = 24 * rot_fee / dpy_improvement if dpy_improvement > 0 else float("inf")
                    new_total_fee = 2 * (s_taker + new_taker)
                    new_ndpy = new_dpy - new_total_fee
                    if new_ndpy > cur_ndpy and rot_be <= ROTATION_BREAKEVEN:
                        rotations.append({
                            "symbol": symbol,
                            "leg": "LONG",
                            "from_venue": l["venue"],
                            "to_venue": venue,
                            "cur_dpy": cur_dpy,
                            "new_dpy": new_dpy,
                            "cur_ndpy": cur_ndpy,
                            "new_ndpy": new_ndpy,
                            "rot_fee": rot_fee,
                            "rot_be": rot_be,
                            "improvement": new_ndpy - cur_ndpy,
                        })

    rotations.sort(key=lambda x: x["improvement"], reverse=True)

    if rotations:
        print(f"\n  {G}ROTATION OPPORTUNITIES ({len(rotations)}){X}")
        print(
            f"  {'SYMBOL':<6} {'LEG':<6} {'FROM':<10} {'TO':<10} "
            f"{'CUR DPY':>9} {'NEW DPY':>9} {'IMPROVE':>9} {'ROT FEE':>8} {'BE(h)':>7}"
        )
        print("  " + "-" * 82)
        for r in rotations:
            print(
                f"  {r['symbol']:<6} {r['leg']:<6} {r['from_venue']:<10} {r['to_venue']:<10} "
                f"{r['cur_dpy'] * 100:>8.4f}% {r['new_dpy'] * 100:>8.4f}% "
                f"{G}+{r['improvement'] * 100:>7.4f}%{X} "
                f"{r['rot_fee'] * 100:>7.3f}% "
                f"{r['rot_be']:.1f}"
            )
    else:
        print(f"\n  {D}No rotation opportunities found — all positions at optimal venues.{X}")

    print(f"\n  Checked {hedges_checked} hedge pair(s), found {len(rotations)} rotation(s)")


async def mode_exit(rates, client):
    print("=" * 100)
    print(f"  HEDGE EXIT SCANNER — Positions below exit threshold")
    print(f"  Exit when DPY < {EXIT_NDPY * 100:.4f}%")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 100)

    print("\n  Fetching positions from all venues...", flush=True)
    positions = await fetch_all_positions(client)
    if not positions:
        print(f"\n  {Y}No open positions found across any venue.{X}")
        return

    # Identify hedge pairs
    by_symbol = defaultdict(list)
    for p in positions:
        by_symbol[p["symbol"]].append(p)

    hedges = []
    unmatched = []

    for symbol, pos_list in by_symbol.items():
        shorts = [p for p in pos_list if p["side"] == "SHORT"]
        longs = [p for p in pos_list if p["side"] == "LONG"]

        matched_s, matched_l = set(), set()
        for si, s in enumerate(shorts):
            for li, l in enumerate(longs):
                if s["venue"] == l["venue"] or li in matched_l:
                    continue
                short_rate = rates.get(s["venue"], {}).get(symbol, 0)
                long_rate = rates.get(l["venue"], {}).get(symbol, 0)
                dpy = short_rate - long_rate
                s_taker = VENUES.get(s["venue"], {}).get("taker_fee", 0)
                l_taker = VENUES.get(l["venue"], {}).get("taker_fee", 0)
                total_fee = 2 * (s_taker + l_taker)
                ndpy = dpy - total_fee

                hedges.append({
                    "symbol": symbol,
                    "short_venue": s["venue"],
                    "long_venue": l["venue"],
                    "short_rate": short_rate,
                    "long_rate": long_rate,
                    "dpy": dpy,
                    "ndpy": ndpy,
                    "total_fee": total_fee,
                    "size_usd": min(s["size_usd"], l["size_usd"]),
                    "should_exit": dpy < EXIT_NDPY,
                })
                matched_s.add(si)
                matched_l.add(li)
                break

        # Track unmatched positions
        for si, s in enumerate(shorts):
            if si not in matched_s:
                unmatched.append(s)
        for li, l in enumerate(longs):
            if li not in matched_l:
                unmatched.append(l)

    if hedges:
        print(
            f"\n  {'SYMBOL':<6} {'SHORT@':<10} {'LONG@':<10} "
            f"{'DPY':>9} {'NDPY':>9} {'SIZE':>12} {'STATUS':>10}"
        )
        print("  " + "-" * 74)
        for h in hedges:
            if h["should_exit"]:
                status = f"{R}EXIT{X}"
            else:
                status = f"{G}HOLD{X}"
            print(
                f"  {h['symbol']:<6} {h['short_venue']:<10} {h['long_venue']:<10} "
                f"{h['dpy'] * 100:>8.4f}% {h['ndpy'] * 100:>8.4f}% "
                f"${h['size_usd']:>11,.2f} {status:>18}"
            )

        exits = [h for h in hedges if h["should_exit"]]
        holds = [h for h in hedges if not h["should_exit"]]
        print(
            f"\n  {R}{len(exits)} hedge(s) flagged for EXIT{X}  |  "
            f"{G}{len(holds)} hedge(s) HOLD{X}"
        )
    else:
        print(f"\n  {Y}No hedge pairs detected in current positions.{X}")

    if unmatched:
        print(f"\n  Unmatched positions (no paired leg found):")
        for p in unmatched:
            print(f"    {Y}{p['venue']:<10} {p['symbol']:<6} {p['side']:<6} ${p['size_usd']:>11,.2f}{X}")


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

async def scan_json(symbols: list[str] | None = None) -> dict:
    """Return opportunity data as a dict (for API / web UI consumption)."""
    target = symbols or TARGET_SYMBOLS

    async with httpx.AsyncClient(timeout=25.0) as client:
        fetchers = [
            ("HL", fetch_hl_rates),
            ("Aster", fetch_aster_rates),
            ("Lighter", fetch_lighter_rates),
            ("Apex", fetch_apex_rates),
            ("dYdX", fetch_dydx_rates),
            ("Drift", fetch_drift_rates),
        ]
        results = await asyncio.gather(
            *[fn(client) for _, fn in fetchers],
            return_exceptions=True,
        )

        rates: dict[str, dict[str, float]] = {}
        venue_status: dict[str, int] = {}
        for (name, _), result in zip(fetchers, results):
            if isinstance(result, Exception):
                rates[name] = {}
                venue_status[name] = 0
            else:
                rates[name] = result
                venue_status[name] = len(result)

    opps = calculate_opportunities(rates, target)

    # Build per-symbol row data for the UI
    rows = []
    for symbol in target:
        venue_rates = {}
        for venue, rd in rates.items():
            if symbol in rd:
                venue_rates[venue] = rd[symbol]
        if len(venue_rates) < 2:
            continue
        best_short_venue = max(venue_rates, key=venue_rates.get)
        best_long_venue = min(venue_rates, key=venue_rates.get)
        if best_short_venue == best_long_venue:
            continue
        short_rate = venue_rates[best_short_venue]
        long_rate = venue_rates[best_long_venue]
        dpy = short_rate - long_rate
        if dpy <= 0:
            continue
        s_taker = VENUES[best_short_venue]["taker_fee"]
        l_taker = VENUES[best_long_venue]["taker_fee"]
        total_fee = 2 * (s_taker + l_taker)
        ndpy = dpy - total_fee
        be = 24 * total_fee / dpy if dpy > 0 else None
        rows.append({
            "symbol": symbol,
            "short_venue": best_short_venue,
            "short_rate": short_rate,
            "long_venue": best_long_venue,
            "long_rate": long_rate,
            "dpy": dpy,
            "ndpy": ndpy,
            "breakeven_hours": be,
            "apy": dpy * 365,
            "mpy": dpy * 30,
            "total_fee": total_fee,
            "all_rates": venue_rates,
        })
    rows.sort(key=lambda x: x["dpy"], reverse=True)

    return {
        "rows": rows,
        "venue_status": venue_status,
        "thresholds": {
            "entry_ndpy": ENTRY_NDPY,
            "entry_breakeven": ENTRY_BREAKEVEN,
            "rotation_breakeven": ROTATION_BREAKEVEN,
            "exit_ndpy": EXIT_NDPY,
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


async def main():
    args = sys.argv[1:]

    mode = "scan"
    symbols_filter = None

    for arg in args:
        if arg.lower() in ("scan", "entry", "rotation", "exit"):
            mode = arg.lower()
        elif arg.startswith("--symbols="):
            symbols_filter = [s.strip().upper() for s in arg.split("=", 1)[1].split(",")]

    async with httpx.AsyncClient(timeout=25.0) as client:
        # Fetch rates from all 6 venues in parallel
        print("  Fetching funding rates from 6 venues...", flush=True)

        fetchers = [
            ("HL", fetch_hl_rates),
            ("Aster", fetch_aster_rates),
            ("Lighter", fetch_lighter_rates),
            ("Apex", fetch_apex_rates),
            ("dYdX", fetch_dydx_rates),
            ("Drift", fetch_drift_rates),
        ]
        results = await asyncio.gather(
            *[fn(client) for _, fn in fetchers],
            return_exceptions=True,
        )

        rates = {}
        for (name, _), result in zip(fetchers, results):
            if isinstance(result, Exception):
                print(f"  ! {name}: {result}")
                rates[name] = {}
            else:
                rates[name] = result
                print(f"  + {name}: {len(result)} symbols")

        # Apply symbol filter
        if symbols_filter:
            filt = set(symbols_filter)
            for venue in rates:
                rates[venue] = {s: r for s, r in rates[venue].items() if s in filt}

        # Dispatch
        if mode == "scan":
            await mode_scan(rates)
        elif mode == "entry":
            await mode_entry(rates)
        elif mode == "rotation":
            await mode_rotation(rates, client)
        elif mode == "exit":
            await mode_exit(rates, client)


if __name__ == "__main__":
    asyncio.run(main())
