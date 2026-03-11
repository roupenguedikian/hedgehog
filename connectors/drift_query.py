#!/usr/bin/env python3
"""
Drift Protocol query tool — used by the /drift Claude skill.
Uses the Drift Data API (REST) + DLOB server. No SDK needed.

Usage: python3 scripts/drift_query.py [funding|account|positions|orders|fills|income|all] [address]
"""
import asyncio
import os
import sys
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

DATA_API = "https://data.api.drift.trade"
DLOB_API = "https://dlob.drift.trade"
DEFAULT_ADDRESS = os.getenv("DRIFT_WALLET_ADDRESS", "")

# DLOB precision constants
PRICE_PRECISION = 1e6
BASE_PRECISION = 1e9


def ts(epoch_s):
    if not epoch_s:
        return "N/A"
    return datetime.fromtimestamp(int(epoch_s), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ═══════════════════════════════════════════════════════════════
# Resolve wallet address → Drift account ID
# ═══════════════════════════════════════════════════════════════

async def resolve_account_id(client: httpx.AsyncClient, authority: str) -> str:
    """Resolve wallet (authority) address to Drift account ID."""
    resp = await client.get(f"{DATA_API}/authority/{authority}/accounts")
    resp.raise_for_status()
    data = resp.json()
    accounts = data.get("accounts", [])
    if accounts:
        return accounts[0]["accountId"]
    return ""


# ═══════════════════════════════════════════════════════════════
# FUNDING — markets overview + average rates + recent history
# ═══════════════════════════════════════════════════════════════

TOP_SYMBOLS = ["SOL-PERP", "BTC-PERP", "ETH-PERP", "SUI-PERP", "DOGE-PERP",
               "WIF-PERP", "BONK-PERP", "JTO-PERP", "JUP-PERP", "PEPE-PERP",
               "AVAX-PERP", "ARB-PERP", "LINK-PERP", "TIA-PERP", "SEI-PERP"]


async def query_funding(client: httpx.AsyncClient):
    # ── Section 1: Markets overview (top 20 by volume, rates, OI, extremes) ──
    print("=" * 80)
    print("  Drift — PERPETUAL MARKETS (prices & funding rates)")
    print("=" * 80)

    resp = await client.get(f"{DATA_API}/stats/markets")
    resp.raise_for_status()
    data = resp.json()
    all_markets = data.get("markets", [])

    perps = [m for m in all_markets if m.get("marketType") == "perp"]

    rows = []
    for m in perps:
        symbol = m.get("symbol", "?")
        oracle = float(m.get("oraclePrice", 0))
        mark = float(m.get("markPrice", 0))
        volume_24h = float(m.get("quoteVolume", m.get("baseVolume", 0)) or 0)
        oi_data = m.get("openInterest", {})
        oi_long = abs(float(oi_data.get("long", 0))) if isinstance(oi_data, dict) else float(oi_data or 0)
        oi_short = abs(float(oi_data.get("short", 0))) if isinstance(oi_data, dict) else 0
        oi_usd = (oi_long + oi_short) / 2 * oracle  # avg of long/short

        fr = m.get("fundingRate", {})
        if isinstance(fr, dict):
            funding = float(fr.get("long", fr.get("short", 0)))
        else:
            funding = float(fr or 0)
        # Drift funding is per-hour rate as a percentage (e.g. -0.001979 = -0.1979%)
        ann = funding * 8760 / 100  # convert from pct to decimal then annualize

        rows.append({
            "symbol": symbol,
            "oracle": oracle,
            "mark": mark,
            "funding_pct": funding,
            "annualized_pct": ann * 100,
            "oi_usd": oi_usd,
            "volume_24h": volume_24h,
        })

    top20 = sorted(rows, key=lambda x: x["volume_24h"], reverse=True)[:20]
    print(f"\n  {'SYMBOL':<14} {'RATE/HR':>10} {'ANNUAL':>8} {'ORACLE':>12} "
          f"{'OI (USD)':>14} {'24H VOL':>14}")
    print("  " + "-" * 80)
    for r in top20:
        print(f"  {r['symbol']:<14} {r['funding_pct']:>9.6f}% {r['annualized_pct']:>+7.2f}% "
              f"${r['oracle']:>11,.2f} ${r['oi_usd']:>13,.0f} ${r['volume_24h']:>13,.0f}")

    # Extreme funding
    by_rate = sorted(rows, key=lambda r: r["annualized_pct"], reverse=True)
    print(f"\n  EXTREME FUNDING (top 5 highest + top 5 most negative)")
    print(f"  {'SYMBOL':<14} {'ANNUAL':>8} {'ORACLE':>12} {'OI (USD)':>14}")
    print("  " + "-" * 55)
    print("  -- HIGHEST --")
    for r in by_rate[:5]:
        print(f"  {r['symbol']:<14} {r['annualized_pct']:>+7.2f}% ${r['oracle']:>11,.4f} ${r['oi_usd']:>13,.0f}")
    print("  -- MOST NEGATIVE --")
    for r in by_rate[-5:]:
        print(f"  {r['symbol']:<14} {r['annualized_pct']:>+7.2f}% ${r['oracle']:>11,.4f} ${r['oi_usd']:>13,.0f}")

    total_vol = sum(r["volume_24h"] for r in rows)
    total_oi = sum(r["oi_usd"] for r in rows)
    avg_rate = sum(r["annualized_pct"] for r in top20) / len(top20) if top20 else 0
    print(f"\n  Total 24h volume: ${total_vol:,.0f}")
    print(f"  Total open interest: ${total_oi:,.0f}")
    print(f"  Active perp markets: {len(rows)}")
    print(f"  Avg annualized rate (top 20): {avg_rate:+.2f}%")

    # ── Section 2: Average rates across timeframes + recent history ──
    print()
    print("=" * 80)
    print("  Drift — FUNDING RATES (averages + recent history)")
    print("=" * 80)

    # Average funding rates across timeframes
    try:
        resp = await client.get(f"{DATA_API}/stats/fundingRates")
        resp.raise_for_status()
        data = resp.json()
        markets = data.get("markets", [])
        if markets:
            print(f"\n  Average Funding Rates (annualized):")
            print(f"  {'SYMBOL':<14} {'24H':>10} {'7D':>10} {'30D':>10} {'1Y':>10}")
            print("  " + "-" * 60)
            for m in markets[:20]:
                sym = m.get("symbol", "?")
                rates = m.get("fundingRates", {})
                r24h = float(rates.get("24h", 0))
                r7d = float(rates.get("7d", 0))
                r30d = float(rates.get("30d", 0))
                r1y = float(rates.get("1y", 0))
                # These are per-hour pct rates, annualize: rate * 8760
                print(f"  {sym:<14} {r24h*8760:>+9.2f}% {r7d*8760:>+9.2f}% "
                      f"{r30d*8760:>+9.2f}% {r1y*8760:>+9.2f}%")
    except Exception as e:
        print(f"\n  Error fetching average rates: {e}")

    # Recent funding history per symbol
    print(f"\n  Recent Funding History (last 5 entries):")
    for symbol in TOP_SYMBOLS:
        try:
            resp = await client.get(f"{DATA_API}/market/{symbol}/fundingRates",
                                    params={"limit": 5})
            resp.raise_for_status()
            data = resp.json()
            records = data.get("records", [])
            if records:
                print(f"\n  {symbol}:")
                for e in records[:5]:
                    rate = float(e.get("fundingRate", 0))
                    ann = rate * 8760
                    oracle_twap = float(e.get("oraclePriceTwap", 0))
                    mark_twap = float(e.get("markPriceTwap", 0))
                    t = ts(e.get("ts", 0))
                    print(f"    {t}  rate={rate:+.6f}%  annualized={ann:+.2f}%  "
                          f"oracle_twap=${oracle_twap:,.2f}  mark_twap=${mark_twap:,.2f}")
        except httpx.HTTPStatusError:
            pass
        except Exception as ex:
            print(f"  {symbol}: error — {ex}")


# ═══════════════════════════════════════════════════════════════
# ACCOUNT — balance, collateral, margin, health
# ═══════════════════════════════════════════════════════════════

async def query_account(client: httpx.AsyncClient, address: str):
    print("=" * 80)
    print(f"  Drift — ACCOUNT: {address}")
    print("=" * 80)

    account_id = await resolve_account_id(client, address)
    if not account_id:
        print("\n  Account not found on Drift")
        return

    print(f"  Account ID: {account_id}")

    # Real-time account state
    try:
        resp = await client.get(f"{DATA_API}/user/{account_id}")
        resp.raise_for_status()
        data = resp.json()

        acct = data.get("account", {})
        balance = float(acct.get("balance", 0))
        collateral = float(acct.get("totalCollateral", 0))
        free_collateral = float(acct.get("freeCollateral", 0))
        health = acct.get("health", "?")
        init_margin = float(acct.get("initialMargin", 0))
        maint_margin = float(acct.get("maintenanceMargin", 0))
        leverage = acct.get("leverage", "0")
        margin_used = collateral - free_collateral
        margin_util = (margin_used / collateral * 100) if collateral > 0 else 0.0

        print(f"\n  Account Balance:      ${balance:,.2f}")
        print(f"  NAV (Equity):         ${collateral:,.2f}")
        print(f"  Margin Used:          ${margin_used:,.2f}")
        print(f"  Margin Utilization:   {margin_util:.1f}%")
        print(f"  Initial Margin:       ${init_margin:,.2f}")
        print(f"  Maint. Margin:        ${maint_margin:,.2f}")
        print(f"  Free Collateral:      ${free_collateral:,.2f}")
        print(f"  Health:               {health}%")
        print(f"  Leverage:             {leverage}x")

        # Balances (spot assets)
        balances = data.get("balances", [])
        non_zero = [b for b in balances if float(b.get("balance", 0)) != 0]
        if non_zero:
            print(f"\n  Asset Balances:")
            for b in non_zero:
                bal = float(b.get("balance", 0))
                liq = float(b.get("liquidationPrice", 0))
                print(f"    {b.get('symbol', '?'):>8s}: {bal:>12.6f}"
                      + (f"  liq=${liq:,.2f}" if liq > 0 else ""))

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            print("\n  Account not found")
        else:
            print(f"\n  Error: {e.response.status_code} — {e.response.text[:200]}")
    except Exception as ex:
        print(f"\n  Error: {ex}")

    # Historical snapshot for PnL/fees context
    try:
        resp = await client.get(f"{DATA_API}/authority/{address}/snapshots/overview",
                                params={"days": 1})
        resp.raise_for_status()
        data = resp.json()
        products = data.get("products", {})
        trade_data = products.get("trade", [])
        if trade_data:
            snapshots = trade_data[0].get("snapshots", [])
            if snapshots:
                latest = snapshots[0]
                upnl = float(latest.get("unrealizedPnl", 0))
                cum_rpnl = float(latest.get("cumulativeRealizedPnl", 0))
                cum_funding = float(latest.get("cumulativeFunding", 0))
                cum_fees = float(latest.get("cumulativeFeePaid", 0))
                cum_rebate = float(latest.get("cumulativeFeeRebate", 0))
                taker_vol = float(latest.get("cumulativeTakerVolume", 0))
                maker_vol = float(latest.get("cumulativeMakerVolume", 0))

                print(f"\n  Performance Snapshot:")
                print(f"    Unrealized PnL:     ${upnl:+,.2f}")
                print(f"    Realized PnL:       ${cum_rpnl:+,.2f}")
                print(f"    Cumulative Funding: ${cum_funding:+,.2f}")
                print(f"    Fees Paid:          ${cum_fees:,.2f}")
                print(f"    Fee Rebates:        ${cum_rebate:,.2f}")
                print(f"    Net Fees:           ${cum_fees - cum_rebate:,.2f}")
                print(f"    Taker Volume:       ${taker_vol:,.0f}")
                print(f"    Maker Volume:       ${maker_vol:,.0f}")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════
# POSITIONS — open perp positions
# ═══════════════════════════════════════════════════════════════

async def query_positions(client: httpx.AsyncClient, address: str):
    print("=" * 80)
    print(f"  Drift — POSITIONS: {address}")
    print("=" * 80)

    account_id = await resolve_account_id(client, address)
    if not account_id:
        print("\n  Account not found")
        return

    try:
        resp = await client.get(f"{DATA_API}/user/{account_id}")
        resp.raise_for_status()
        data = resp.json()

        positions = data.get("positions", [])
        if not positions:
            print("\n  No open perpetual positions")
            return

        total_upnl = 0
        print(f"\n  {'SYMBOL':<14} {'SIDE':>5} {'SIZE':>12} {'NOTIONAL':>12} "
              f"{'ENTRY':>12} {'MARK':>12} {'uPnL':>10} {'LIQ':>12} {'LEV':>6}")
        print("  " + "-" * 103)
        for p in positions:
            size = float(p.get("baseAssetAmount", p.get("size", 0)))
            if size == 0:
                continue
            side = "LONG" if size > 0 else "SHORT"
            symbol = p.get("symbol", p.get("marketName", f"mkt-{p.get('marketIndex', '?')}"))
            entry = float(p.get("entryPrice", p.get("avgEntryPrice", 0)))
            mark = float(p.get("markPrice", p.get("oraclePrice", 0)))
            upnl = float(p.get("unrealizedPnl", p.get("pnl", 0)))
            notional = abs(size) * mark
            liq = float(p.get("liquidationPrice", 0))
            leverage = p.get("leverage", None)
            if leverage is not None:
                lev_str = f"{float(leverage):.1f}x"
            else:
                lev_str = "N/A"
            total_upnl += upnl
            print(f"  {symbol:<14} {side:>5} {abs(size):>12.4f} ${notional:>11,.2f} "
                  f"${entry:>11,.4f} ${mark:>11,.4f} ${upnl:>+9,.2f} "
                  + (f"${liq:>11,.4f}" if liq > 0 else f"{'N/A':>12}")
                  + f" {lev_str:>6}")
        print(f"  TOTAL uPnL: ${total_upnl:+,.2f}")

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            print("\n  Account not found")
        else:
            print(f"\n  Error: {e.response.status_code} — {e.response.text[:200]}")
    except Exception as ex:
        print(f"\n  Error: {ex}")


# ═══════════════════════════════════════════════════════════════
# ORDERS — open orders
# ═══════════════════════════════════════════════════════════════

async def query_orders(client: httpx.AsyncClient, address: str):
    print("=" * 80)
    print(f"  Drift — OPEN ORDERS: {address}")
    print("=" * 80)

    account_id = await resolve_account_id(client, address)
    if not account_id:
        print("\n  Account not found")
        return

    try:
        resp = await client.get(f"{DATA_API}/user/{account_id}")
        resp.raise_for_status()
        data = resp.json()

        orders = data.get("orders", [])
        if not orders:
            print("\n  No open orders")
            return

        print(f"\n  {'SYMBOL':<14} {'SIDE':>5} {'TYPE':>8} {'PRICE':>12} "
              f"{'SIZE':>12} {'FILLED':>12} {'TIF':>6} {'STATUS':>8}")
        print("  " + "-" * 85)
        for o in orders:
            symbol = o.get("marketName", o.get("symbol", f"mkt-{o.get('marketIndex', '?')}"))
            direction = o.get("direction", o.get("side", "?"))
            order_type = o.get("orderType", o.get("type", "?"))
            price = float(o.get("price", 0))
            size = float(o.get("baseAssetAmount", o.get("size", 0)))
            filled = float(o.get("baseAssetAmountFilled", 0))
            tif = o.get("timeInForce", "-")
            status = o.get("status", "OPEN")
            print(f"  {symbol:<14} {direction:>5} {order_type:>8} ${price:>11,.4f} "
                  f"{size:>12.4f} {filled:>12.4f} {str(tif):>6} {str(status):>8}")

    except Exception as ex:
        print(f"\n  Error: {ex}")


# ═══════════════════════════════════════════════════════════════
# FILLS — recent trades for this user
# ═══════════════════════════════════════════════════════════════

async def query_fills(client: httpx.AsyncClient, address: str):
    print("=" * 80)
    print(f"  Drift — RECENT FILLS: {address}")
    print("=" * 80)

    account_id = await resolve_account_id(client, address)
    if not account_id:
        print("\n  Account not found")
        return

    try:
        resp = await client.get(f"{DATA_API}/user/{account_id}/trades",
                                params={"limit": 15})
        resp.raise_for_status()
        data = resp.json()
        records = data.get("records", [])

        if not records:
            print("\n  No recent fills")
            return

        total_fee = 0
        print(f"\n  {'TIME':<20} {'SYMBOL':<12} {'SIDE':<6} {'SIZE':>12} "
              f"{'QUOTE':>12} {'PRICE':>12} {'FEE':>10} {'ROLE':>6}")
        print("  " + "-" * 96)
        for rec in records:
            is_taker = rec.get("taker") == account_id
            side = rec.get("takerOrderDirection") if is_taker else rec.get("makerOrderDirection")
            fee = float(rec.get("takerFee", 0)) if is_taker else float(rec.get("makerFee", 0))
            # userFee is the actual fee the user paid (negative = rebate)
            if "userFee" in rec:
                fee = float(rec["userFee"])
            total_fee += fee
            base_filled = float(rec.get("baseAssetAmountFilled", 0))
            quote_filled = float(rec.get("quoteAssetAmountFilled", 0))
            oracle = float(rec.get("oraclePrice", 0))
            price = quote_filled / base_filled if base_filled else oracle
            symbol = rec.get("symbol", f"mkt-{rec.get('marketIndex', '?')}")
            time_str = ts(rec.get("ts", 0))
            role = "TAKER" if is_taker else "MAKER"
            print(f"  {time_str:<20} {symbol:<12} {str(side):<6} {base_filled:>12.4f} "
                  f"${quote_filled:>11,.2f} ${price:>11,.4f} ${fee:>9,.4f} {role:>6}")

        print(f"\n  Total fees: ${total_fee:,.4f}")

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            print("\n  No fills found")
        else:
            print(f"\n  Error: {e.response.status_code} — {e.response.text[:200]}")
    except Exception as ex:
        print(f"\n  Error: {ex}")


# ═══════════════════════════════════════════════════════════════
# INCOME — funding payments
# ═══════════════════════════════════════════════════════════════

async def query_income(client: httpx.AsyncClient, address: str):
    print("=" * 80)
    print(f"  Drift — FUNDING PAYMENTS: {address}")
    print("=" * 80)

    account_id = await resolve_account_id(client, address)
    if not account_id:
        print("\n  Account not found")
        return

    try:
        resp = await client.get(f"{DATA_API}/user/{account_id}/fundingPayments",
                                params={"limit": 20})
        resp.raise_for_status()
        data = resp.json()
        records = data.get("records", [])

        if not records:
            print("\n  No funding payment records")
            return

        # Get market index → symbol mapping
        markets_resp = await client.get(f"{DATA_API}/stats/markets")
        markets_resp.raise_for_status()
        market_data = markets_resp.json()
        idx_to_sym = {}
        for m in market_data.get("markets", []):
            if m.get("marketType") == "perp":
                idx_to_sym[m.get("marketIndex")] = m.get("symbol", "?")

        total_funding = 0
        print(f"\n  {'TIME':<20} {'SYMBOL':<14} {'PAYMENT':>12} {'BASE SIZE':>14}")
        print("  " + "-" * 65)
        for rec in records:
            payment = float(rec.get("fundingPayment", 0))
            total_funding += payment
            base = float(rec.get("baseAssetAmount", 0))
            market_idx = rec.get("marketIndex", "?")
            symbol = idx_to_sym.get(market_idx, f"mkt-{market_idx}")
            time_str = ts(rec.get("ts", 0))
            print(f"  {time_str:<20} {symbol:<14} ${payment:>+11,.4f} {base:>14.4f}")

        print(f"\n  Net funding (last {len(records)} entries): ${total_funding:+,.4f}")

        # Group by market
        by_market = {}
        for rec in records:
            market_idx = rec.get("marketIndex", "?")
            symbol = idx_to_sym.get(market_idx, f"mkt-{market_idx}")
            payment = float(rec.get("fundingPayment", 0))
            by_market[symbol] = by_market.get(symbol, 0) + payment

        if len(by_market) > 1:
            print(f"\n  By market:")
            for sym, total in sorted(by_market.items(), key=lambda x: x[1]):
                print(f"    {sym:<14} ${total:+,.4f}")

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            print("\n  No funding payments found")
        else:
            print(f"\n  Error: {e.response.status_code} — {e.response.text[:200]}")
    except Exception as ex:
        print(f"\n  Error: {ex}")


# ═══════════════════════════════════════════════════════════════

PUBLIC_SECTIONS = {"funding": query_funding}
ACCT_SECTIONS = {
    "account": query_account,
    "positions": query_positions,
    "orders": query_orders,
    "fills": query_fills,
    "income": query_income,
}
ALL_NAMES = list(PUBLIC_SECTIONS) + list(ACCT_SECTIONS) + ["all"]


async def main():
    args = sys.argv[1:]

    command = "all"
    address = DEFAULT_ADDRESS

    for arg in args:
        if len(arg) > 30 and not arg.startswith("-"):
            address = arg
        elif arg.lower() in ALL_NAMES:
            command = arg.lower()

    if command in ACCT_SECTIONS and not address:
        print("Error: DRIFT_WALLET_ADDRESS not set in .env and no address provided")
        sys.exit(1)

    async with httpx.AsyncClient(timeout=20.0) as client:
        if command in PUBLIC_SECTIONS:
            await PUBLIC_SECTIONS[command](client)
        elif command in ACCT_SECTIONS:
            await ACCT_SECTIONS[command](client, address)
        elif command == "all":
            await query_funding(client)
            print()
            if address:
                await query_account(client, address)
                print()
                await query_positions(client, address)
                print()
                await query_orders(client, address)
                print()
                await query_fills(client, address)
                print()
                await query_income(client, address)
            else:
                print("  [Account sections skipped — set DRIFT_WALLET_ADDRESS in .env]")


if __name__ == "__main__":
    asyncio.run(main())
