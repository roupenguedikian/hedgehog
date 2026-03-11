#!/usr/bin/env python3
"""
Query Lighter ZK-rollup for account info, positions, funding rates, and market data.
Uses the public REST API + lighter-sdk for authenticated endpoints.

Usage:
    python3 scripts/lighter_query.py [account|positions|funding|income|orders|fills|margin|markets|all] [account_index]
"""
import asyncio
import csv
import io
import os
import sys
from datetime import datetime, timezone
from collections import defaultdict
from dotenv import load_dotenv
import httpx

load_dotenv()

BASE_URL = "https://mainnet.zklighter.elliot.ai"
_acct_idx = os.getenv("LIGHTER_ACCOUNT_INDEX", "")
if not _acct_idx:
    sys.exit("ERROR: LIGHTER_ACCOUNT_INDEX not set in .env")
DEFAULT_ACCOUNT_INDEX = int(_acct_idx)
API_KEY_PRIVATE = os.getenv("LIGHTER_API_KEY_PRIVATE_KEY", os.getenv("LIGHTER_API_KEY_PRIVATE", ""))
_api_idx = os.getenv("LIGHTER_API_KEY_INDEX", "")
if not _api_idx:
    sys.exit("ERROR: LIGHTER_API_KEY_INDEX not set in .env")
API_KEY_INDEX = int(_api_idx)

MARKET_NAMES = {
    0: "ETH", 1: "BTC", 2: "SOL", 3: "DOGE", 7: "XRP", 8: "LINK", 9: "AVAX",
    10: "NEAR", 11: "DOT", 12: "TON", 14: "POL", 16: "SUI", 24: "HYPE", 25: "BNB",
    27: "AAVE", 30: "UNI", 35: "LTC", 39: "ADA", 43: "TRX", 45: "PUMP", 58: "BCH",
    77: "XMR", 79: "SKY", 83: "ASTER", 90: "ZEC", 119: "XLM",
}


def _get_auth_token(account_index: int) -> str | None:
    """Generate auth token using lighter-sdk SignerClient."""
    if not API_KEY_PRIVATE:
        return None
    try:
        import lighter
        signer = lighter.SignerClient(
            url=BASE_URL,
            api_private_keys={API_KEY_INDEX: API_KEY_PRIVATE},
            account_index=account_index,
        )
        err = signer.check_client()
        if err:
            print(f"  Warning: signer check failed: {err}")
            return None
        auth_token, err = signer.create_auth_token_with_expiry(
            deadline=3600, api_key_index=API_KEY_INDEX,
        )
        if err:
            print(f"  Warning: auth token error: {err}")
            return None
        return auth_token
    except ImportError:
        print("  Warning: lighter-sdk not installed — authenticated endpoints unavailable")
        return None


async def _get_account(client: httpx.AsyncClient, account_index: int) -> dict:
    """Fetch account data (cached within a single run)."""
    resp = await client.get("/api/v1/account", params={"by": "index", "value": str(account_index)})
    resp.raise_for_status()
    return resp.json()["accounts"][0]


