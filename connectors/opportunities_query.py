#!/usr/bin/env python3
"""Opportunities Scanner — cross-venue hedge pair opportunities with volume filter and 1h EMA.

Fetches funding rates + 24h volume from all 10 venues, finds the best hedge pairs
(short high-rate venue + long low-rate venue per symbol), requires >$100k volume
on BOTH venues, and ranks by spread APY%.

Usage:
    python3 connectors/opportunities_query.py [--min-volume=100000] [--top=50]
"""
import asyncio
import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import httpx


# ── Load .env ────────────────────────────────────────────────────
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


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_env(PROJECT_ROOT / ".env")

MIN_VOLUME = 100_000  # $100k 24h volume filter per venue
EMA_FILE = PROJECT_ROOT / "data" / "funding_ema.json"
EMA_TAU = 3600.0  # 1-hour time constant in seconds

# ANSI colors
G = "\033[92m"
R = "\033[91m"
Y = "\033[93m"
D = "\033[2m"
B = "\033[1m"
X = "\033[0m"

# Venue quality tiers (weakest link of both legs)
VENUE_TIER = {
    "HL":       1.00,   # tier_1
    "dYdX":     1.00,   # tier_1
    "Drift":    1.00,   # tier_1
    "Lighter":  0.90,   # tier_2
    "Aster":    0.90,   # tier_2
    "Paradex":  0.90,   # tier_2
    "Apex":     0.80,   # tier_3
    "Ethereal": 0.80,   # tier_3
    "EdgeX":    0.80,   # tier_3
    "XYZ":      0.80,   # tier_3
}

# Taker fees (decimal, e.g. 0.00045 = 4.5 bps)
VENUE_FEES = {
    "HL":       0.00045,
    "Aster":    0.00035,
    "Lighter":  0.0,
    "Drift":    0.0003,
    "dYdX":     0.0005,
    "Apex":     0.00025,
    "EdgeX":    0.00038,
    "Paradex":  0.0002,
    "Ethereal": 0.0005,
    "XYZ":      0.00045,
}

# EdgeX API
EDGEX_BASE = "https://pro.edgex.exchange"


# ═══════════════════════════════════════════════════════════════
# FETCHERS — each returns {symbol: {"apy": float, "volume": float}}
# ═══════════════════════════════════════════════════════════════

async def fetch_hl(client: httpx.AsyncClient) -> dict:
    """Hyperliquid: 1h funding cycle."""
    resp = await client.post(
        "https://api.hyperliquid.xyz/info",
        json={"type": "metaAndAssetCtxs"},
    )
    resp.raise_for_status()
    data = resp.json()
    universe, ctxs = data[0]["universe"], data[1]
    result = {}
    for u, c in zip(universe, ctxs):
        symbol = u["name"].upper()
        rate = float(c.get("funding") or 0)
        volume = float(c.get("dayNtlVlm") or 0)
        apy = rate * 8760 * 100  # 1h cycle → annualized %
        result[symbol] = {"apy": apy, "volume": volume}
    return result


async def fetch_lighter(client: httpx.AsyncClient) -> dict:
    """Lighter: 8h funding cycle."""
    base = "https://mainnet.zklighter.elliot.ai"

    fr_resp = await client.get(f"{base}/api/v1/funding-rates")
    fr_resp.raise_for_status()
    rate_by_mid = {}
    for fr in fr_resp.json().get("funding_rates", []):
        rate_by_mid[fr["market_id"]] = float(fr.get("rate", 0))

    resp = await client.get(f"{base}/api/v1/orderBookDetails")
    resp.raise_for_status()
    details = resp.json().get("order_book_details", [])

    result = {}
    for d in details:
        if d.get("status") != "active":
            continue
        sym = d.get("symbol", "").replace("-USD", "").upper()
        if not sym:
            continue
        mid = d["market_id"]
        rate = rate_by_mid.get(mid, 0.0)
        volume = float(d.get("daily_quote_token_volume", 0))
        apy = rate * 1095 * 100  # 8h cycle: 8760/8 = 1095
        result[sym] = {"apy": apy, "volume": volume}
    return result


