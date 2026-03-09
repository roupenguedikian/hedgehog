#!/usr/bin/env python3
"""Generate comprehensive per-venue Grafana dashboards for HedgeHog."""
import json
import os

VENUES = [
    {
        "key": "hyperliquid", "name": "Hyperliquid",
        "chain": "Hyperliquid L1", "deposit_chain": "Arbitrum",
        "chain_type": "EVM", "cycle": "1h",
        "maker_fee": "1.5 bps", "taker_fee": "4.5 bps",
        "max_leverage": "50x", "collateral": "USDC",
        "tier": "Tier 1", "tier_limit": "35%",
        "color": "#06b6d4",
        "features": ["Zero Gas"],
    },
    {
        "key": "lighter", "name": "Lighter",
        "chain": "Lighter ZK-Rollup", "deposit_chain": "Ethereum",
        "chain_type": "EVM", "cycle": "1h",
        "maker_fee": "0 bps", "taker_fee": "0 bps",
        "max_leverage": "50x", "collateral": "USDC",
        "tier": "Tier 2", "tier_limit": "25%",
        "color": "#8b5cf6",
        "features": ["Zero Gas", "Anti-MEV (ZK)"],
    },
    {
        "key": "aster", "name": "Aster",
        "chain": "BNB Chain", "deposit_chain": "BNB Chain",
        "chain_type": "EVM", "cycle": "8h",
        "maker_fee": "2 bps", "taker_fee": "5 bps",
        "max_leverage": "200x", "collateral": "USDT",
        "tier": "Tier 2", "tier_limit": "25%",
        "color": "#f59e0b",
        "features": ["Zero Gas", "Privacy", "Anti-MEV"],
    },
    {
        "key": "drift", "name": "Drift",
        "chain": "Solana", "deposit_chain": "Solana",
        "chain_type": "Solana", "cycle": "1h",
        "maker_fee": "0 bps", "taker_fee": "3 bps",
        "max_leverage": "20x", "collateral": "USDC",
        "tier": "Tier 1", "tier_limit": "35%",
        "color": "#e879a8",
        "features": [],
    },
    {
        "key": "dydx", "name": "dYdX v4",
        "chain": "dYdX Cosmos Appchain", "deposit_chain": "Noble",
        "chain_type": "Cosmos", "cycle": "1h",
        "maker_fee": "1 bps", "taker_fee": "5 bps",
        "max_leverage": "20x", "collateral": "USDC",
        "tier": "Tier 1", "tier_limit": "35%",
        "color": "#6366f1",
        "features": [],
    },
    {
        "key": "apex", "name": "ApeX Omni",
        "chain": "Multi-ZKLink", "deposit_chain": "Arbitrum",
        "chain_type": "EVM", "cycle": "8h",
        "maker_fee": "0 bps", "taker_fee": "2.5 bps",
        "max_leverage": "100x", "collateral": "USDT",
        "tier": "Tier 3", "tier_limit": "15%",
        "color": "#ef4444",
        "features": ["Zero Gas", "Anti-MEV"],
    },
    {
        "key": "paradex", "name": "Paradex",
        "chain": "Paradex StarkNet", "deposit_chain": "Ethereum",
        "chain_type": "StarkNet", "cycle": "1h",
        "maker_fee": "0 bps", "taker_fee": "2 bps",
        "max_leverage": "50x", "collateral": "USDC",
        "tier": "Tier 2", "tier_limit": "25%",
        "color": "#a855f7",
        "features": ["Privacy", "Anti-MEV"],
    },
    {
        "key": "ethereal", "name": "Ethereal",
        "chain": "Converge", "deposit_chain": "Arbitrum",
        "chain_type": "EVM", "cycle": "8h",
        "maker_fee": "2 bps", "taker_fee": "5 bps",
        "max_leverage": "20x", "collateral": "USDe",
        "tier": "Tier 3", "tier_limit": "15%",
        "color": "#14b8a6",
        "features": ["Yield-Bearing Collateral (15-25% APY)"],
    },
    {
        "key": "injective", "name": "Injective",
        "chain": "Injective L1", "deposit_chain": "Ethereum",
        "chain_type": "Cosmos", "cycle": "1h",
        "maker_fee": "0 bps", "taker_fee": "1.8 bps",
        "max_leverage": "20x", "collateral": "USDT",
        "tier": "Tier 2", "tier_limit": "25%",
        "color": "#0ea5e9",
        "features": ["Zero Gas", "Anti-MEV (FBA)"],
    },
]

DS = {"type": "prometheus", "uid": "${DS_PROMETHEUS}"}
DS_PG = {"type": "postgres", "uid": "${DS_TIMESCALEDB}"}