async def query_account(client: httpx.AsyncClient, account_index: int):
    """Account overview: balances, equity, margin utilization."""
    print("=" * 82)
    print(f"  LIGHTER ACCOUNT — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 82)

    acct = await _get_account(client, account_index)

    collateral = float(acct["collateral"])
    available = float(acct["available_balance"])
    total_asset = float(acct["total_asset_value"])
    margin_used = collateral - available
    util = (margin_used / collateral * 100) if collateral > 0 else 0

    # Compute uPnL from positions
    total_upnl = 0.0
    for p in acct["positions"]:
        if abs(float(p["position"])) > 1e-12:
            total_upnl += float(p["unrealized_pnl"])

    print(f"  Account Index: {account_index}    L1: {acct['l1_address']}")
    print(f"  Collateral:    ${collateral:,.2f}")
    print(f"  Available:     ${available:,.2f}")
    print(f"  NAV:           ${total_asset:,.2f}")
    upnl_color = "\033[92m" if total_upnl > 0 else "\033[91m" if total_upnl < 0 else ""
    upnl_reset = "\033[0m" if upnl_color else ""
    print(f"  uPnL:          {upnl_color}${total_upnl:+,.2f}{upnl_reset}")
    print(f"  Margin Used:   ${margin_used:,.2f} ({util:.1f}%)")
    print(f"  Maint. Margin: N/A (per-position IMF in margin view)")


async def query_margin(client: httpx.AsyncClient, account_index: int):
    """Detailed margin breakdown per position + account limits."""
    print("=" * 82)
    print(f"  MARGIN DETAILS — Account {account_index}")
    print("=" * 82)

    acct = await _get_account(client, account_index)

    collateral = float(acct["collateral"])
    available = float(acct["available_balance"])
    total_asset = float(acct["total_asset_value"])
    margin_used = collateral - available
    util = (margin_used / collateral * 100) if collateral > 0 else 0

    print(f"\n  Collateral:       ${collateral:,.2f}")
    print(f"  Available:        ${available:,.2f}")
    print(f"  Total Equity:     ${total_asset:,.2f}")
    print(f"  Margin Used:      ${margin_used:,.2f} ({util:.1f}%)")
    print(f"  Trading Mode:     {'Cross' if acct.get('account_trading_mode', 0) == 0 else 'Isolated'}")

    # Per-position margin requirements
    positions = []
    for p in acct["positions"]:
        size = float(p["position"])
        if abs(size) < 1e-12:
            continue
        value = float(p["position_value"])
        imf = float(p["initial_margin_fraction"])
        margin_req = value * imf / 100
        allocated = float(p.get("allocated_margin", 0))
        positions.append({
            "symbol": p["symbol"],
            "side": "SHORT" if p["sign"] == -1 else "LONG",
            "value": value,
            "imf": imf,
            "margin_req": margin_req,
            "allocated": allocated,
            "mode": "Isolated" if p.get("margin_mode", 0) == 1 else "Cross",
        })

    if positions:
        positions.sort(key=lambda x: x["margin_req"], reverse=True)
        total_margin_req = sum(p["margin_req"] for p in positions)

        print(f"\n  {'Symbol':<8}{'Side':<6}{'Value':>12}{'IMF':>6}{'Margin Req':>12}{'Mode':>10}")
        print("  " + "-" * 56)
        for p in positions:
            print(f"  {p['symbol']:<8}{p['side']:<6}  ${p['value']:>9,.2f}{p['imf']:>5.0f}%"
                  f"  ${p['margin_req']:>9,.2f}  {p['mode']:>8}")
        print("  " + "-" * 56)
        print(f"  {'TOTAL':<8}{'':6}{'':>12}{'':>6}  ${total_margin_req:>9,.2f}")
        buffer = available / total_margin_req * 100 if total_margin_req > 0 else 0
        print(f"\n  Buffer (available / total margin req): {buffer:.1f}%")

    # Account limits / tier info
    auth_token = _get_auth_token(account_index)
    if auth_token:
        try:
            resp = await client.get("/api/v1/accountLimits",
                                    params={"account_index": account_index},
                                    headers={"Authorization": auth_token})
            limits = resp.json()
            if limits.get("code") == 200:
                print(f"\n  Account Tier:     {limits.get('user_tier', '?')}")
                print(f"  Maker Fee:        {limits.get('current_maker_fee_tick', 0)} ticks")
                print(f"  Taker Fee:        {limits.get('current_taker_fee_tick', 0)} ticks")
                lit = limits.get("effective_lit_stakes", "0")
                if float(lit) > 0:
                    print(f"  Staked LIT:       {lit}")
        except Exception:
            pass


async def query_positions(client: httpx.AsyncClient, account_index: int):
    """Open positions with entry, mark, PnL, leverage, liquidation."""
    print("=" * 82)
    print(f"  POSITIONS — Account {account_index}")
    print("=" * 82)

    acct = await _get_account(client, account_index)

    positions = []
    for p in acct["positions"]:
        size = float(p["position"])
        if abs(size) < 1e-12:
            continue
        value = float(p["position_value"])
        imf = float(p["initial_margin_fraction"])
        mark = value / abs(size) if abs(size) > 1e-12 else 0.0
        leverage = 100.0 / imf if imf > 0 else 0.0
        positions.append({
            "symbol": p["symbol"],
            "side": "SHORT" if p["sign"] == -1 else "LONG",
            "size": size,
            "entry": float(p["avg_entry_price"]),
            "mark": mark,
            "value": value,
            "upnl": float(p["unrealized_pnl"]),
            "rpnl": float(p["realized_pnl"]),
            "liq": float(p["liquidation_price"]) if p["liquidation_price"] != "0" else None,
            "imf": imf,
            "leverage": leverage,
        })

    if not positions:
        print("\n  No open positions")
        return

    positions.sort(key=lambda x: x["value"], reverse=True)

    print(f"\n  {'Symbol':<8}{'Side':<6}{'Size':>12}{'Entry':>12}{'Mark':>12}{'Value':>12}"
          f"{'uPnL':>12}{'Liq Price':>12}{'Lev':>6}")
    print("  " + "-" * 92)

    total_value = 0
    total_upnl = 0
    for p in positions:
        liq_str = f"${p['liq']:,.4f}" if p["liq"] else "N/A"
        color = "\033[92m" if p["upnl"] > 0 else "\033[91m" if p["upnl"] < 0 else ""
        reset = "\033[0m" if color else ""
        print(f"  {p['symbol']:<8}{p['side']:<6}{p['size']:>12,.1f}{p['entry']:>12,.5f}"
              f"{p['mark']:>12,.5f}  ${p['value']:>9,.2f}  {color}${p['upnl']:>+9,.2f}{reset}"
              f"  {liq_str:>10}  {p['leverage']:>4.1f}x")
        total_value += p["value"]
        total_upnl += p["upnl"]

    print("  " + "-" * 92)
    color = "\033[92m" if total_upnl > 0 else "\033[91m" if total_upnl < 0 else ""
    reset = "\033[0m" if color else ""
    print(f"  {'TOTAL':<8}{'':6}{'':>12}{'':>12}{'':>12}  ${total_value:>9,.2f}"
          f"  {color}${total_upnl:>+9,.2f}{reset}")


async def query_orders(client: httpx.AsyncClient, account_index: int):
    """Open orders across all markets (authenticated)."""
    print("=" * 82)
    print(f"  OPEN ORDERS — Account {account_index}")
    print("=" * 82)

    auth_token = _get_auth_token(account_index)
    if not auth_token:
        print("\n  Cannot fetch orders: no auth token (check credentials)")
        return

    headers = {"Authorization": auth_token}

    # Get account to find which markets have open orders
    acct = await _get_account(client, account_index)
    markets_with_orders = []
    for p in acct["positions"]:
        if int(p.get("open_order_count", 0)) > 0 or int(p.get("pending_order_count", 0)) > 0:
            markets_with_orders.append(p["market_id"])

    total_orders = int(acct.get("total_order_count", 0))
    pending = int(acct.get("pending_order_count", 0))

    if total_orders == 0 and pending == 0 and not markets_with_orders:
        # Try market_id=255 (all markets) as fallback
        try:
            resp = await client.get("/api/v1/accountActiveOrders",
                                    params={"account_index": account_index, "market_id": 255},
                                    headers=headers)
            data = resp.json()
            orders = data.get("orders", [])
            if not orders:
                print(f"\n  No open orders (total_order_count={total_orders})")
                return
        except Exception:
            print(f"\n  No open orders (total_order_count={total_orders})")
            return

    all_orders = []

    # Query each market with orders, plus the all-markets endpoint
    query_markets = set(markets_with_orders)
    if not query_markets:
        query_markets = {255}  # all markets

    for mid in query_markets:
        try:
            resp = await client.get("/api/v1/accountActiveOrders",
                                    params={"account_index": account_index, "market_id": mid},
                                    headers=headers)
            data = resp.json()
            for o in data.get("orders", []):
                o["_market_id"] = mid
                all_orders.append(o)
        except Exception:
            continue

    if not all_orders:
        print(f"\n  No open orders")
        return

    print(f"\n  {'Symbol':<8}{'Side':<6}{'Size':>12}{'Price':>14}{'Filled':>10}{'Type':<12}{'TIF':<8}{'Status':<10}")
    print("  " + "-" * 84)
    for o in all_orders:
        mid = o.get("market_index", o.get("_market_id", 0))
        symbol = MARKET_NAMES.get(mid, f"MKT_{mid}")
        side = "SELL" if o.get("is_ask", False) else "BUY"
        size = o.get("base_amount", o.get("size", "?"))
        price = o.get("price", "?")
        otype = o.get("order_type", "?")
        tif = o.get("time_in_force", "?")
        status = o.get("status", "?")
        filled = "N/A"
        print(f"  {symbol:<8}{side:<6}{size:>12}{price:>14}  {filled:>8}  {otype:<12}{tif:<8}{status:<10}")

    print(f"\n  Total open orders: {len(all_orders)}")


async def query_fills(client: httpx.AsyncClient, account_index: int):
    """Recent trade fills via export CSV (authenticated)."""
    print("=" * 82)
    print(f"  RECENT FILLS — Account {account_index}")
    print("=" * 82)

    auth_token = _get_auth_token(account_index)
    if not auth_token:
        print("\n  Cannot fetch fills: no auth token (check credentials)")
        return

    headers = {"Authorization": auth_token}

    try:
        resp = await client.get("/api/v1/export",
                                params={"account_index": account_index, "type": "trade"},
                                headers=headers)
        data = resp.json()
        data_url = data.get("data_url")
        if not data_url:
            print("\n  No trade export available")
            return
    except Exception as e:
        print(f"\n  Error fetching export URL: {e}")
        return

    # Fetch the CSV from S3
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as dl:
            resp = await dl.get(data_url)
            resp.raise_for_status()
            text = resp.text
    except Exception as e:
        print(f"\n  Error downloading trade CSV: {e}")
        return

    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)

    if not rows:
        print("\n  No fills found")
        return

    # CSV is newest-first
    recent = rows[:30]
    total = len(rows)

    print(f"\n  Total fills on record: {total}")
    print(f"\n  {'Date':<20}{'Market':<8}{'Side':<14}{'Size':>12}{'Price':>14}{'Value':>14}{'PnL':>12}{'Fee':>8}{'Role':<8}")
    print("  " + "-" * 112)

    for r in recent:
        date = r.get("Date", "")[:19]
        market = r.get("Market", "?")
        side = r.get("Side", "?")
        size = r.get("Size", "?")
        price = r.get("Price", "?")
        value = r.get("Trade Value", "?")
        pnl = r.get("Closed PnL", "-")
        role = r.get("Role", "?")
        fee = "$0.00"

        # Color PnL
        pnl_str = pnl
        color = ""
        reset = ""
        if pnl and pnl != "-":
            try:
                pnl_val = float(pnl)
                pnl_str = f"${pnl_val:+,.2f}"
                color = "\033[92m" if pnl_val > 0 else "\033[91m" if pnl_val < 0 else ""
                reset = "\033[0m" if color else ""
            except ValueError:
                pass

        # Color side
        side_color = "\033[92m" if "Long" in side else "\033[91m" if "Short" in side else ""
        side_reset = "\033[0m" if side_color else ""

        try:
            value_str = f"${float(value):>12,.2f}"
        except (ValueError, TypeError):
            value_str = f"{value:>13}"

        print(f"  {date:<20}{market:<8}{side_color}{side:<14}{side_reset}"
              f"{size:>12}{price:>14}{value_str}  {color}{pnl_str:>10}{reset}  {fee:>6}  {role:<8}")