async def fetch_aster(client: httpx.AsyncClient) -> dict:
    """Aster: mixed cycles (1h/4h/8h)."""
    base = "https://fapi.asterdex.com"

    resp = await client.get(f"{base}/fapi/v1/premiumIndex")
    resp.raise_for_status()
    all_premium = resp.json()

    resp2 = await client.get(f"{base}/fapi/v1/ticker/24hr")
    resp2.raise_for_status()
    tickers = {t["symbol"]: t for t in resp2.json()}

    # Detect cycle hours per nextFundingTime group
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

    result = {}
    for p in all_premium:
        raw_sym = p.get("symbol", "")
        sym = raw_sym.replace("USDT", "").replace("USDC", "").upper()
        if not sym:
            continue
        rate = float(p.get("lastFundingRate") or 0)
        cycle_h = group_cycle.get(int(p.get("nextFundingTime", 0)), 8)
        apy = rate * (8760 / cycle_h) * 100

        t = tickers.get(raw_sym, {})
        volume = float(t.get("quoteVolume") or 0)
        result[sym] = {"apy": apy, "volume": volume}
    return result


async def fetch_apex(client: httpx.AsyncClient) -> dict:
    """Apex Omni: 8h funding cycle. Fetches per-symbol (no bulk endpoint)."""
    base = "https://omni.apex.exchange"
    symbols = [
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "SUIUSDT",
        "LINKUSDT", "ARBUSDT", "AVAXUSDT", "WIFUSDT", "NEARUSDT",
        "AAVEUSDT", "XRPUSDT", "BNBUSDT", "TONUSDT", "ADAUSDT",
        "MATICUSDT", "OPUSDT", "APTUSDT", "TRXUSDT", "LTCUSDT",
        "DOTUSDT", "SEIUSDT", "PEPEUSDT", "ONDOUSDT", "HYPEUSDT",
        "JUPUSDT", "ORDIUSDT", "TIAUSDT", "STXUSDT", "MKRUSDT",
    ]

    async def _get(sym):
        try:
            r = await client.get(f"{base}/api/v3/ticker", params={"symbol": sym})
            items = r.json().get("data", [])
            if items and isinstance(items, list) and items[0]:
                d = items[0]
                rate = float(d.get("fundingRate") or 0)
                apy = rate * (8760 / 8) * 100
                volume = float(d.get("turnover24h") or d.get("volume24h") or 0)
                clean = sym.replace("USDT", "").replace("USDC", "").upper()
                return clean, {"apy": apy, "volume": volume}
        except Exception:
            pass
        return None, None

    results = await asyncio.gather(*[_get(s) for s in symbols])
    return {sym: data for sym, data in results if sym is not None}


async def fetch_dydx(client: httpx.AsyncClient) -> dict:
    """dYdX v4: 1h funding cycle."""
    resp = await client.get("https://indexer.dydx.trade/v4/perpetualMarkets")
    resp.raise_for_status()
    markets = resp.json().get("markets", {})

    result = {}
    for ticker, m in markets.items():
        if m.get("status") != "ACTIVE":
            continue
        sym = ticker.replace("-USD", "").upper()
        rate = float(m.get("nextFundingRate", 0))
        volume = float(m.get("volume24H", 0))
        apy = rate * 8760 * 100
        result[sym] = {"apy": apy, "volume": volume}
    return result


async def fetch_drift(client: httpx.AsyncClient) -> dict:
    """Drift: 1h funding cycle. Rate is pct/hr."""
    resp = await client.get("https://data.api.drift.trade/stats/markets")
    resp.raise_for_status()

    result = {}
    for m in resp.json().get("markets", []):
        if m.get("marketType") != "perp":
            continue
        sym = m.get("symbol", "").replace("-PERP", "").upper()
        if not sym:
            continue

        fr = m.get("fundingRate", {})
        if isinstance(fr, dict):
            rate_pct_hr = float(fr.get("long", fr.get("short", 0)))
        else:
            rate_pct_hr = float(fr or 0)

        # Drift: pct/hr → annualized %
        apy = rate_pct_hr * 8760 / 100 * 100  # pct/hr * 8760 hrs = annualized pct
        volume = float(m.get("quoteVolume", m.get("baseVolume", 0)) or 0)
        result[sym] = {"apy": apy, "volume": volume}
    return result


import re as _re

# EdgeX symbol cleaning pattern
_EDGEX_SYM_RE = _re.compile(r"^(1000(?:PEPE|SATS|SHIB|BONK|FLOKI)|[A-Z0-9]+?)2?USD$")


