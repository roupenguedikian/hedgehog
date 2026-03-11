#!/usr/bin/env python3
"""
Paradex DEX query tool — used by the /paradex Claude skill.

Usage:
    python3 connectors/paradex_query.py [funding|account|positions|orders|fills|income|all]

Public data (funding/markets) requires no auth.
Account data requires PARADEX_L2_ADDRESS and PARADEX_L2_PRIVATE_KEY in .env.
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

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
load_env(os.path.join(_ROOT, ".env"))

BASE = "https://api.prod.paradex.trade/v1"

L2_ADDRESS = os.environ.get("PARADEX_L2_ADDRESS", "")
L2_PRIVATE_KEY = os.environ.get("PARADEX_L2_PRIVATE_KEY", "")

HAS_AUTH = bool(L2_ADDRESS and L2_PRIVATE_KEY)

# ── Auth helper (uses paradex-py SDK for StarkNet signing) ────────────
_jwt_token = None

def _get_jwt() -> str:
    global _jwt_token
    if _jwt_token:
        return _jwt_token
    try:
        from paradex_py import ParadexSubkey
        paradex = ParadexSubkey(
            env="prod",
            l2_private_key=L2_PRIVATE_KEY,
            l2_address=L2_ADDRESS,
        )
        _jwt_token = paradex.account.jwt_token
        return _jwt_token
    except ImportError:
        print("  ERROR: paradex-py not installed. Run: pip install paradex-py")
        sys.exit(1)
    except Exception as e:
        print(f"  ERROR: Auth failed — {e}")
        sys.exit(1)


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {_get_jwt()}"}


def ts(ms):
    if not ms:
        return "N/A"
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ═══════════════════════════════════════════════════════════════
# PUBLIC — no auth needed
# ═══════════════════════════════════════════════════════════════

async def funding(client: httpx.AsyncClient):
    """Top 20 funding rates by volume + extreme rates."""
    print("=" * 80)
    print("  Paradex — FUNDING RATES (top 20 by 24h volume)")
    print("=" * 80)

    # Fetch markets config (for funding_period_hours) and summaries
    mkt_resp = await client.get("/markets")
    mkt_resp.raise_for_status()
    markets = mkt_resp.json().get("results", [])

    # Build period map: symbol → funding_period_hours
    period_map = {}
    for m in markets:
        sym = m.get("symbol", "")
        if m.get("asset_kind") == "PERP":
            period_map[sym] = int(m.get("funding_period_hours", 8))

    # Fetch all market summaries in parallel (one request per symbol)
    async def _fetch_summary(sym):
        try:
            resp = await client.get("/markets/summary", params={"market": sym})
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if not results:
                return None
            s = results[0]
            rate = float(s.get("funding_rate") or 0)
            mark = float(s.get("mark_price") or 0)
            underlying = float(s.get("underlying_price") or 0)
            vol = float(s.get("volume_24h") or 0)
            oi = float(s.get("open_interest") or 0)
            chg = float(s.get("price_change_rate_24h") or 0) * 100
            cycle_h = period_map.get(sym, 8)
            payments_per_year = 365 * (24 / cycle_h)
            ann = rate * payments_per_year
            oi_usd = oi * mark
            return {
                "symbol": sym, "rate": rate, "annualized": ann,
                "mark": mark, "underlying": underlying, "volume_24h": vol,
                "chg": chg, "cycle_h": cycle_h, "oi_usd": oi_usd,
            }
        except Exception:
            return None

    results = await asyncio.gather(*[_fetch_summary(sym) for sym in period_map])
    assets = [r for r in results if r is not None]

    if not assets:
        print("\n  No market data available")
        return

    top20 = sorted(assets, key=lambda x: x["volume_24h"], reverse=True)[:20]
    print(f"\n  {'SYMBOL':>18s} | {'RATE':>10s} | {'CYCLE':>5s} | {'ANNUAL':>8s} | {'MARK':>12s} | "
          f"{'OI (USD)':>14s} | {'24H VOL':>16s} | {'CHG%':>7s}")
    print("  " + "-" * 107)
    for a in top20:
        print(f"  {a['symbol']:>18s} | {a['rate']*100:>9.4f}% | {a['cycle_h']:>4d}h | {a['annualized']*100:>7.2f}% | "
              f"${a['mark']:>11,.2f} | ${a['oi_usd']:>13,.0f} | "
              f"${a['volume_24h']:>15,.0f} | {a['chg']:>+6.2f}%")

    by_rate = sorted(assets, key=lambda x: x["annualized"], reverse=True)
    print(f"\n  EXTREME FUNDING (top 5 highest + top 5 most negative)")
    print(f"  {'SYMBOL':>18s} | {'CYCLE':>5s} | {'ANNUAL':>8s} | {'MARK':>12s} | {'OI (USD)':>14s} | {'24H VOL':>16s}")
    print("  " + "-" * 89)
    print("  -- HIGHEST --")
    for a in by_rate[:5]:
        print(f"  {a['symbol']:>18s} | {a['cycle_h']:>4d}h | {a['annualized']*100:>+7.2f}% | ${a['mark']:>11,.4f} | "
              f"${a['oi_usd']:>13,.0f} | ${a['volume_24h']:>15,.0f}")
    print("  -- MOST NEGATIVE --")
    for a in by_rate[-5:]:
        print(f"  {a['symbol']:>18s} | {a['cycle_h']:>4d}h | {a['annualized']*100:>+7.2f}% | ${a['mark']:>11,.4f} | "
              f"${a['oi_usd']:>13,.0f} | ${a['volume_24h']:>15,.0f}")

    total_vol = sum(a["volume_24h"] for a in assets)
    active = len([a for a in assets if a["volume_24h"] > 0])
    avg_rate = sum(a["annualized"] for a in top20) / len(top20) if top20 else 0
    print(f"\n  Total 24h volume: ${total_vol:,.0f}")
    print(f"  Active markets: {active}/{len(assets)}")
    print(f"  Avg annualized rate (top 20): {avg_rate*100:.2f}%")


# ═══════════════════════════════════════════════════════════════
# AUTHENTICATED — requires paradex-py + L2 keys
# ═══════════════════════════════════════════════════════════════

async def account(client: httpx.AsyncClient):
    """Account balance with margin breakdown."""
    print("=" * 80)
    print(f"  Paradex — ACCOUNT ({L2_ADDRESS[:10]}...{L2_ADDRESS[-6:]})")
    print("=" * 80)

    headers = _auth_headers()
    resp = await client.get("/account", headers=headers)
    resp.raise_for_status()
    acct = resp.json()

    account_value = float(acct.get("account_value") or 0)
    free_collateral = float(acct.get("free_collateral") or 0)
    total_collateral = float(acct.get("total_collateral") or 0)
    init_margin = float(acct.get("initial_margin_requirement") or 0)
    maint_margin = float(acct.get("maintenance_margin_requirement") or 0)
    margin_cushion = float(acct.get("margin_cushion") or 0)
    margin_used = total_collateral - free_collateral
    margin_util = (margin_used / total_collateral * 100) if total_collateral > 0 else 0
    status = acct.get("status", "?")

    print(f"\n  NAV (Equity):         ${account_value:,.2f}")
    print(f"  Total Collateral:     ${total_collateral:,.2f}")
    print(f"  Free Collateral:      ${free_collateral:,.2f}")
    print(f"  Margin Used:          ${margin_used:,.2f} ({margin_util:.1f}%)")
    print(f"  Init. Margin Req:     ${init_margin:,.2f}")
    print(f"  Maint. Margin Req:    ${maint_margin:,.2f}")
    print(f"  Margin Cushion:       ${margin_cushion:,.2f}")
    print(f"  Status:               {status}")
    print(f"  Settlement:           {acct.get('settlement_asset', 'USDC')}")

    # Balances
    bal_resp = await client.get("/balance", headers=headers)
    bal_resp.raise_for_status()
    balances = bal_resp.json().get("results", [])
    if balances:
        print(f"\n  Token Balances:")
        for b in balances:
            print(f"    {b.get('token', '?'):>6s}: {float(b.get('size', 0)):>12,.2f}")


async def positions(client: httpx.AsyncClient):
    """Open positions."""
    print("=" * 80)
    print("  Paradex — POSITIONS")
    print("=" * 80)

    headers = _auth_headers()
    resp = await client.get("/positions", headers=headers)
    resp.raise_for_status()
    all_pos = resp.json().get("results", [])

    # Filter open positions (size != 0)
    open_pos = [p for p in all_pos if float(p.get("size") or 0) != 0]

    if not open_pos:
        print("\n  No open positions")
        return

    total_upnl = 0
    print(f"\n  {'MARKET':>18s} | {'SIDE':>5s} | {'SIZE':>10s} | {'NOTIONAL':>12s} | "
          f"{'ENTRY':>10s} | {'uPnL':>10s} | {'uFUND':>10s} | {'LEV':>5s} | {'LIQ':>12s}")
    print("  " + "-" * 115)
    for p in open_pos:
        size = float(p.get("size") or 0)
        side = p.get("side", "?")
        entry = float(p.get("average_entry_price") or 0)
        upnl = float(p.get("unrealized_pnl") or 0)
        ufund = float(p.get("unrealized_funding_pnl") or 0)
        lev = p.get("leverage", "?")
        liq = p.get("liquidation_price", "")
        # Estimate notional from cost_usd or entry * size
        cost_usd = float(p.get("cost_usd") or 0)
        notional = abs(cost_usd) if cost_usd else abs(size) * entry
        total_upnl += upnl
        liq_str = f"${float(liq):>11,.2f}" if liq else "N/A".rjust(12)
        print(f"  {p.get('market','?'):>18s} | {side:>5s} | {abs(size):>10.4f} | ${notional:>11,.2f} | "
              f"${entry:>9,.2f} | ${upnl:>+9,.2f} | ${ufund:>+9,.4f} | {lev:>5s}x | {liq_str}")
    print(f"  TOTAL uPnL: ${total_upnl:+,.2f}")


async def orders(client: httpx.AsyncClient):
    """Open orders."""
    print("=" * 80)
    print("  Paradex — OPEN ORDERS")
    print("=" * 80)

    headers = _auth_headers()
    resp = await client.get("/orders", headers=headers)
    resp.raise_for_status()
    ords = resp.json().get("results", [])

    if not ords:
        print("\n  No open orders")
        return

    print(f"\n  {'MARKET':>18s} | {'SIDE':>5s} | {'TYPE':>8s} | {'PRICE':>12s} | "
          f"{'SIZE':>10s} | {'FILLED':>10s} | {'STATUS':>10s} | {'TIME':>20s}")
    print("  " + "-" * 115)
    for o in ords:
        remaining = float(o.get("remaining_size") or 0)
        orig = float(o.get("size") or 0)
        filled = orig - remaining
        print(f"  {o.get('market','?'):>18s} | {o.get('side',''):>5s} | {o.get('type',''):>8s} | "
              f"${float(o.get('price',0)):>11,.4f} | {orig:>10.4f} | "
              f"{filled:>10.4f} | {o.get('status',''):>10s} | {ts(o.get('created_at', 0)):>20s}")


async def fills(client: httpx.AsyncClient):
    """Recent fills (last 20)."""
    print("=" * 80)
    print("  Paradex — RECENT FILLS (last 20)")
    print("=" * 80)

    headers = _auth_headers()
    resp = await client.get("/fills", params={"page_size": 20}, headers=headers)
    resp.raise_for_status()
    trades = resp.json().get("results", [])

    if not trades:
        print("\n  No recent fills")
        return

    total_fee = 0
    print(f"\n  {'TIME':>20s} | {'MARKET':>18s} | {'SIDE':>5s} | {'PRICE':>12s} | "
          f"{'SIZE':>10s} | {'VALUE':>12s} | {'FEE':>10s} | {'LIQ':>6s}")
    print("  " + "-" * 112)
    for t in trades:
        size = float(t.get("size") or 0)
        price = float(t.get("price") or 0)
        value = size * price
        fee = float(t.get("fee") or 0)
        total_fee += fee
        liquidity = t.get("liquidity", "-")
        print(f"  {ts(t.get('created_at',0)):>20s} | {t.get('market',''):>18s} | "
              f"{t.get('side',''):>5s} | ${price:>11,.4f} | "
              f"{size:>10.4f} | ${value:>11,.2f} | "
              f"${fee:>9,.4f} | {liquidity:>6s}")
    print(f"  Total fees: ${total_fee:,.4f}")


async def income(client: httpx.AsyncClient):
    """Funding payment history."""
    print("=" * 80)
    print("  Paradex — FUNDING PAYMENTS (last 30)")
    print("=" * 80)

    headers = _auth_headers()
    try:
        resp = await client.get("/funding/payments", params={"page_size": 30},
                                headers=headers, timeout=60.0)
        resp.raise_for_status()
        payments = resp.json().get("results", [])
    except httpx.ReadTimeout:
        print("\n  Funding payments endpoint timed out (Paradex API is slow for this)")
        return

    if not payments:
        print("\n  No funding payments found")
        return

    total = 0
    print(f"\n  {'TIME':>20s} | {'MARKET':>18s} | {'PAYMENT':>12s} | {'RATE':>12s} | {'INDEX':>14s}")
    print("  " + "-" * 85)
    for p in payments:
        amt = float(p.get("payment") or p.get("amount") or 0)
        total += amt
        rate = p.get("funding_rate", "")
        idx = p.get("funding_index", "")
        market = p.get("market", "?")
        print(f"  {ts(p.get('created_at',0)):>20s} | {market:>18s} | ${amt:>+11,.4f} | "
              f"{rate:>12s} | {idx:>14s}")
    print(f"  Net funding: ${total:+,.4f}")


# ═══════════════════════════════════════════════════════════════
# DISPATCH
# ═══════════════════════════════════════════════════════════════

PUBLIC_SECTIONS = {"funding": funding}
AUTH_SECTIONS = {
    "account": account,
    "positions": positions,
    "orders": orders,
    "fills": fills,
    "income": income,
}
ALL_SECTIONS = {**PUBLIC_SECTIONS, **AUTH_SECTIONS}


async def main():
    arg = sys.argv[1].lower().strip() if len(sys.argv) > 1 else "all"

    async with httpx.AsyncClient(base_url=BASE, timeout=20.0) as client:
        if arg == "all":
            await funding(client)
            print()
            if HAS_AUTH:
                for name, fn in AUTH_SECTIONS.items():
                    try:
                        await fn(client)
                    except httpx.HTTPStatusError as e:
                        print(f"\n  {name}: HTTP {e.response.status_code} — {e.response.text[:100]}")
                    except httpx.ReadTimeout:
                        print(f"\n  {name}: timed out")
                    print()
            else:
                print("  [Auth sections skipped — set PARADEX_L2_ADDRESS and PARADEX_L2_PRIVATE_KEY in .env]")
        elif arg in PUBLIC_SECTIONS:
            await PUBLIC_SECTIONS[arg](client)
        elif arg in AUTH_SECTIONS:
            if not HAS_AUTH:
                print(f"ERROR: {arg} requires PARADEX_L2_ADDRESS and PARADEX_L2_PRIVATE_KEY in .env")
                sys.exit(1)
            await AUTH_SECTIONS[arg](client)
        else:
            print(f"Unknown section: {arg}")
            print(f"Valid options: {', '.join(ALL_SECTIONS.keys())}, all")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