def make_venue_dashboard(v):
    key = v["key"]
    name = v["name"]
    color = v["color"]
    uid = f"hedgehog-venue-{key}"

    links = [
        {"title": "Main Dashboard", "url": "/d/hedgehog-main",
         "type": "link", "icon": "dashboard", "targetBlank": False},
    ]
    for other in VENUES:
        if other["key"] != key:
            links.append({
                "title": other["name"],
                "url": f"/d/hedgehog-venue-{other['key']}",
                "type": "link", "icon": "exchange-alt", "targetBlank": False,
            })

    panels = []
    pid_counter = [0]

    def pid():
        pid_counter[0] += 1
        return pid_counter[0]

    y = [0]  # mutable cursor for y positioning

    def row(title):
        panels.append({
            "id": pid(), "type": "row", "title": title,
            "gridPos": {"h": 1, "w": 24, "x": 0, "y": y[0]},
            "collapsed": False, "panels": [],
        })
        y[0] += 1

    def next_y(h):
        cur = y[0]
        y[0] += h
        return cur

    # ═══════════════════════════════════════════════════════════════════
    # SECTION 1: STATUS & BALANCES
    # ═══════════════════════════════════════════════════════════════════
    row(f"{name} — Status & Balances")
    sy = next_y(5)

    # Status UP/DOWN
    panels.append({
        "id": pid(), "type": "stat", "title": "Status",
        "gridPos": {"h": 5, "w": 3, "x": 0, "y": sy},
        "fieldConfig": {"defaults": {
            "mappings": [{"type": "value", "options": {
                "0": {"text": "DOWN", "color": "red"},
                "1": {"text": "UP", "color": "green"},
            }}],
            "thresholds": {"mode": "absolute", "steps": [
                {"color": "red", "value": None}, {"color": "green", "value": 1},
            ]},
        }, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]},
                    "colorMode": "background", "textMode": "auto"},
        "targets": [{"expr": f'hedgehog_venue_up{{venue="{key}"}}', "refId": "A"}],
        "datasource": DS,
    })

    # Composite Score
    panels.append({
        "id": pid(), "type": "gauge", "title": "Score",
        "gridPos": {"h": 5, "w": 3, "x": 3, "y": sy},
        "fieldConfig": {"defaults": {
            "min": 0, "max": 1, "color": {"mode": "continuous-GrYlRd"},
            "thresholds": {"mode": "absolute", "steps": [
                {"color": "red", "value": None},
                {"color": "yellow", "value": 0.4},
                {"color": "green", "value": 0.7},
            ]},
        }, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]},
                    "showThresholdLabels": False, "showThresholdMarkers": True},
        "targets": [{"expr": f'hedgehog_venue_score{{venue="{key}"}}', "refId": "A"}],
        "datasource": DS,
    })

    # Available Balance
    panels.append({
        "id": pid(), "type": "stat", "title": "Available Balance",
        "gridPos": {"h": 5, "w": 3, "x": 6, "y": sy},
        "fieldConfig": {"defaults": {
            "unit": "currencyUSD",
            "color": {"mode": "fixed", "fixedColor": "#10b981"},
            "thresholds": {"mode": "absolute", "steps": [{"color": "#10b981", "value": None}]},
        }, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]},
                    "colorMode": "value", "graphMode": "area", "textMode": "auto"},
        "targets": [{"expr": f'hedgehog_venue_balance_available_usd{{venue="{key}"}}', "refId": "A"}],
        "datasource": DS,
    })

    # Total Collateral
    panels.append({
        "id": pid(), "type": "stat", "title": "Total Collateral",
        "gridPos": {"h": 5, "w": 3, "x": 9, "y": sy},
        "fieldConfig": {"defaults": {
            "unit": "currencyUSD",
            "color": {"mode": "fixed", "fixedColor": "#f59e0b"},
            "thresholds": {"mode": "absolute", "steps": [{"color": "#f59e0b", "value": None}]},
        }, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]},
                    "colorMode": "value", "graphMode": "area", "textMode": "auto"},
        "targets": [{"expr": f'hedgehog_venue_balance_total_usd{{venue="{key}"}}', "refId": "A"}],
        "datasource": DS,
    })

    # Margin Used
    panels.append({
        "id": pid(), "type": "stat", "title": "Margin Used",
        "gridPos": {"h": 5, "w": 3, "x": 12, "y": sy},
        "fieldConfig": {"defaults": {
            "unit": "currencyUSD",
            "color": {"mode": "fixed", "fixedColor": "#ef4444"},
            "thresholds": {"mode": "absolute", "steps": [{"color": "#ef4444", "value": None}]},
        }, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]},
                    "colorMode": "value", "graphMode": "area", "textMode": "auto"},
        "targets": [{"expr": f'hedgehog_venue_margin_used_usd{{venue="{key}"}}', "refId": "A"}],
        "datasource": DS,
    })

    # Venue Exposure gauge
    tier_limit = float(v["tier_limit"].rstrip("%"))
    panels.append({
        "id": pid(), "type": "gauge", "title": "Exposure (% NAV)",
        "gridPos": {"h": 5, "w": 3, "x": 15, "y": sy},
        "fieldConfig": {"defaults": {
            "unit": "percent", "min": 0, "max": tier_limit,
            "color": {"mode": "thresholds"},
            "thresholds": {"mode": "absolute", "steps": [
                {"color": "green", "value": None},
                {"color": "yellow", "value": tier_limit * 0.7},
                {"color": "red", "value": tier_limit * 0.9},
            ]},
        }, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]},
                    "showThresholdLabels": True, "showThresholdMarkers": True},
        "targets": [{"expr": f'hedgehog_venue_exposure_pct{{venue="{key}"}}', "refId": "A"}],
        "datasource": DS,
    })

    # Venue info text
    features_str = ", ".join(v["features"]) if v["features"] else "None"
    info_html = (
        f"<div style='font-size:12px;line-height:1.7;padding:4px'>"
        f"<b>Chain:</b> {v['chain']}<br>"
        f"<b>Type:</b> {v['chain_type']}<br>"
        f"<b>Deposit via:</b> {v['deposit_chain']}<br>"
        f"<b>Cycle:</b> {v['cycle']}<br>"
        f"<b>Fees:</b> {v['maker_fee']} maker / {v['taker_fee']} taker<br>"
        f"<b>Max Leverage:</b> {v['max_leverage']}<br>"
        f"<b>Collateral:</b> {v['collateral']}<br>"
        f"<b>{v['tier']}</b> (max {v['tier_limit']})<br>"
        f"<b>Features:</b> {features_str}"
        f"</div>"
    )
    panels.append({
        "id": pid(), "type": "text", "title": "Venue Info",
        "gridPos": {"h": 5, "w": 6, "x": 18, "y": sy},
        "options": {"mode": "html", "content": info_html},
    })

    # ═══════════════════════════════════════════════════════════════════
    # SECTION 2: BALANCE & CAPITAL HISTORY
    # ═══════════════════════════════════════════════════════════════════
    row(f"Capital & Fees")
    sy = next_y(7)

    # Balance over time
    panels.append({
        "id": pid(), "type": "timeseries", "title": "Balance Over Time",
        "gridPos": {"h": 7, "w": 8, "x": 0, "y": sy},
        "fieldConfig": {"defaults": {
            "unit": "currencyUSD",
            "custom": {"lineWidth": 2, "fillOpacity": 10, "gradientMode": "opacity",
                       "showPoints": "never", "spanNulls": True},
        }, "overrides": [
            {"matcher": {"id": "byName", "options": "Available"},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#10b981"}}]},
            {"matcher": {"id": "byName", "options": "Total"},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#f59e0b"}}]},
            {"matcher": {"id": "byName", "options": "Margin Used"},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#ef4444"}}]},
        ]},
        "options": {"tooltip": {"mode": "multi"}, "legend": {"displayMode": "list", "placement": "bottom"}},
        "targets": [
            {"expr": f'hedgehog_venue_balance_available_usd{{venue="{key}"}}', "legendFormat": "Available", "refId": "A"},
            {"expr": f'hedgehog_venue_balance_total_usd{{venue="{key}"}}', "legendFormat": "Total", "refId": "B"},
            {"expr": f'hedgehog_venue_margin_used_usd{{venue="{key}"}}', "legendFormat": "Margin Used", "refId": "C"},
        ],
        "datasource": DS,
    })

    # Funding collected stat
    panels.append({
        "id": pid(), "type": "stat", "title": "Funding Collected",
        "gridPos": {"h": 7, "w": 4, "x": 8, "y": sy},
        "fieldConfig": {"defaults": {
            "unit": "currencyUSD",
            "color": {"mode": "fixed", "fixedColor": "#10b981"},
            "thresholds": {"mode": "absolute", "steps": [{"color": "#10b981", "value": None}]},
        }, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]},
                    "colorMode": "value", "graphMode": "area", "textMode": "auto"},
        "targets": [{"expr": f'hedgehog_venue_funding_collected_usd{{venue="{key}"}}', "refId": "A"}],
        "datasource": DS,
    })

    # Fees & Gas over time for this venue
    panels.append({
        "id": pid(), "type": "timeseries", "title": "Fees & Gas Paid (Cumulative)",
        "gridPos": {"h": 7, "w": 8, "x": 12, "y": sy},
        "fieldConfig": {"defaults": {
            "unit": "currencyUSD",
            "custom": {"lineWidth": 2, "fillOpacity": 10, "showPoints": "never"},
        }, "overrides": [
            {"matcher": {"id": "byName", "options": "Fees"},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#ef4444"}}]},
            {"matcher": {"id": "byName", "options": "Gas"},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "#f59e0b"}}]},
        ]},
        "options": {"tooltip": {"mode": "multi"}, "legend": {"displayMode": "list", "placement": "bottom"}},
        "targets": [
            {"expr": f'hedgehog_venue_fees_paid_usd_total{{venue="{key}"}}', "legendFormat": "Fees", "refId": "A"},
            {"expr": f'hedgehog_venue_gas_paid_usd_total{{venue="{key}"}}', "legendFormat": "Gas", "refId": "B"},
        ],
        "datasource": DS,
    })

    # Notional exposure and position count
    panels.append({
        "id": pid(), "type": "stat", "title": "Notional Exposure",
        "gridPos": {"h": 7, "w": 2, "x": 20, "y": sy},
        "fieldConfig": {"defaults": {
            "unit": "currencyUSD",
            "color": {"mode": "fixed", "fixedColor": color},
            "thresholds": {"mode": "absolute", "steps": [{"color": color, "value": None}]},
        }, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]},
                    "colorMode": "value", "graphMode": "none", "textMode": "auto"},
        "targets": [{"expr": f'hedgehog_venue_notional_usd{{venue="{key}"}}', "refId": "A"}],
        "datasource": DS,
    })

    panels.append({
        "id": pid(), "type": "stat", "title": "Positions",
        "gridPos": {"h": 7, "w": 2, "x": 22, "y": sy},
        "fieldConfig": {"defaults": {
            "color": {"mode": "fixed", "fixedColor": "#8b5cf6"},
            "thresholds": {"mode": "absolute", "steps": [{"color": "#8b5cf6", "value": None}]},
        }, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]},
                    "colorMode": "value", "graphMode": "none", "textMode": "auto"},
        "targets": [{"expr": f'hedgehog_venue_position_count{{venue="{key}"}}', "refId": "A"}],
        "datasource": DS,
    })

    # ═══════════════════════════════════════════════════════════════════
    # SECTION 3: FUNDING RATES
    # ═══════════════════════════════════════════════════════════════════
    row("Funding Rates")
    sy = next_y(9)

    # Funding rate timeseries all symbols
    panels.append({
        "id": pid(), "type": "timeseries", "title": "Funding Rates Over Time (Annualized)",
        "gridPos": {"h": 9, "w": 14, "x": 0, "y": sy},
        "fieldConfig": {"defaults": {
            "unit": "percentunit",
            "color": {"mode": "palette-classic"},
            "custom": {"lineWidth": 2, "fillOpacity": 8, "gradientMode": "opacity",
                       "showPoints": "never", "spanNulls": True},
        }, "overrides": []},
        "options": {"tooltip": {"mode": "multi", "sort": "desc"},
                    "legend": {"displayMode": "table", "placement": "right",
                               "calcs": ["lastNotNull", "mean", "min", "max"]}},
        "targets": [{"expr": f'hedgehog_funding_rate_annualized{{venue="{key}"}}',
                     "legendFormat": "{{symbol}}", "refId": "A"}],
        "datasource": DS,
    })

    # Current rates table
    panels.append({
        "id": pid(), "type": "table", "title": "Current Rates",
        "gridPos": {"h": 9, "w": 5, "x": 14, "y": sy},
        "fieldConfig": {"defaults": {}, "overrides": [
            {"matcher": {"id": "byName", "options": "Value"},
             "properties": [
                 {"id": "unit", "value": "percentunit"},
                 {"id": "displayName", "value": "Ann. Rate"},
                 {"id": "custom.displayMode", "value": "color-background"},
                 {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                     {"color": "dark-red", "value": None},
                     {"color": "semi-dark-red", "value": -0.05},
                     {"color": "transparent", "value": 0},
                     {"color": "semi-dark-green", "value": 0.05},
                     {"color": "dark-green", "value": 0.15},
                 ]}},
             ]},
            {"matcher": {"id": "byName", "options": "symbol"},
             "properties": [{"id": "displayName", "value": "Symbol"}]},
        ]},
        "options": {"showHeader": True,
                    "sortBy": [{"desc": True, "displayName": "Ann. Rate"}]},
        "targets": [{"expr": f'hedgehog_funding_rate_annualized{{venue="{key}"}}',
                     "legendFormat": "{{symbol}}", "refId": "A",
                     "instant": True, "format": "table"}],
        "transformations": [{"id": "sortBy", "options": {"fields": {},
                            "sort": [{"field": "Value", "desc": True}]}}],
        "datasource": DS,
    })

    # Predicted vs actual
    panels.append({
        "id": pid(), "type": "timeseries", "title": "Predicted vs Actual Rate",
        "gridPos": {"h": 9, "w": 5, "x": 19, "y": sy},
        "fieldConfig": {"defaults": {
            "unit": "percentunit",
            "custom": {"lineWidth": 2, "fillOpacity": 0, "showPoints": "never", "spanNulls": True},
        }, "overrides": [
            {"matcher": {"id": "byRegexp", "options": "/Actual/"},
             "properties": [{"id": "custom.lineStyle", "value": {"fill": "solid"}}]},
            {"matcher": {"id": "byRegexp", "options": "/Predicted/"},
             "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 5]}}]},
        ]},
        "options": {"tooltip": {"mode": "multi"},
                    "legend": {"displayMode": "list", "placement": "bottom"}},
        "targets": [
            {"expr": f'hedgehog_funding_rate_annualized{{venue="{key}",symbol=~"BTC|ETH|SOL"}}',
             "legendFormat": "Actual {{symbol}}", "refId": "A"},
            {"expr": f'hedgehog_funding_rate_predicted{{venue="{key}",symbol=~"BTC|ETH|SOL"}}',
             "legendFormat": "Predicted {{symbol}}", "refId": "B"},
        ],
        "datasource": DS,
    })

    # ═══════════════════════════════════════════════════════════════════
    # SECTION 4: FUNDING RATE STATISTICS
    # ═══════════════════════════════════════════════════════════════════
    row("Funding Rate Statistics (24h)")
    sy = next_y(6)

    # Mean rate 24h
    panels.append({
        "id": pid(), "type": "bargauge", "title": "Mean Rate 24h (Ann.)",
        "gridPos": {"h": 6, "w": 8, "x": 0, "y": sy},
        "fieldConfig": {"defaults": {
            "unit": "percentunit",
            "color": {"mode": "continuous-GrYlRd"},
            "thresholds": {"mode": "absolute", "steps": [
                {"color": "red", "value": None},
                {"color": "yellow", "value": 0},
                {"color": "green", "value": 0.05},
            ]},
        }, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]},
                    "orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True},
        "targets": [{"expr": f'hedgehog_funding_rate_mean_24h{{venue="{key}"}}',
                     "legendFormat": "{{symbol}}", "refId": "A", "instant": True}],
        "datasource": DS,
    })

    # Rate volatility (std dev)
    panels.append({
        "id": pid(), "type": "bargauge", "title": "Rate Volatility 24h (Std Dev)",
        "gridPos": {"h": 6, "w": 8, "x": 8, "y": sy},
        "fieldConfig": {"defaults": {
            "color": {"mode": "continuous-YlRd"},
            "thresholds": {"mode": "absolute", "steps": [
                {"color": "green", "value": None},
                {"color": "yellow", "value": 0.0001},
                {"color": "red", "value": 0.001},
            ]},
        }, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]},
                    "orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True},
        "targets": [{"expr": f'hedgehog_funding_rate_std_24h{{venue="{key}"}}',
                     "legendFormat": "{{symbol}}", "refId": "A", "instant": True}],
        "datasource": DS,
    })

    # Rate flips 24h
    panels.append({
        "id": pid(), "type": "bargauge", "title": "Rate Sign Flips (24h)",
        "gridPos": {"h": 6, "w": 8, "x": 16, "y": sy},
        "fieldConfig": {"defaults": {
            "color": {"mode": "thresholds"},
            "thresholds": {"mode": "absolute", "steps": [
                {"color": "green", "value": None},
                {"color": "yellow", "value": 3},
                {"color": "red", "value": 6},
            ]},
        }, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]},
                    "orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True},
        "targets": [{"expr": f'hedgehog_funding_rate_flips_24h{{venue="{key}"}}',
                     "legendFormat": "{{symbol}}", "refId": "A", "instant": True}],
        "datasource": DS,
    })

    # ═══════════════════════════════════════════════════════════════════
    # SECTION 5: MARKET DATA
    # ═══════════════════════════════════════════════════════════════════
    row("Market Data")
    sy = next_y(8)

    # Mark prices
    panels.append({
        "id": pid(), "type": "timeseries", "title": "Mark Prices",
        "gridPos": {"h": 8, "w": 8, "x": 0, "y": sy},
        "fieldConfig": {"defaults": {
            "unit": "currencyUSD",
            "color": {"mode": "palette-classic"},
            "custom": {"lineWidth": 1, "fillOpacity": 0, "showPoints": "never", "spanNulls": True},
        }, "overrides": []},
        "options": {"tooltip": {"mode": "multi", "sort": "desc"},
                    "legend": {"displayMode": "table", "placement": "right", "calcs": ["lastNotNull"]}},
        "targets": [{"expr": f'hedgehog_mark_price{{venue="{key}"}}',
                     "legendFormat": "{{symbol}}", "refId": "A"}],
        "datasource": DS,
    })

    # Orderbook spread
    panels.append({
        "id": pid(), "type": "timeseries", "title": "Bid-Ask Spread (bps)",
        "gridPos": {"h": 8, "w": 8, "x": 8, "y": sy},
        "fieldConfig": {"defaults": {
            "unit": "none", "decimals": 1,
            "color": {"mode": "palette-classic"},
            "custom": {"lineWidth": 1, "fillOpacity": 10, "showPoints": "never", "spanNulls": True,
                       "thresholdsStyle": {"mode": "line"}},
            "thresholds": {"mode": "absolute", "steps": [
                {"color": "green", "value": None},
                {"color": "yellow", "value": 5},
                {"color": "red", "value": 20},
            ]},
        }, "overrides": []},
        "options": {"tooltip": {"mode": "multi"},
                    "legend": {"displayMode": "list", "placement": "bottom"}},
        "targets": [{"expr": f'hedgehog_orderbook_spread_bps{{venue="{key}"}}',
                     "legendFormat": "{{symbol}}", "refId": "A"}],
        "datasource": DS,
    })

    # Orderbook depth
    panels.append({
        "id": pid(), "type": "timeseries", "title": "Orderbook Depth (1% from mid)",
        "gridPos": {"h": 8, "w": 8, "x": 16, "y": sy},
        "fieldConfig": {"defaults": {
            "unit": "currencyUSD",
            "color": {"mode": "palette-classic"},
            "custom": {"lineWidth": 1, "fillOpacity": 15, "showPoints": "never",
                       "stacking": {"mode": "none"}},
        }, "overrides": []},
        "options": {"tooltip": {"mode": "multi"},
                    "legend": {"displayMode": "list", "placement": "bottom"}},
        "targets": [
            {"expr": f'hedgehog_orderbook_bid_depth_usd{{venue="{key}"}}',
             "legendFormat": "Bid {{symbol}}", "refId": "A"},
            {"expr": f'hedgehog_orderbook_ask_depth_usd{{venue="{key}"}}',
             "legendFormat": "Ask {{symbol}}", "refId": "B"},
        ],
        "datasource": DS,
    })

    # ═══════════════════════════════════════════════════════════════════
    # SECTION 6: ARBITRAGE OPPORTUNITIES
    # ═══════════════════════════════════════════════════════════════════
    row("Arbitrage Opportunities")
    sy = next_y(8)

    spread_overrides = [
        {"matcher": {"id": "byName", "options": "Value"}, "properties": [
            {"id": "unit", "value": "percentunit"},
            {"id": "displayName", "value": "Spread (Ann.)"},
            {"id": "custom.displayMode", "value": "color-background"},
            {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                {"color": "transparent", "value": None},
                {"color": "semi-dark-green", "value": 0.03},
                {"color": "dark-green", "value": 0.10},
            ]}},
        ]}
    ]

    # Short leg table
    panels.append({
        "id": pid(), "type": "table", "title": f"Spreads — {name} as SHORT Leg",
        "gridPos": {"h": 8, "w": 12, "x": 0, "y": sy},
        "fieldConfig": {"defaults": {}, "overrides": spread_overrides},
        "options": {"showHeader": True, "sortBy": [{"desc": True, "displayName": "Spread (Ann.)"}]},
        "targets": [{"expr": f'hedgehog_best_spread_annualized{{short_venue="{key}"}}',
                     "legendFormat": "{{symbol}} \u2192 LONG {{long_venue}}", "refId": "A",
                     "instant": True, "format": "table"}],
        "datasource": DS,
    })

    # Long leg table
    panels.append({
        "id": pid(), "type": "table", "title": f"Spreads — {name} as LONG Leg",
        "gridPos": {"h": 8, "w": 12, "x": 12, "y": sy},
        "fieldConfig": {"defaults": {}, "overrides": spread_overrides},
        "options": {"showHeader": True, "sortBy": [{"desc": True, "displayName": "Spread (Ann.)"}]},
        "targets": [{"expr": f'hedgehog_best_spread_annualized{{long_venue="{key}"}}',
                     "legendFormat": "{{symbol}} \u2190 SHORT {{short_venue}}", "refId": "A",
                     "instant": True, "format": "table"}],
        "datasource": DS,
    })

    sy = next_y(8)

    # Spread history
    panels.append({
        "id": pid(), "type": "timeseries", "title": "Spread History",
        "gridPos": {"h": 8, "w": 24, "x": 0, "y": sy},
        "fieldConfig": {"defaults": {
            "unit": "percentunit", "color": {"mode": "palette-classic"},
            "custom": {"lineWidth": 1, "fillOpacity": 5, "showPoints": "never", "spanNulls": True},
        }, "overrides": []},
        "options": {"tooltip": {"mode": "multi", "sort": "desc"},
                    "legend": {"displayMode": "table", "placement": "right",
                               "calcs": ["lastNotNull", "max"]}},
        "targets": [
            {"expr": f'hedgehog_best_spread_annualized{{short_venue="{key}"}}',
             "legendFormat": "SHORT {{symbol}} \u2192 LONG {{long_venue}}", "refId": "A"},
            {"expr": f'hedgehog_best_spread_annualized{{long_venue="{key}"}}',
             "legendFormat": "LONG {{symbol}} \u2190 SHORT {{short_venue}}", "refId": "B"},
        ],
        "datasource": DS,
    })

    # ═══════════════════════════════════════════════════════════════════
    # SECTION 7: POSITIONS
    # ═══════════════════════════════════════════════════════════════════
    row("Positions")
    sy = next_y(8)

    # Positions as short leg
    panels.append({
        "id": pid(), "type": "table", "title": f"Positions — SHORT Leg on {name}",
        "gridPos": {"h": 8, "w": 12, "x": 0, "y": sy},
        "fieldConfig": {"defaults": {}, "overrides": [
            {"matcher": {"id": "byName", "options": "Value #size"},
             "properties": [{"id": "unit", "value": "currencyUSD"}, {"id": "displayName", "value": "Size ($)"}]},
            {"matcher": {"id": "byName", "options": "Value #pnl"},
             "properties": [
                 {"id": "unit", "value": "currencyUSD"}, {"id": "displayName", "value": "Unrealized P&L"},
                 {"id": "custom.displayMode", "value": "color-text"},
                 {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                     {"color": "red", "value": None}, {"color": "green", "value": 0}]}},
             ]},
            {"matcher": {"id": "byName", "options": "Value #funding"},
             "properties": [{"id": "unit", "value": "currencyUSD"}, {"id": "displayName", "value": "Funding Accrued"}]},
        ]},
        "options": {"showHeader": True},
        "targets": [
            {"expr": f'hedgehog_position_size_usd{{short_venue="{key}"}}',
             "legendFormat": "{{symbol}} [{{hedge_id}}]", "refId": "size", "instant": True, "format": "table"},
            {"expr": f'hedgehog_position_unrealized_pnl_usd{{venue="{key}",side="short"}}',
             "legendFormat": "{{symbol}} [{{hedge_id}}]", "refId": "pnl", "instant": True, "format": "table"},
            {"expr": f'hedgehog_position_funding_accrued_usd{{short_venue="{key}"}}',
             "legendFormat": "{{symbol}} [{{hedge_id}}]", "refId": "funding", "instant": True, "format": "table"},
        ],
        "datasource": DS,
    })

    # Positions as long leg
    panels.append({
        "id": pid(), "type": "table", "title": f"Positions — LONG Leg on {name}",
        "gridPos": {"h": 8, "w": 12, "x": 12, "y": sy},
        "fieldConfig": {"defaults": {}, "overrides": [
            {"matcher": {"id": "byName", "options": "Value #size"},
             "properties": [{"id": "unit", "value": "currencyUSD"}, {"id": "displayName", "value": "Size ($)"}]},
            {"matcher": {"id": "byName", "options": "Value #pnl"},
             "properties": [
                 {"id": "unit", "value": "currencyUSD"}, {"id": "displayName", "value": "Unrealized P&L"},
                 {"id": "custom.displayMode", "value": "color-text"},
                 {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                     {"color": "red", "value": None}, {"color": "green", "value": 0}]}},
             ]},
            {"matcher": {"id": "byName", "options": "Value #funding"},
             "properties": [{"id": "unit", "value": "currencyUSD"}, {"id": "displayName", "value": "Funding Accrued"}]},
        ]},
        "options": {"showHeader": True},
        "targets": [
            {"expr": f'hedgehog_position_size_usd{{long_venue="{key}"}}',
             "legendFormat": "{{symbol}} [{{hedge_id}}]", "refId": "size", "instant": True, "format": "table"},
            {"expr": f'hedgehog_position_unrealized_pnl_usd{{venue="{key}",side="long"}}',
             "legendFormat": "{{symbol}} [{{hedge_id}}]", "refId": "pnl", "instant": True, "format": "table"},
            {"expr": f'hedgehog_position_funding_accrued_usd{{long_venue="{key}"}}',
             "legendFormat": "{{symbol}} [{{hedge_id}}]", "refId": "funding", "instant": True, "format": "table"},
        ],
        "datasource": DS,
    })

    sy = next_y(7)

    # Position P&L over time
    panels.append({
        "id": pid(), "type": "timeseries", "title": "Position P&L Over Time",
        "gridPos": {"h": 7, "w": 12, "x": 0, "y": sy},
        "fieldConfig": {"defaults": {
            "unit": "currencyUSD", "color": {"mode": "palette-classic"},
            "custom": {"lineWidth": 2, "fillOpacity": 10, "showPoints": "never", "spanNulls": True},
        }, "overrides": []},
        "options": {"tooltip": {"mode": "multi"},
                    "legend": {"displayMode": "list", "placement": "bottom"}},
        "targets": [
            {"expr": f'hedgehog_position_unrealized_pnl_usd{{venue="{key}"}}',
             "legendFormat": "{{symbol}} [{{hedge_id}}] {{side}}", "refId": "A"},
        ],
        "datasource": DS,
    })

    # Leverage & Liquidation
    panels.append({
        "id": pid(), "type": "table", "title": "Leverage & Liquidation Prices",
        "gridPos": {"h": 7, "w": 12, "x": 12, "y": sy},
        "fieldConfig": {"defaults": {}, "overrides": [
            {"matcher": {"id": "byName", "options": "Value #lev"},
             "properties": [{"id": "displayName", "value": "Leverage"}, {"id": "decimals", "value": 1}]},
            {"matcher": {"id": "byName", "options": "Value #liq"},
             "properties": [{"id": "unit", "value": "currencyUSD"}, {"id": "displayName", "value": "Liq. Price"}]},
            {"matcher": {"id": "byName", "options": "Value #entry"},
             "properties": [{"id": "unit", "value": "currencyUSD"}, {"id": "displayName", "value": "Entry Price"}]},
            {"matcher": {"id": "byName", "options": "Value #mark"},
             "properties": [{"id": "unit", "value": "currencyUSD"}, {"id": "displayName", "value": "Mark Price"}]},
        ]},
        "options": {"showHeader": True},
        "targets": [
            {"expr": f'hedgehog_position_leverage{{venue="{key}"}}',
             "legendFormat": "{{symbol}} [{{hedge_id}}] {{side}}", "refId": "lev", "instant": True, "format": "table"},
            {"expr": f'hedgehog_position_liquidation_price{{venue="{key}"}}',
             "legendFormat": "{{symbol}} [{{hedge_id}}] {{side}}", "refId": "liq", "instant": True, "format": "table"},
            {"expr": f'hedgehog_position_entry_price{{venue="{key}"}}',
             "legendFormat": "{{symbol}} [{{hedge_id}}] {{side}}", "refId": "entry", "instant": True, "format": "table"},
            {"expr": f'hedgehog_position_mark_price{{venue="{key}"}}',
             "legendFormat": "{{symbol}} [{{hedge_id}}] {{side}}", "refId": "mark", "instant": True, "format": "table"},
        ],
        "datasource": DS,
    })

    # ═══════════════════════════════════════════════════════════════════
    # SECTION 8: ORDERS & EXECUTION
    # ═══════════════════════════════════════════════════════════════════
    row("Orders & Execution")
    sy = next_y(7)

    # Open orders
    panels.append({
        "id": pid(), "type": "stat", "title": "Open Orders",
        "gridPos": {"h": 7, "w": 3, "x": 0, "y": sy},
        "fieldConfig": {"defaults": {
            "color": {"mode": "fixed", "fixedColor": "#3b82f6"},
            "thresholds": {"mode": "absolute", "steps": [{"color": "#3b82f6", "value": None}]},
        }, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]},
                    "colorMode": "value", "graphMode": "none", "textMode": "auto"},
        "targets": [{"expr": f'hedgehog_open_orders{{venue="{key}"}}', "refId": "A"}],
        "datasource": DS,
    })

    # Total trades
    panels.append({
        "id": pid(), "type": "stat", "title": "Total Trades",
        "gridPos": {"h": 7, "w": 3, "x": 3, "y": sy},
        "fieldConfig": {"defaults": {
            "color": {"mode": "fixed", "fixedColor": color},
            "thresholds": {"mode": "absolute", "steps": [{"color": color, "value": None}]},
        }, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]},
                    "colorMode": "value", "graphMode": "area", "textMode": "auto"},
        "targets": [{"expr": f'sum(hedgehog_trades_total{{venue="{key}"}})', "refId": "A"}],
        "datasource": DS,
    })

    # Rollbacks
    panels.append({
        "id": pid(), "type": "stat", "title": "Rollbacks",
        "gridPos": {"h": 7, "w": 3, "x": 6, "y": sy},
        "fieldConfig": {"defaults": {
            "color": {"mode": "thresholds"},
            "thresholds": {"mode": "absolute", "steps": [
                {"color": "green", "value": None}, {"color": "yellow", "value": 1},
                {"color": "red", "value": 5},
            ]},
        }, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]},
                    "colorMode": "value", "graphMode": "area", "textMode": "auto"},
        "targets": [{"expr": f'hedgehog_rollbacks_total{{venue="{key}"}}', "refId": "A"}],
        "datasource": DS,
    })

    # Trades over time by action
    panels.append({
        "id": pid(), "type": "timeseries", "title": "Trades Over Time",
        "gridPos": {"h": 7, "w": 7, "x": 9, "y": sy},
        "fieldConfig": {"defaults": {
            "custom": {"lineWidth": 2, "fillOpacity": 20,
                       "stacking": {"mode": "normal"}, "showPoints": "never"},
        }, "overrides": [
            {"matcher": {"id": "byName", "options": "OPEN"},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "green"}}]},
            {"matcher": {"id": "byName", "options": "CLOSE"},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}]},
            {"matcher": {"id": "byName", "options": "ROTATE"},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "yellow"}}]},
        ]},
        "options": {"tooltip": {"mode": "multi"},
                    "legend": {"displayMode": "list", "placement": "bottom"}},
        "targets": [{"expr": f'increase(hedgehog_trades_total{{venue="{key}"}}[5m])',
                     "legendFormat": "{{action}}", "refId": "A"}],
        "datasource": DS,
    })

    # Order fill time histogram
    panels.append({
        "id": pid(), "type": "heatmap", "title": "Order Fill Time Distribution",
        "gridPos": {"h": 7, "w": 8, "x": 16, "y": sy},
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {"calculate": True, "color": {"scheme": "Oranges", "mode": "scheme"},
                    "cellGap": 2, "yAxis": {"axisPlacement": "left"},
                    "tooltip": {"show": True}},
        "targets": [{"expr": f'hedgehog_order_fill_time_seconds_bucket{{venue="{key}"}}',
                     "legendFormat": "{{le}}", "refId": "A", "format": "heatmap"}],
        "datasource": DS,
    })

    # ═══════════════════════════════════════════════════════════════════
    # SECTION 9: ERRORS & SLIPPAGE
    # ═══════════════════════════════════════════════════════════════════
    row("Errors & Slippage")
    sy = next_y(7)

    # Order errors by type
    panels.append({
        "id": pid(), "type": "timeseries", "title": "Order Errors",
        "gridPos": {"h": 7, "w": 8, "x": 0, "y": sy},
        "fieldConfig": {"defaults": {
            "custom": {"lineWidth": 2, "fillOpacity": 20, "showPoints": "auto",
                       "stacking": {"mode": "normal"}},
        }, "overrides": [
            {"matcher": {"id": "byName", "options": "exception"},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}]},
            {"matcher": {"id": "byName", "options": "rejected"},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "orange"}}]},
            {"matcher": {"id": "byName", "options": "partial_fill"},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "yellow"}}]},
            {"matcher": {"id": "byName", "options": "timeout"},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": "purple"}}]},
        ]},
        "options": {"tooltip": {"mode": "multi"},
                    "legend": {"displayMode": "list", "placement": "bottom"}},
        "targets": [{"expr": f'increase(hedgehog_order_errors_total{{venue="{key}"}}[5m])',
                     "legendFormat": "{{error_type}}", "refId": "A"}],
        "datasource": DS,
    })

    # Order error totals
    panels.append({
        "id": pid(), "type": "table", "title": "Error Summary",
        "gridPos": {"h": 7, "w": 4, "x": 8, "y": sy},
        "fieldConfig": {"defaults": {}, "overrides": [
            {"matcher": {"id": "byName", "options": "Value"},
             "properties": [{"id": "displayName", "value": "Count"},
                            {"id": "custom.displayMode", "value": "color-background"},
                            {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                                {"color": "green", "value": None},
                                {"color": "yellow", "value": 1},
                                {"color": "red", "value": 10},
                            ]}}]},
        ]},
        "options": {"showHeader": True, "sortBy": [{"desc": True, "displayName": "Count"}]},
        "targets": [{"expr": f'hedgehog_order_errors_total{{venue="{key}"}}',
                     "legendFormat": "{{error_type}}", "refId": "A",
                     "instant": True, "format": "table"}],
        "datasource": DS,
    })

    # Slippage histogram
    panels.append({
        "id": pid(), "type": "histogram", "title": "Slippage Distribution (bps)",
        "gridPos": {"h": 7, "w": 6, "x": 12, "y": sy},
        "fieldConfig": {"defaults": {
            "color": {"mode": "fixed", "fixedColor": color},
            "custom": {"fillOpacity": 80},
        }, "overrides": []},
        "options": {"bucketSize": 2, "combine": True,
                    "tooltip": {"mode": "single"},
                    "legend": {"displayMode": "hidden"}},
        "targets": [{"expr": f'hedgehog_order_slippage_bps_bucket{{venue="{key}"}}',
                     "legendFormat": "{{le}}", "refId": "A", "format": "heatmap"}],
        "datasource": DS,
    })

    # API latency
    panels.append({
        "id": pid(), "type": "timeseries", "title": "API Latency",
        "gridPos": {"h": 7, "w": 6, "x": 18, "y": sy},
        "fieldConfig": {"defaults": {
            "unit": "s", "color": {"mode": "palette-classic"},
            "custom": {"lineWidth": 1, "fillOpacity": 10, "showPoints": "never"},
        }, "overrides": []},
        "options": {"tooltip": {"mode": "multi"},
                    "legend": {"displayMode": "list", "placement": "bottom"}},
        "targets": [
            {"expr": f'rate(hedgehog_venue_api_latency_seconds_sum{{venue="{key}"}}[5m]) / rate(hedgehog_venue_api_latency_seconds_count{{venue="{key}"}}[5m])',
             "legendFormat": "avg {{endpoint}}", "refId": "A"},
            {"expr": f'histogram_quantile(0.95, rate(hedgehog_venue_api_latency_seconds_bucket{{venue="{key}"}}[5m]))',
             "legendFormat": "p95", "refId": "B"},
        ],
        "datasource": DS,
    })

    # ═══════════════════════════════════════════════════════════════════
    # SECTION 10: ORDER HISTORY (TimescaleDB)
    # ═══════════════════════════════════════════════════════════════════
    row("Order History (Database)")
    sy = next_y(8)

    panels.append({
        "id": pid(), "type": "table", "title": "Recent Orders",
        "gridPos": {"h": 8, "w": 24, "x": 0, "y": sy},
        "fieldConfig": {"defaults": {}, "overrides": [
            {"matcher": {"id": "byName", "options": "fees_paid"},
             "properties": [{"id": "unit", "value": "currencyUSD"}]},
            {"matcher": {"id": "byName", "options": "gas_cost"},
             "properties": [{"id": "unit", "value": "currencyUSD"}]},
            {"matcher": {"id": "byName", "options": "avg_price"},
             "properties": [{"id": "unit", "value": "currencyUSD"}]},
            {"matcher": {"id": "byName", "options": "slippage_bps"},
             "properties": [{"id": "unit", "value": "none"}, {"id": "displayName", "value": "Slippage (bps)"}]},
            {"matcher": {"id": "byName", "options": "status"},
             "properties": [{"id": "custom.displayMode", "value": "color-text"},
                            {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                                {"color": "green", "value": None}]}}]},
        ]},
        "options": {"showHeader": True, "sortBy": [{"desc": True, "displayName": "timestamp"}]},
        "targets": [{
            "rawSql": f"SELECT timestamp, symbol, side, order_id, status, filled_qty, avg_price, fees_paid, gas_cost, slippage_bps, tx_hash FROM execution_log WHERE venue = '{key}' ORDER BY timestamp DESC LIMIT 50",
            "refId": "A", "format": "table",
        }],
        "datasource": DS_PG,
    })

    # ═══════════════════════════════════════════════════════════════════
    # SECTION 11: FUNDING PAYMENT HISTORY (TimescaleDB)
    # ═══════════════════════════════════════════════════════════════════
    row("Funding Payment History (Database)")
    sy = next_y(8)

    panels.append({
        "id": pid(), "type": "timeseries", "title": "Funding Rates History (from DB)",
        "gridPos": {"h": 8, "w": 16, "x": 0, "y": sy},
        "fieldConfig": {"defaults": {
            "unit": "percentunit", "color": {"mode": "palette-classic"},
            "custom": {"lineWidth": 1, "fillOpacity": 5, "showPoints": "never", "spanNulls": True},
        }, "overrides": []},
        "options": {"tooltip": {"mode": "multi", "sort": "desc"},
                    "legend": {"displayMode": "table", "placement": "right",
                               "calcs": ["lastNotNull", "mean"]}},
        "targets": [{
            "rawSql": f"SELECT hour AS time, symbol, avg_annualized AS value FROM funding_rates_hourly WHERE venue = '{key}' AND $__timeFilter(hour) ORDER BY hour",
            "refId": "A", "format": "time_series",
        }],
        "datasource": DS_PG,
    })

    panels.append({
        "id": pid(), "type": "table", "title": "Recent Funding Snapshots",
        "gridPos": {"h": 8, "w": 8, "x": 16, "y": sy},
        "fieldConfig": {"defaults": {}, "overrides": [
            {"matcher": {"id": "byName", "options": "annualized"},
             "properties": [{"id": "unit", "value": "percentunit"}]},
            {"matcher": {"id": "byName", "options": "mark_price"},
             "properties": [{"id": "unit", "value": "currencyUSD"}]},
            {"matcher": {"id": "byName", "options": "open_interest"},
             "properties": [{"id": "unit", "value": "currencyUSD"}]},
        ]},
        "options": {"showHeader": True, "sortBy": [{"desc": True, "displayName": "timestamp"}]},
        "targets": [{
            "rawSql": f"SELECT timestamp, symbol, rate, annualized, mark_price, index_price, open_interest, predicted_rate FROM funding_rates WHERE venue = '{key}' ORDER BY timestamp DESC LIMIT 50",
            "refId": "A", "format": "table",
        }],
        "datasource": DS_PG,
    })

    # ═══════════════════════════════════════════════════════════════════
    # SECTION 12: SCORE BREAKDOWN & HEALTH
    # ═══════════════════════════════════════════════════════════════════
    row("Score Breakdown & Health")
    sy = next_y(8)

    # Score components radar/bar
    panels.append({
        "id": pid(), "type": "bargauge", "title": "Score Components",
        "gridPos": {"h": 8, "w": 8, "x": 0, "y": sy},
        "fieldConfig": {"defaults": {
            "min": 0, "max": 1,
            "color": {"mode": "continuous-GrYlRd"},
            "thresholds": {"mode": "absolute", "steps": [
                {"color": "red", "value": None},
                {"color": "yellow", "value": 0.4},
                {"color": "green", "value": 0.7},
            ]},
        }, "overrides": []},
        "options": {"reduceOptions": {"calcs": ["lastNotNull"]},
                    "orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True},
        "targets": [{"expr": f'hedgehog_venue_score_component{{venue="{key}"}}',
                     "legendFormat": "{{component}}", "refId": "A", "instant": True}],
        "datasource": DS,
    })

    # Score over time
    panels.append({
        "id": pid(), "type": "timeseries", "title": "Score Over Time",
        "gridPos": {"h": 8, "w": 8, "x": 8, "y": sy},
        "fieldConfig": {"defaults": {
            "min": 0, "max": 1,
            "color": {"mode": "fixed", "fixedColor": color},
            "custom": {"lineWidth": 2, "fillOpacity": 15, "gradientMode": "opacity",
                       "showPoints": "never", "spanNulls": True,
                       "thresholdsStyle": {"mode": "area"}},
            "thresholds": {"mode": "absolute", "steps": [
                {"color": "rgba(239,68,68,0.1)", "value": None},
                {"color": "rgba(245,158,11,0.1)", "value": 0.4},
                {"color": "rgba(16,185,129,0.1)", "value": 0.7},
            ]},
        }, "overrides": []},
        "options": {"tooltip": {"mode": "single"},
                    "legend": {"displayMode": "hidden"}},
        "targets": [{"expr": f'hedgehog_venue_score{{venue="{key}"}}',
                     "legendFormat": "Score", "refId": "A"}],
        "datasource": DS,
    })

    # Uptime timeline
    panels.append({
        "id": pid(), "type": "state-timeline", "title": "Uptime History",
        "gridPos": {"h": 8, "w": 8, "x": 16, "y": sy},
        "fieldConfig": {"defaults": {
            "color": {"mode": "thresholds"},
            "thresholds": {"mode": "absolute", "steps": [
                {"color": "red", "value": None}, {"color": "green", "value": 1}]},
            "mappings": [{"type": "value", "options": {
                "0": {"text": "DOWN", "color": "red"},
                "1": {"text": "UP", "color": "green"},
            }}],
        }, "overrides": []},
        "options": {"showValue": "auto", "rowHeight": 0.8,
                    "mergeValues": True, "alignValue": "center"},
        "targets": [{"expr": f'hedgehog_venue_up{{venue="{key}"}}',
                     "legendFormat": name, "refId": "A"}],
        "datasource": DS,
    })

    # ═══════════════════════════════════════════════════════════════════
    # SECTION 13: VENUE SCORE HISTORY (TimescaleDB)
    # ═══════════════════════════════════════════════════════════════════
    row("Score History (Database)")
    sy = next_y(7)

    panels.append({
        "id": pid(), "type": "timeseries", "title": "Composite Score History (DB)",
        "gridPos": {"h": 7, "w": 12, "x": 0, "y": sy},
        "fieldConfig": {"defaults": {
            "min": 0, "max": 1, "color": {"mode": "palette-classic"},
            "custom": {"lineWidth": 1, "fillOpacity": 5, "showPoints": "never"},
        }, "overrides": []},
        "options": {"tooltip": {"mode": "multi"},
                    "legend": {"displayMode": "table", "placement": "right", "calcs": ["lastNotNull", "mean"]}},
        "targets": [{
            "rawSql": f"SELECT timestamp AS time, symbol, composite_score AS value FROM venue_scores WHERE venue = '{key}' AND $__timeFilter(timestamp) ORDER BY timestamp",
            "refId": "A", "format": "time_series",
        }],
        "datasource": DS_PG,
    })

    panels.append({
        "id": pid(), "type": "table", "title": "Recent Score Snapshots",
        "gridPos": {"h": 7, "w": 12, "x": 12, "y": sy},
        "fieldConfig": {"defaults": {}, "overrides": [
            {"matcher": {"id": "byName", "options": "composite_score"},
             "properties": [{"id": "custom.displayMode", "value": "color-background"},
                            {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                                {"color": "red", "value": None}, {"color": "yellow", "value": 0.4},
                                {"color": "green", "value": 0.7}]}}]},
        ]},
        "options": {"showHeader": True, "sortBy": [{"desc": True, "displayName": "timestamp"}]},
        "targets": [{
            "rawSql": f"SELECT timestamp, symbol, composite_score, avg_funding_30d, liquidity_depth, fee_score, consistency_score FROM venue_scores WHERE venue = '{key}' ORDER BY timestamp DESC LIMIT 30",
            "refId": "A", "format": "table",
        }],
        "datasource": DS_PG,
    })

    # Build dashboard
    dashboard = {
        "annotations": {"list": []},
        "editable": True,
        "fiscalYearStartMonth": 0,
        "graphTooltip": 1,
        "links": links,
        "panels": panels,
        "refresh": "15s",
        "schemaVersion": 39,
        "tags": ["hedgehog", "defi", "venue", key],
        "templating": {"list": [
            {"current": {"selected": False, "text": "Prometheus", "value": "Prometheus"},
             "hide": 2, "includeAll": False, "name": "DS_PROMETHEUS",
             "options": [], "query": "prometheus", "type": "datasource"},
            {"current": {"selected": False, "text": "TimescaleDB", "value": "TimescaleDB"},
             "hide": 2, "includeAll": False, "name": "DS_TIMESCALEDB",
             "options": [], "query": "postgres", "type": "datasource"},
        ]},
        "time": {"from": "now-6h", "to": "now"},
        "timepicker": {},
        "timezone": "browser",
        "title": f"HedgeHog \u2014 {name}",
        "uid": uid,
        "version": 1,
    }
    return dashboard