async def fetch_edgex(client: httpx.AsyncClient) -> dict:
    """EdgeX: 4h funding cycle. Use dedicated funding endpoint + ticker for volume."""
    # Step 1: get contract list (for id→symbol mapping, skip TEMP*)
    meta_resp = await client.get(f"{EDGEX_BASE}/api/v1/public/meta/getMetaData")
    meta_resp.raise_for_status()
    contracts = meta_resp.json()["data"]["contractList"]

    real_contracts = []
    for c in contracts:
        name = c["contractName"]
        if name.startswith("TEMP"):
            continue
        real_contracts.append((c["contractId"], name))

    cname_map = {cid: cn for cid, cn in real_contracts}
    cids = [cid for cid, _ in real_contracts]

    # Step 2: fetch funding rates (reliable endpoint, not Cloudflare-blocked)
    async def _get_funding(cid):
        try:
            r = await client.get(
                f"{EDGEX_BASE}/api/v1/public/funding/getLatestFundingRate",
                params={"contractId": cid},
            )
            if r.status_code != 200 or "<!DOCTYPE" in r.text[:50]:
                return None
            items = r.json().get("data", [])
            if not items:
                return None
            return cid, float(items[0].get("fundingRate") or 0)
        except Exception:
            return None

    # Step 3: fetch tickers for volume (may be Cloudflare-blocked, best-effort)
    async def _get_volume(cid):
        try:
            r = await client.get(
                f"{EDGEX_BASE}/api/v1/public/quote/getTicker/",
                params={"contractId": cid},
            )
            if r.status_code != 200 or "<!DOCTYPE" in r.text[:50]:
                return None
            items = r.json().get("data", [])
            if not items:
                return None
            return cid, float(items[0].get("value") or 0)
        except Exception:
            return None

    # Batch funding requests (10 at a time, 0.2s delay)
    funding_map: dict[str, float] = {}
    for i in range(0, len(cids), 10):
        batch = cids[i : i + 10]
        results = await asyncio.gather(*[_get_funding(cid) for cid in batch])
        for r in results:
            if r:
                funding_map[r[0]] = r[1]
        if i + 10 < len(cids):
            await asyncio.sleep(0.2)

    # Batch volume requests (best-effort, 10 at a time)
    volume_map: dict[str, float] = {}
    ticker_blocked = False
    for i in range(0, len(cids), 10):
        if ticker_blocked:
            break
        batch = cids[i : i + 10]
        results = await asyncio.gather(*[_get_volume(cid) for cid in batch])
        got_any = False
        for r in results:
            if r:
                volume_map[r[0]] = r[1]
                got_any = True
        if not got_any and i == 0:
            ticker_blocked = True  # all 403 → skip remaining batches
        if i + 10 < len(cids):
            await asyncio.sleep(0.2)

    # Build output from funding rates + volumes
    out: dict[str, dict] = {}
    for cid, rate in funding_map.items():
        cname = cname_map.get(cid, "")
        m = _EDGEX_SYM_RE.match(cname)
        sym = m.group(1) if m else cname.replace("USD", "")
        apy = rate * (8760 / 4) * 100  # 4h cycle
        volume = volume_map.get(cid, 0.0)
        # Deduplicate: keep the entry with higher volume (v1 + v2 contracts)
        if sym not in out or volume > out[sym]["volume"]:
            out[sym] = {"apy": apy, "volume": volume}
    return out


async def fetch_paradex(client: httpx.AsyncClient) -> dict:
    """Paradex: 8h funding cycle (variable per market)."""
    base = "https://api.prod.paradex.trade/v1"

    mkt_resp = await client.get(f"{base}/markets")
    mkt_resp.raise_for_status()
    markets = mkt_resp.json().get("results", [])

    # Build PERP symbol → funding_period_hours map
    perps: dict[str, int] = {}
    for m in markets:
        if m.get("asset_kind") == "PERP":
            perps[m["symbol"]] = int(m.get("funding_period_hours", 8))

    async def _get_summary(sym, cycle_h):
        try:
            resp = await client.get(f"{base}/markets/summary", params={"market": sym})
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if not results:
                return None
            s = results[0]
            rate = float(s.get("funding_rate") or 0)
            vol = float(s.get("volume_24h") or 0)
            apy = rate * (8760 / cycle_h) * 100
            clean = sym.replace("-USD-PERP", "").upper()
            return clean, {"apy": apy, "volume": vol}
        except Exception:
            return None

    results = await asyncio.gather(*[_get_summary(sym, ch) for sym, ch in perps.items()])
    return {sym: data for r in results if r for sym, data in [r]}