async def query_funding(client: httpx.AsyncClient, account_index: int):
    """Market-wide funding rates for all active perpetual markets (public).
    Lighter has 1-hour funding cycles (8760 payments/year)."""
    print("=" * 82)
    print(f"  LIGHTER — FUNDING RATES (top 20 by 24h volume)")
    print("=" * 82)

    # Fetch all active markets from orderBookDetails
    resp = await client.get("/api/v1/orderBookDetails")
    resp.raise_for_status()
    details = resp.json().get("order_book_details", [])

    active_markets = [d for d in details if d.get("status") == "active"]

    # Fetch all funding rates in one call (bulk endpoint)
    rate_map: dict[int, float] = {}
    try:
        fr_resp = await client.get("/api/v1/funding-rates")
        fr_resp.raise_for_status()
        for fr in fr_resp.json().get("funding_rates", []):
            rate_map[fr["market_id"]] = float(fr.get("rate", 0))
    except Exception:
        pass

    assets = []
    for d in active_markets:
        mid = d["market_id"]
        symbol = d.get("symbol", MARKET_NAMES.get(mid, f"MKT_{mid}"))
        last_price = float(d.get("last_trade_price", 0))
        oi = float(d.get("open_interest", 0))
        vol_24h = float(d.get("daily_quote_token_volume", 0))

        rate = rate_map.get(mid, 0.0)
        ann = rate * 8760  # 1h funding cycle
        mark = last_price  # best available proxy
        oi_usd = oi * last_price if last_price > 0 else oi

        assets.append({
            "symbol": symbol,
            "market_id": mid,
            "rate": rate,
            "annualized": ann,
            "mark": mark,
            "oi": oi,
            "oi_usd": oi_usd,
            "volume_24h": vol_24h,
        })

    # Sort by 24h volume for the top-20 table
    by_vol = sorted(assets, key=lambda a: a["volume_24h"], reverse=True)
    top20 = by_vol[:20]

    print(f"\n  {'SYMBOL':<10}{'RATE':>12}{'ANNUAL':>10}{'MARK':>14}{'OI':>16}{'24H VOL':>16}")
    print("  " + "-" * 78)
    for a in top20:
        ann_color = "\033[92m" if a["annualized"] > 0 else "\033[91m" if a["annualized"] < 0 else ""
        ann_reset = "\033[0m" if ann_color else ""
        print(f"  {a['symbol']:<10}{a['rate']*100:>11.6f}%"
              f"  {ann_color}{a['annualized']*100:>+8.2f}%{ann_reset}"
              f"  ${a['mark']:>12,.4f}"
              f"  {a['oi']:>14,.1f}  ${a['volume_24h']:>14,.0f}")

    # Extreme funding scanner (top 5 highest + top 5 most negative)
    by_rate = sorted(assets, key=lambda a: a["annualized"], reverse=True)
    print(f"\n  EXTREME FUNDING (top 5 highest + top 5 most negative)")
    print(f"  {'SYMBOL':<10}{'ANNUAL':>10}{'MARK':>14}{'OI':>16}{'24H VOL':>16}")
    print("  " + "-" * 66)
    print("  -- HIGHEST --")
    for a in by_rate[:5]:
        print(f"  {a['symbol']:<10}{a['annualized']*100:>+9.2f}%"
              f"  ${a['mark']:>12,.4f}  {a['oi']:>14,.1f}  ${a['volume_24h']:>14,.0f}")
    print("  -- MOST NEGATIVE --")
    for a in by_rate[-5:]:
        print(f"  {a['symbol']:<10}{a['annualized']*100:>+9.2f}%"
              f"  ${a['mark']:>12,.4f}  {a['oi']:>14,.1f}  ${a['volume_24h']:>14,.0f}")

    total_vol = sum(a["volume_24h"] for a in assets)
    active_count = len([a for a in assets if a["volume_24h"] > 0])
    avg_rate = sum(a["annualized"] for a in top20) / len(top20) if top20 else 0
    print(f"\n  Funding cycle: 1h (8760 payments/year)")
    print(f"  Total 24h volume: ${total_vol:,.0f}")
    print(f"  Active markets: {active_count}/{len(assets)}")
    print(f"  Avg annualized rate (top 20): {avg_rate*100:.2f}%")