def add_links_to_main_dashboard(main_path):
    with open(main_path) as f:
        main = json.load(f)
    links = []
    for v in VENUES:
        links.append({
            "title": v["name"],
            "url": f"/d/hedgehog-venue-{v['key']}",
            "type": "link", "icon": "exchange-alt", "targetBlank": False,
        })
    main["links"] = links
    main["tags"] = ["hedgehog", "defi", "funding-rates", "main"]
    with open(main_path, "w") as f:
        json.dump(main, f, indent=2)
    print(f"  Updated: {main_path}")


def main():
    out_dir = os.path.join(os.path.dirname(__file__), "..", "config", "grafana", "dashboards")
    os.makedirs(out_dir, exist_ok=True)

    for v in VENUES:
        dashboard = make_venue_dashboard(v)
        path = os.path.join(out_dir, f"venue_{v['key']}.json")
        with open(path, "w") as f:
            json.dump(dashboard, f, indent=2)
        panel_count = len([p for p in dashboard["panels"] if p.get("type") != "row"])
        print(f"  Created: venue_{v['key']}.json ({panel_count} panels)")

    main_path = os.path.join(out_dir, "hedgehog.json")
    if os.path.exists(main_path):
        add_links_to_main_dashboard(main_path)

    print(f"\nGenerated {len(VENUES)} venue dashboards + updated main dashboard.")


if __name__ == "__main__":
    main()