async def fetch_ethereal(client: httpx.AsyncClient) -> dict:
    """Ethereal: 1h funding cycle. Volume is base-denominated, converted via oracle."""
    base = "https://api.ethereal.trade"

    # Paginated product list
    params = {"limit": 100}
    products = []
    while True:
        resp = await client.get(f"{base}/v1/product", params=params)
        resp.raise_for_status()
        body = resp.json()
        products.extend(body.get("data", []))
        if not body.get("hasNext"):
            break
        params["cursor"] = body["nextCursor"]

    # Oracle prices for USD conversion
    product_ids = [p["id"] for p in products if p.get("id")]
    prices: dict[str, float] = {}
    if product_ids:
        try:
            resp = await client.get(
                f"{base}/v1/product/market-price",
                params={"productIds": product_ids[:50]},
            )
            resp.raise_for_status()
            for mp in resp.json().get("data", []):
                prices[mp["productId"]] = float(mp.get("oraclePrice") or 0)
        except Exception:
            pass

    result = {}
    for p in products:
        ticker = p.get("displayTicker", p.get("ticker", ""))
        rate_1h = float(p.get("fundingRate1h") or 0)
        vol_base = float(p.get("volume24h") or 0)
        oracle = prices.get(p.get("id", ""), 0)
        vol_usd = vol_base * oracle if oracle > 0 else 0
        apy = rate_1h * 8760 * 100
        clean = ticker.replace("-USD", "").upper()
        if clean:
            result[clean] = {"apy": apy, "volume": vol_usd}
    return result


async def fetch_tradexyz(client: httpx.AsyncClient) -> dict:
    """trade.xyz (XYZ): 1h funding cycle. HIP-3 DEX on Hyperliquid."""
    resp = await client.post(
        "https://api.hyperliquid.xyz/info",
        json={"type": "metaAndAssetCtxs", "dex": "xyz"},
    )
    resp.raise_for_status()
    data = resp.json()
    universe, ctxs = data[0]["universe"], data[1]
    result = {}
    for u, c in zip(universe, ctxs):
        name = u["name"]
        sym = name.split(":", 1)[1] if ":" in name else name
        sym = sym.upper()
        rate = float(c.get("funding") or 0)
        volume = float(c.get("dayNtlVlm") or 0)
        apy = rate * 8760 * 100  # 1h cycle
        result[sym] = {"apy": apy, "volume": volume}
    return result



# ═══════════════════════════════════════════════════════════════
# EMA COMPUTATION
# ═══════════════════════════════════════════════════════════════

def load_ema() -> tuple[float, dict[str, float]]:
    """Load previous EMA state. Returns (last_ts, {key: ema_value})."""
    if not EMA_FILE.exists():
        return 0.0, {}
    try:
        with open(EMA_FILE) as f:
            data = json.load(f)
        return data.get("last_ts", 0.0), data.get("ema", {})
    except (json.JSONDecodeError, KeyError):
        return 0.0, {}


def save_ema(ts: float, ema: dict[str, float]):
    """Persist EMA state."""
    EMA_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(EMA_FILE, "w") as f:
        json.dump({"last_ts": ts, "ema": ema}, f)


def update_ema(
    current: dict[str, float],
    prev_ema: dict[str, float],
    dt: float,
) -> dict[str, float]:
    """Update EMA for all keys. alpha = 1 - exp(-dt / tau), tau = 1 hour."""
    if dt <= 0 or not prev_ema:
        return dict(current)

    alpha = 1.0 - math.exp(-dt / EMA_TAU)
    new_ema = {}
    for key, val in current.items():
        if key in prev_ema:
            new_ema[key] = alpha * val + (1.0 - alpha) * prev_ema[key]
        else:
            new_ema[key] = val
    # Preserve old keys not in current (will decay next update)
    for key, val in prev_ema.items():
        if key not in new_ema:
            new_ema[key] = val
    return new_ema


# ═══════════════════════════════════════════════════════════════
# HEDGE PAIR BUILDER
# ═══════════════════════════════════════════════════════════════