async def query_income(client: httpx.AsyncClient, account_index: int):
    """Funding payment history (authenticated) — user-specific income from funding."""
    print("=" * 82)
    print(f"  FUNDING PAYMENTS — Account {account_index}")
    print("=" * 82)

    auth_token = _get_auth_token(account_index)
    if not auth_token:
        print("\n  Cannot fetch funding: no auth token (check credentials)")
        return

    # Also fetch positions to mark open ones
    acct = await _get_account(client, account_index)
    open_mids = set()
    for p in acct["positions"]:
        if abs(float(p["position"])) > 1e-12:
            open_mids.add(p["market_id"])

    # Paginate funding history
    all_fundings = []
    cursor = None
    for _ in range(5):
        params = {"account_index": account_index, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        resp = await client.get("/api/v1/positionFunding", params=params,
                                headers={"Authorization": auth_token})
        data = resp.json()
        entries = data.get("position_fundings", [])
        if not entries:
            break
        all_fundings.extend(entries)
        cursor = data.get("next_cursor")
        if not cursor:
            break

    if not all_fundings:
        print("\n  No funding payment history")
        return

    # Aggregate by market
    by_market = defaultdict(lambda: {"total": 0.0, "count": 0, "side": ""})
    for f in all_fundings:
        mid = f["market_id"]
        by_market[mid]["total"] += float(f["change"])
        by_market[mid]["count"] += 1
        if not by_market[mid]["side"]:
            by_market[mid]["side"] = f["position_side"].upper()

    oldest = min(int(f["timestamp"]) for f in all_fundings)
    newest = max(int(f["timestamp"]) for f in all_fundings)
    hours_span = max((newest - oldest) / 3600, 1)

    print(f"  Period: {datetime.fromtimestamp(oldest, tz=timezone.utc).strftime('%m-%d %H:%M')} → "
          f"{datetime.fromtimestamp(newest, tz=timezone.utc).strftime('%m-%d %H:%M')} UTC  "
          f"({len(all_fundings)} entries)")
    print(f"\n  {'Symbol':<8}{'Side':<7}{'Hrs':>4}{'Total':>12}{'$/Hour':>12}{'$/Day':>12}{'Ann.':>12}")
    print("  " + "-" * 78)

    grand_total = 0.0
    for mid in sorted(by_market.keys()):
        info = by_market[mid]
        name = MARKET_NAMES.get(mid, f"MKT_{mid}")
        total = info["total"]
        count = info["count"]
        avg_hr = total / count if count else 0
        daily = avg_hr * 24
        annual = avg_hr * 8760
        grand_total += total

        marker = " <" if mid in open_mids else ""
        color = "\033[92m" if total > 0 else "\033[91m" if total < 0 else ""
        reset = "\033[0m" if color else ""
        print(f"  {name:<8}{info['side']:<7}{count:>4}  {color}${total:>+10.4f}{reset}"
              f"  ${avg_hr:>+10.6f}  ${daily:>+9.4f}  ${annual:>+9.2f}{marker}")

    print("  " + "-" * 78)
    daily_total = grand_total / (hours_span / 24) if hours_span > 0 else 0
    color = "\033[92m" if grand_total > 0 else "\033[91m" if grand_total < 0 else ""
    reset = "\033[0m" if color else ""
    print(f"  {'NET':<8}{'':7}{'':>4}  {color}${grand_total:>+10.4f}{reset}"
          f"  {'':>12}  ${daily_total:>+9.4f}  ${daily_total * 365:>+9.2f}")
    print(f"\n  < = current open position")


async def query_markets(client: httpx.AsyncClient):
    """All active markets with prices, funding rates, open interest."""
    print("=" * 82)
    print(f"  LIGHTER — PERPETUAL MARKETS")
    print("=" * 82)

    resp = await client.get("/api/v1/orderBookDetails")
    resp.raise_for_status()
    details = resp.json().get("order_book_details", [])

    rows = []
    for d in details:
        if d.get("status") != "active":
            continue
        mid = d["market_id"]
        symbol = d.get("symbol", f"MKT_{mid}")
        last_price = float(d.get("last_trade_price", 0))
        oi = float(d.get("open_interest", 0))
        vol_24h = float(d.get("daily_quote_token_volume", 0))
        change_24h = float(d.get("daily_price_change", 0))
        rows.append({
            "symbol": symbol,
            "market_id": mid,
            "price": last_price,
            "oi": oi,
            "volume_24h": vol_24h,
            "change_24h": change_24h,
            "maker_fee": d.get("maker_fee", "0"),
            "taker_fee": d.get("taker_fee", "0"),
        })

    rows.sort(key=lambda r: r["volume_24h"], reverse=True)

    print(f"\n  {'Symbol':<10}{'ID':>4}{'Price':>14}{'24h Chg':>10}{'OI':>16}{'Vol 24h':>16}")
    print("  " + "-" * 72)
    for r in rows[:30]:
        color = "\033[92m" if r["change_24h"] > 0 else "\033[91m" if r["change_24h"] < 0 else ""
        reset = "\033[0m" if color else ""
        print(f"  {r['symbol']:<10}{r['market_id']:>4}{r['price']:>14,.4f}"
              f"  {color}{r['change_24h']:>+8.2f}%{reset}"
              f"  {r['oi']:>14,.1f}  ${r['volume_24h']:>14,.0f}")

    print(f"\n  Total active markets: {len(rows)}")


COMMANDS = ("account", "positions", "funding", "income", "orders", "fills", "margin", "markets", "all")


async def main():
    args = sys.argv[1:]

    command = "all"
    account_index = DEFAULT_ACCOUNT_INDEX

    for arg in args:
        if arg.isdigit() and len(arg) > 3:
            account_index = int(arg)
        elif arg in COMMANDS:
            command = arg

    async with httpx.AsyncClient(base_url=BASE_URL, timeout=20.0) as client:
        if command == "account":
            await query_account(client, account_index)
        elif command == "positions":
            await query_positions(client, account_index)
        elif command == "funding":
            await query_funding(client, account_index)
        elif command == "income":
            await query_income(client, account_index)
        elif command == "orders":
            await query_orders(client, account_index)
        elif command == "fills":
            await query_fills(client, account_index)
        elif command == "margin":
            await query_margin(client, account_index)
        elif command == "markets":
            await query_markets(client)
        elif command == "all":
            await query_account(client, account_index)
            print()
            await query_positions(client, account_index)
            print()
            await query_margin(client, account_index)
            print()
            await query_orders(client, account_index)
            print()
            await query_funding(client, account_index)
            print()
            await query_income(client, account_index)
            print()
            await query_fills(client, account_index)


if __name__ == "__main__":
    asyncio.run(main())
