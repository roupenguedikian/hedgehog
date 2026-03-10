"""
Exchange Verification Suite for Hedgehog/Aegis Protocol.

3-Tier verification per exchange:
  Tier 1 — Read-only (no auth): connectivity, funding rates, orderbook, mark price
  Tier 2 — Authenticated reads: balance, positions, open orders
  Tier 3 — Write operations: place limit, verify resting, cancel, verify cancelled,
           place market (IOC), verify fill, cancel_all sweep

Run: python -m tests.exchange_verification.runner --venue hyperliquid --tier 3
"""