def build_hedge_pairs(venue_data, min_vol, ema):
    """Find all valid hedge pairs: same symbol, different venues, both >min_vol.

    For each symbol, generates ALL venue pairs (not just best), so we can
    see the full opportunity set.
    """
    # Group by symbol: {symbol: [(venue, apy, volume), ...]}
    by_symbol: dict[str, list] = defaultdict(list)
    for venue, symbols in venue_data.items():
        for symbol, info in symbols.items():
            if info["volume"] >= min_vol:
                by_symbol[symbol].append((venue, info["apy"], info["volume"]))

    pairs = []
    for symbol, entries in by_symbol.items():
        if len(entries) < 2:
            continue
        # Sort by APY descending — first is best short, last is best long
        entries.sort(key=lambda e: e[1], reverse=True)

        short_venue, short_apy, short_vol = entries[0]
        long_venue, long_apy, long_vol = entries[-1]

        if short_venue == long_venue:
            continue

        spread = short_apy - long_apy
        if spread <= 0:
            continue

        # Round-trip taker fees (one-time cost, entry + exit both legs)
        short_fee = VENUE_FEES.get(short_venue, 0)
        long_fee = VENUE_FEES.get(long_venue, 0)
        fee_round_trip = 2 * (short_fee + long_fee)
        fee_bps = fee_round_trip * 10000  # as basis points

        # Daily yield and APY from the funding spread
        daily_pct = spread / 365
        net_apy = spread  # ongoing yield; fee is one-time, not recurring

        # EMA spread
        ema_short = ema.get(f"{short_venue}:{symbol}", short_apy)
        ema_long = ema.get(f"{long_venue}:{symbol}", long_apy)
        ema_spread = ema_short - ema_long

        # Breakeven hours to recoup one-time round-trip fees
        be_hours = (fee_round_trip * 100 / daily_pct * 24) if daily_pct > 0 else float("inf")

        # Min volume of the two legs (bottleneck)
        min_leg_vol = min(short_vol, long_vol)

        # ── Composite score ──────────────────────────────────
        # 1. Base: EMA spread with log compression above 100% APY
        ema_clamped = max(ema_spread, 0)
        if ema_clamped <= 100:
            base = ema_clamped
        else:
            base = 100 * (1 + math.log(ema_clamped / 100))

        # 2. Persistence: penalize spot-vs-EMA divergence
        delta = spread - ema_spread
        persistence = math.exp(-abs(delta) / 40)

        # 3. Volume confidence: log-scaled from min leg volume
        vol_conf = min(1.0, max(0.5, 0.5 + 0.25 * math.log10(min_leg_vol / 100_000)))

        # 4. Venue quality: weakest link of both legs
        venue_q = min(
            VENUE_TIER.get(short_venue, 0.7),
            VENUE_TIER.get(long_venue, 0.7),
        )

        score = base * persistence * vol_conf * venue_q

        pairs.append({
            "symbol": symbol,
            "short_venue": short_venue,
            "short_apy": short_apy,
            "short_vol": short_vol,
            "long_venue": long_venue,
            "long_apy": long_apy,
            "long_vol": long_vol,
            "spread": spread,
            "daily_pct": daily_pct,
            "net_apy": net_apy,
            "ema_spread": ema_spread,
            "fee_bps": fee_bps,
            "be_hours": be_hours,
            "min_vol": min_leg_vol,
            "n_venues": len(entries),
            "score": score,
        })

    pairs.sort(key=lambda p: p["score"], reverse=True)
    return pairs


# ═══════════════════════════════════════════════════════════════
# DISPLAY
# ═══════════════════════════════════════════════════════════════

def _color_apy(val, width=8):
    if val > 0:
        return f"{G}{val:>+{width}.2f}%{X}"
    elif val < 0:
        return f"{R}{val:>+{width}.2f}%{X}"
    return f"{val:>+{width}.2f}%"


def _fmt_vol(vol):
    if vol >= 1_000_000_000:
        return f"${vol / 1e9:.1f}B"
    elif vol >= 1_000_000:
        return f"${vol / 1e6:.1f}M"
    else:
        return f"${vol / 1e3:.0f}k"


def display_pairs(pairs, top_n, ema_age_str):
    """Print hedge pair opportunity table."""
    print(f"\n  EMA: tau=1h, {ema_age_str}")
    print(f"  Score = log-compressed EMA × persistence × volume_conf × venue_quality")
    print(
        f"\n  {'#':>3} {'SCORE':>6} {'SYMBOL':<8} "
        f"{'SHORT@':<9} {'S.APY%':>8} {'S.VOL':>8} "
        f"{'LONG@':<9} {'L.APY%':>8} {'L.VOL':>8} "
        f"{'NET/DAY':>9} {'NET APY':>9} {'EMA':>9} {'DELTA':>7} "
        f"{'FEE':>5} {'BE(h)':>6}"
    )
    print("  " + "─" * 131)

    for i, p in enumerate(pairs[:top_n], 1):
        delta = p["spread"] - p["ema_spread"]

        if abs(delta) < 1.0:
            delta_str = f"{D}{delta:>+6.1f}%{X}"
        elif delta > 0:
            delta_str = f"{G}{delta:>+6.1f}%{X}"
        else:
            delta_str = f"{R}{delta:>+6.1f}%{X}"

        daily_str = _color_apy(p["daily_pct"], width=7)
        apy_str = _color_apy(p["net_apy"])
        be_str = f"{p['be_hours']:.1f}" if p["be_hours"] < 9999 else "inf"
        fee_str = f"{p['fee_bps']:.0f}bp"
        score_str = f"{B}{p['score']:>5.0f}{X}" if p["score"] >= 50 else f"{p['score']:>5.0f}"

        print(
            f"  {i:>3} {score_str} {p['symbol']:<8} "
            f"{p['short_venue']:<9} {_color_apy(p['short_apy'])} {_fmt_vol(p['short_vol']):>8} "
            f"{p['long_venue']:<9} {_color_apy(p['long_apy'])} {_fmt_vol(p['long_vol']):>8} "
            f"{daily_str} {apy_str} {_color_apy(p['ema_spread'])} {delta_str} "
            f"{fee_str:>5} {be_str:>6}"
        )


# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

async def main():
    min_vol = MIN_VOLUME
    top_n = 50
    for arg in sys.argv[1:]:
        if arg.startswith("--min-volume="):
            min_vol = float(arg.split("=", 1)[1])
        elif arg.startswith("--top="):
            top_n = int(arg.split("=", 1)[1])

    now = time.time()
    ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    print("=" * 130)
    print(f"  HEDGE OPPORTUNITIES — {ts_str}")
    print(f"  Volume filter: >${min_vol / 1000:.0f}k on BOTH legs  |  1h EMA  |  10 venues")
    print("=" * 130)
    print("  Fetching rates from 10 venues...", flush=True)

    venues = [
        ("HL", fetch_hl),
        ("Aster", fetch_aster),
        ("Lighter", fetch_lighter),
        ("Apex", fetch_apex),
        ("dYdX", fetch_dydx),
        ("Drift", fetch_drift),
        ("EdgeX", fetch_edgex),
        ("Paradex", fetch_paradex),
        ("Ethereal", fetch_ethereal),
        ("XYZ", fetch_tradexyz),
    ]

    async with httpx.AsyncClient(timeout=25.0) as client:
        results = await asyncio.gather(
            *[fn(client) for _, fn in venues],
            return_exceptions=True,
        )

    # Collect venue data
    venue_data: dict[str, dict] = {}
    for (name, _), result in zip(venues, results):
        if isinstance(result, Exception):
            print(f"  ! {name}: {result}")
            venue_data[name] = {}
        else:
            venue_data[name] = result
            count = sum(1 for v in result.values() if v["volume"] >= min_vol)
            print(f"  + {name}: {len(result)} symbols, {count} above ${min_vol / 1000:.0f}k vol")

    # Build current APY map for EMA (all symbols above volume filter)
    current_apy: dict[str, float] = {}
    for venue, symbols in venue_data.items():
        for symbol, info in symbols.items():
            if info["volume"] >= min_vol:
                current_apy[f"{venue}:{symbol}"] = info["apy"]

    # Compute EMA
    prev_ts, prev_ema = load_ema()
    dt = now - prev_ts if prev_ts > 0 else 0
    ema = update_ema(current_apy, prev_ema, dt)
    save_ema(now, ema)

    # Build hedge pairs
    pairs = build_hedge_pairs(venue_data, min_vol, ema)

    # Display
    if dt > 0:
        ema_age = f"dt={dt:.0f}s"
    else:
        ema_age = "seed (first run)"

    display_pairs(pairs, top_n, ema_age)

    # Summary
    print(f"\n  {len(pairs)} hedge pairs found (both legs >${min_vol / 1000:.0f}k vol)")
    if pairs:
        above_100 = sum(1 for p in pairs if p["spread"] > 100)
        above_10 = sum(1 for p in pairs if 10 < p["spread"] <= 100)
        below_10 = sum(1 for p in pairs if p["spread"] <= 10)
        print(
            f"  {G}{above_100} spreads >100%{X}  |  "
            f"{Y}{above_10} spreads 10-100%{X}  |  "
            f"{D}{below_10} spreads <10%{X}"
        )
        net_positive = sum(1 for p in pairs if p["net_apy"] > 0)
        print(f"  {net_positive}/{len(pairs)} pairs have positive net APY")

    print()


if __name__ == "__main__":
    asyncio.run(main())
