#!/usr/bin/env python3
"""
Hedgehog Dashboard Styler
Generates a sleek, modern main dashboard and applies consistent
styling transforms to all venue dashboards.

Aesthetic: Dark terminal command-center with neon accent colors.
"""

import json
from pathlib import Path
from copy import deepcopy

DASHBOARD_DIR = Path(__file__).parent.parent / "config" / "grafana" / "dashboards"

# ── Color Palette ─────────────────────────────────────────────────────────
C = {
    "amber":   "#F59E0B",
    "emerald": "#10B981",
    "red":     "#EF4444",
    "cyan":    "#06B6D4",
    "violet":  "#8B5CF6",
    "blue":    "#3B82F6",
    "orange":  "#F97316",
    "pink":    "#EC4899",
    "slate":   "#64748B",
    "teal":    "#14B8A6",
}

DS = {"type": "prometheus", "uid": "hedgehog-prometheus"}

# ── Venue nav links ──────────────────────────────────────────────────────
VENUES = [
    ("Hyperliquid", "hyperliquid"),
    ("Lighter", "lighter"),
    ("Aster", "aster"),
    ("Drift", "drift"),
    ("dYdX v4", "dydx"),
    ("ApeX Omni", "apex"),
    ("Paradex", "paradex"),
    ("Ethereal", "ethereal"),
    ("Injective", "injective"),
]


def venue_links(exclude=None):
    links = []
    for label, slug in VENUES:
        if slug == exclude:
            continue
        links.append({
            "title": label,
            "url": f"/d/hedgehog-venue-{slug}",
            "type": "link",
            "icon": "bolt",
            "targetBlank": False,
        })
    return links


def main_links():
    return [{"title": "Main Dashboard", "url": "/d/hedgehog-main",
             "type": "link", "icon": "home", "targetBlank": False}]


# ── Panel helpers ─────────────────────────────────────────────────────────

def _gp(h, w, x, y):
    return {"h": h, "w": w, "x": x, "y": y}


def row(pid, title, y, collapsed=False):
    return {
        "id": pid, "type": "row", "title": title,
        "gridPos": _gp(1, 24, 0, y),
        "collapsed": collapsed, "panels": [],
    }


def stat_panel(pid, title, expr, gp, color, unit=None,
               graph_mode="area", color_mode="value",
               thresholds=None, mappings=None, decimals=None,
               text_mode="auto", description=None):
    fd = {"color": {"mode": "fixed", "fixedColor": color}}
    if unit:
        fd["unit"] = unit
    if decimals is not None:
        fd["decimals"] = decimals
    if thresholds:
        fd["thresholds"] = thresholds
    else:
        fd["thresholds"] = {
            "mode": "absolute",
            "steps": [{"color": color, "value": None}],
        }
    if mappings:
        fd["mappings"] = mappings
    p = {
        "id": pid, "type": "stat", "title": title,
        "transparent": True,
        "gridPos": gp,
        "fieldConfig": {"defaults": fd, "overrides": []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"]},
            "colorMode": color_mode,
            "graphMode": graph_mode,
            "textMode": text_mode,
        },
        "targets": [{"expr": expr, "refId": "A"}],
        "datasource": DS,
    }
    if description:
        p["description"] = description
    return p


def timeseries_panel(pid, title, targets, gp, color=None,
                     unit=None, fill=18, lw=2,
                     legend_mode="hidden", legend_placement="bottom",
                     legend_calcs=None, tooltip="single",
                     stacking=None, overrides=None,
                     description=None, show_points="never"):
    custom = {
        "lineWidth": lw,
        "fillOpacity": fill,
        "gradientMode": "opacity",
        "lineInterpolation": "smooth",
        "showPoints": show_points,
        "spanNulls": True,
        "axisBorderShow": False,
        "barAlignment": 0,
    }
    if stacking:
        custom["stacking"] = stacking
    fd = {"custom": custom}
    if unit:
        fd["unit"] = unit
    if color:
        fd["color"] = {"mode": "fixed", "fixedColor": color}
    fd["thresholds"] = {
        "mode": "absolute",
        "steps": [{"color": color or C["slate"], "value": None}],
    }
    opts = {
        "tooltip": {"mode": tooltip},
        "legend": {"displayMode": legend_mode, "placement": legend_placement},
    }
    if legend_calcs:
        opts["legend"]["calcs"] = legend_calcs
    p = {
        "id": pid, "type": "timeseries", "title": title,
        "transparent": True,
        "gridPos": gp,
        "fieldConfig": {"defaults": fd, "overrides": overrides or []},
        "options": opts,
        "targets": targets,
        "datasource": DS,
    }
    if description:
        p["description"] = description
    return p


def gauge_panel(pid, title, expr, gp, unit="percent",
                min_val=0, max_val=100, steps=None, description=None):
    if steps is None:
        steps = [
            {"color": C["emerald"], "value": None},
            {"color": C["amber"], "value": max_val * 0.6},
            {"color": C["red"], "value": max_val * 0.9},
        ]
    p = {
        "id": pid, "type": "gauge", "title": title,
        "transparent": True,
        "gridPos": gp,
        "fieldConfig": {
            "defaults": {
                "unit": unit, "min": min_val, "max": max_val,
                "color": {"mode": "thresholds"},
                "thresholds": {"mode": "absolute", "steps": steps},
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"]},
            "showThresholdLabels": True,
            "showThresholdMarkers": True,
            "orientation": "auto",
        },
        "targets": [{"expr": expr, "refId": "A"}],
        "datasource": DS,
    }
    if description:
        p["description"] = description
    return p


# ── Main Dashboard ────────────────────────────────────────────────────────

def generate_main_dashboard():
    panels = []
    pid = 1

    # ━━ PORTFOLIO OVERVIEW ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    panels.append(row(pid, "Portfolio Overview", 0)); pid += 1

    panels.append(stat_panel(
        pid, "Portfolio NAV", "hedgehog_portfolio_nav_usd",
        _gp(5, 5, 0, 1), C["amber"], unit="currencyUSD", decimals=0,
        description="Total portfolio net asset value across all venues"
    )); pid += 1

    panels.append(stat_panel(
        pid, "Net P&L", "hedgehog_portfolio_pnl_usd",
        _gp(5, 5, 5, 1), C["emerald"], unit="currencyUSD", decimals=0,
        thresholds={"mode": "absolute", "steps": [
            {"color": C["red"], "value": None},
            {"color": C["emerald"], "value": 0},
        ]},
        description="Cumulative profit & loss"
    )); pid += 1

    panels.append(stat_panel(
        pid, "Funding Earned", "hedgehog_portfolio_funding_collected_usd",
        _gp(5, 5, 10, 1), C["teal"], unit="currencyUSD", decimals=0,
        description="Total funding rate payments collected"
    )); pid += 1

    panels.append(stat_panel(
        pid, "Best Spread", "max(hedgehog_best_spread_annualized)",
        _gp(5, 5, 15, 1), C["cyan"], unit="percentunit", decimals=1,
        graph_mode="none",
        description="Highest annualized spread available across all venue pairs"
    )); pid += 1

    panels.append(stat_panel(
        pid, "Active Hedges", "hedgehog_active_positions",
        _gp(5, 4, 20, 1), C["violet"], graph_mode="none",
        description="Number of active hedge position pairs"
    )); pid += 1

    # NAV chart
    panels.append(timeseries_panel(
        pid, "Portfolio Performance",
        [{"expr": "hedgehog_portfolio_nav_usd", "legendFormat": "NAV", "refId": "A"}],
        _gp(9, 24, 0, 6), color=C["amber"], unit="currencyUSD", fill=20, lw=2,
        description="Portfolio NAV over time"
    )); pid += 1

    # ━━ FUNDING RATES & OPPORTUNITIES ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    panels.append(row(pid, "Funding Rates & Opportunities", 15)); pid += 1

    # Heatmap
    panels.append({
        "id": pid, "type": "heatmap",
        "title": "Rate Heatmap (Annualized)",
        "transparent": True,
        "gridPos": _gp(10, 14, 0, 16),
        "fieldConfig": {"defaults": {}, "overrides": []},
        "options": {
            "calculate": False,
            "color": {"scheme": "Spectral", "mode": "scheme", "fill": "opacity",
                      "steps": 128, "reverse": False},
            "cellGap": 1,
            "cellRadius": 2,
            "yAxis": {"axisPlacement": "left"},
            "tooltip": {"show": True, "yHistogram": False},
            "filterValues": {"le": 1e-9},
            "showValue": "never",
        },
        "targets": [{
            "expr": "hedgehog_funding_rate_annualized",
            "legendFormat": "{{venue}} / {{symbol}}",
            "refId": "A", "format": "heatmap",
        }],
        "datasource": DS,
        "description": "Annualized funding rates across all venues and symbols. Green = positive (short pays long), Red = negative",
    }); pid += 1

    # Top opportunities table
    panels.append({
        "id": pid, "type": "table",
        "title": "Top Opportunities",
        "transparent": True,
        "gridPos": _gp(10, 10, 14, 16),
        "fieldConfig": {
            "defaults": {
                "custom": {
                    "align": "auto",
                    "cellOptions": {"type": "auto"},
                    "filterable": False,
                },
            },
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "Value"},
                    "properties": [
                        {"id": "unit", "value": "percentunit"},
                        {"id": "decimals", "value": 1},
                        {"id": "custom.cellOptions", "value": {
                            "type": "color-background",
                            "mode": "gradient",
                        }},
                        {"id": "thresholds", "value": {
                            "mode": "absolute",
                            "steps": [
                                {"color": "transparent", "value": None},
                                {"color": "#065F46", "value": 0.03},
                                {"color": "#047857", "value": 0.06},
                                {"color": "#059669", "value": 0.10},
                                {"color": "#10B981", "value": 0.20},
                            ],
                        }},
                    ],
                },
            ],
        },
        "options": {
            "showHeader": True,
            "sortBy": [{"desc": True, "displayName": "Value"}],
            "cellHeight": "sm",
            "footer": {"show": False},
        },
        "targets": [{
            "expr": "topk(10, hedgehog_best_spread_annualized)",
            "legendFormat": "{{symbol}}  SHORT {{short_venue}} / LONG {{long_venue}}",
            "refId": "A", "instant": True, "format": "table",
        }],
        "transformations": [{
            "id": "sortBy",
            "options": {
                "fields": {},
                "sort": [{"field": "Value", "desc": True}],
            },
        }],
        "datasource": DS,
        "description": "Top arbitrage opportunities ranked by annualized spread",
    }); pid += 1

    # ━━ RISK MONITOR ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    panels.append(row(pid, "Risk Monitor", 26)); pid += 1

    # Circuit breaker + healthy venues in a compact row
    panels.append(stat_panel(
        pid, "Circuit Breaker", "hedgehog_circuit_breaker_triggered",
        _gp(3, 4, 0, 27), C["emerald"],
        color_mode="background",
        graph_mode="none",
        mappings=[{"type": "value", "options": {
            "0": {"text": "CLEAR", "color": C["emerald"]},
            "1": {"text": "TRIPPED", "color": C["red"]},
        }}],
        thresholds={"mode": "absolute", "steps": [
            {"color": C["emerald"], "value": None},
            {"color": C["red"], "value": 1},
        ]},
        description="Emergency halt status — if tripped, all positions are force-closed"
    )); pid += 1

    panels.append(stat_panel(
        pid, "Healthy Venues", "hedgehog_venues_healthy",
        _gp(3, 4, 4, 27), C["emerald"],
        graph_mode="none", text_mode="value_and_name",
        thresholds={"mode": "absolute", "steps": [
            {"color": C["red"], "value": None},
            {"color": C["amber"], "value": 5},
            {"color": C["emerald"], "value": 7},
        ]},
        description="Number of connected venues reporting healthy status"
    )); pid += 1

    # Risk gauges
    risk_gauges = [
        ("Drawdown", "hedgehog_risk_drawdown_pct", 0, 5,
         [{"color": C["emerald"], "value": None},
          {"color": C["amber"], "value": 3},
          {"color": C["red"], "value": 4.5}],
         "Current drawdown from peak NAV — hard limit 5%"),
        ("Venue Exposure", "hedgehog_risk_max_venue_exposure_pct", 0, 25,
         [{"color": C["emerald"], "value": None},
          {"color": C["amber"], "value": 18},
          {"color": C["red"], "value": 23}],
         "Maximum single-venue capital concentration — limit 25%"),
        ("Chain Risk", "hedgehog_risk_chain_concentration_pct", 0, 40,
         [{"color": C["emerald"], "value": None},
          {"color": C["amber"], "value": 28},
          {"color": C["red"], "value": 36}],
         "Maximum single-chain capital concentration — limit 40%"),
        ("Margin Used", "hedgehog_risk_margin_utilization_pct", 0, 60,
         [{"color": C["emerald"], "value": None},
          {"color": C["amber"], "value": 45},
          {"color": C["red"], "value": 55}],
         "Total margin utilization across all venues — limit 60%"),
        ("Oracle Div.", "hedgehog_risk_oracle_divergence_pct", 0, 0.5,
         [{"color": C["emerald"], "value": None},
          {"color": C["amber"], "value": 0.3},
          {"color": C["red"], "value": 0.45}],
         "Maximum oracle price divergence between venues"),
        ("Bridge Risk", "hedgehog_risk_bridge_transit_pct", 0, 10,
         [{"color": C["emerald"], "value": None},
          {"color": C["amber"], "value": 5},
          {"color": C["red"], "value": 9}],
         "Capital currently in-transit through cross-chain bridges"),
    ]

    for i, (title, expr, mn, mx, steps, desc) in enumerate(risk_gauges):
        panels.append(gauge_panel(
            pid, title, expr,
            _gp(7, 4, i * 4, 30),
            min_val=mn, max_val=mx, steps=steps, description=desc,
        )); pid += 1

    # ━━ POSITIONS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    panels.append(row(pid, "Positions", 37)); pid += 1

    panels.append({
        "id": pid, "type": "table",
        "title": "Active Hedge Positions",
        "transparent": True,
        "gridPos": _gp(8, 24, 0, 38),
        "fieldConfig": {
            "defaults": {
                "custom": {
                    "align": "auto",
                    "cellOptions": {"type": "auto"},
                    "filterable": True,
                },
            },
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "Value #pnl"},
                    "properties": [
                        {"id": "unit", "value": "currencyUSD"},
                        {"id": "decimals", "value": 2},
                        {"id": "displayName", "value": "Net P&L"},
                        {"id": "thresholds", "value": {
                            "mode": "absolute",
                            "steps": [
                                {"color": C["red"], "value": None},
                                {"color": C["emerald"], "value": 0},
                            ],
                        }},
                        {"id": "custom.cellOptions", "value": {
                            "type": "color-text",
                        }},
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "Value #size"},
                    "properties": [
                        {"id": "unit", "value": "currencyUSD"},
                        {"id": "decimals", "value": 0},
                        {"id": "displayName", "value": "Size"},
                    ],
                },
            ],
        },
        "options": {
            "showHeader": True,
            "cellHeight": "sm",
            "footer": {"show": False},
        },
        "targets": [
            {
                "expr": "hedgehog_position_pnl_usd",
                "legendFormat": "{{symbol}} — {{hedge_id}}",
                "refId": "pnl", "instant": True, "format": "table",
            },
            {
                "expr": "hedgehog_position_size_usd",
                "legendFormat": "{{symbol}} — {{hedge_id}}",
                "refId": "size", "instant": True, "format": "table",
            },
        ],
        "datasource": DS,
        "description": "All active hedge pairs — each row is a short+long leg pair",
    }); pid += 1

    # ━━ VENUE INTELLIGENCE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    panels.append(row(pid, "Venue Intelligence", 46)); pid += 1

    # Bar gauge — LCD mode
    panels.append({
        "id": pid, "type": "bargauge",
        "title": "Venue Scores",
        "transparent": True,
        "gridPos": _gp(9, 12, 0, 47),
        "fieldConfig": {
            "defaults": {
                "min": 0, "max": 1,
                "color": {"mode": "continuous-BlYlRd"},
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"color": C["red"], "value": None},
                        {"color": C["amber"], "value": 0.4},
                        {"color": C["emerald"], "value": 0.7},
                    ],
                },
                "decimals": 2,
            },
            "overrides": [],
        },
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"]},
            "orientation": "horizontal",
            "displayMode": "lcd",
            "showUnfilled": True,
            "valueMode": "text",
            "namePlacement": "left",
        },
        "targets": [{
            "expr": "sort_desc(hedgehog_venue_score)",
            "legendFormat": "{{venue}}", "refId": "A", "instant": True,
        }],
        "datasource": DS,
        "description": "Composite venue scores (0-1) based on rates, consistency, liquidity, fees, cycle frequency, maturity, uptime",
    }); pid += 1

    # State timeline — venue health
    panels.append({
        "id": pid, "type": "state-timeline",
        "title": "Venue Uptime",
        "transparent": True,
        "gridPos": _gp(9, 12, 12, 47),
        "fieldConfig": {
            "defaults": {
                "color": {"mode": "thresholds"},
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"color": C["red"], "value": None},
                        {"color": C["emerald"], "value": 1},
                    ],
                },
                "mappings": [{
                    "type": "value",
                    "options": {
                        "0": {"text": "DOWN", "color": C["red"]},
                        "1": {"text": "UP", "color": C["emerald"]},
                    },
                }],
            },
            "overrides": [],
        },
        "options": {
            "showValue": "auto",
            "rowHeight": 0.75,
            "mergeValues": True,
            "alignValue": "center",
        },
        "targets": [{
            "expr": "hedgehog_venue_up",
            "legendFormat": "{{venue}}", "refId": "A",
        }],
        "datasource": DS,
        "description": "Historical venue connectivity status",
    }); pid += 1

    # ━━ STRATEGY ENGINE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    panels.append(row(pid, "Strategy Engine", 56)); pid += 1

    panels.append(stat_panel(
        pid, "Total Cycles", "hedgehog_cycles_total",
        _gp(4, 6, 0, 57), C["blue"],
        description="Total strategy loop iterations since startup"
    )); pid += 1

    panels.append(timeseries_panel(
        pid, "Cycle Duration",
        [{"expr": "rate(hedgehog_cycle_duration_seconds_sum[5m]) / rate(hedgehog_cycle_duration_seconds_count[5m])",
          "legendFormat": "avg cycle", "refId": "A"}],
        _gp(7, 12, 6, 57), color=C["violet"], unit="s", fill=12,
        description="Average time per strategy loop iteration"
    )); pid += 1

    # Trade decisions — stacked
    panels.append(timeseries_panel(
        pid, "Trade Decisions",
        [{"expr": "increase(hedgehog_trade_decisions_total[5m])",
          "legendFormat": "{{decision}}", "refId": "A"}],
        _gp(7, 6, 18, 57), fill=25, lw=2,
        legend_mode="list", legend_placement="bottom",
        tooltip="multi",
        stacking={"mode": "normal"},
        overrides=[
            {"matcher": {"id": "byName", "options": "APPROVE"},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": C["emerald"]}}]},
            {"matcher": {"id": "byName", "options": "REJECT"},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": C["red"]}}]},
            {"matcher": {"id": "byName", "options": "RESIZE"},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": C["amber"]}}]},
        ],
        description="Risk engine trade approval/rejection decisions over time"
    )); pid += 1

    # Fees & Gas
    panels.append(timeseries_panel(
        pid, "Fees & Gas",
        [
            {"expr": "hedgehog_fees_paid_usd_total", "legendFormat": "Trading Fees", "refId": "A"},
            {"expr": "hedgehog_gas_paid_usd_total", "legendFormat": "Gas Costs", "refId": "B"},
        ],
        _gp(7, 12, 0, 64), unit="currencyUSD", fill=12, lw=2,
        legend_mode="list", legend_placement="bottom", tooltip="multi",
        overrides=[
            {"matcher": {"id": "byName", "options": "Trading Fees"},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": C["pink"]}}]},
            {"matcher": {"id": "byName", "options": "Gas Costs"},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": C["orange"]}}]},
        ],
        description="Cumulative trading fees and gas costs"
    )); pid += 1

    # Funding rates over time
    panels.append(timeseries_panel(
        pid, "Funding Rates (BTC / ETH / SOL)",
        [{"expr": 'hedgehog_funding_rate_annualized{symbol=~"BTC|ETH|SOL"}',
          "legendFormat": "{{symbol}}/{{venue}}", "refId": "A"}],
        _gp(7, 12, 12, 64), unit="percentunit", fill=0, lw=1,
        legend_mode="table", legend_placement="right",
        legend_calcs=["lastNotNull"],
        tooltip="multi",
        description="Annualized funding rates for major assets across all venues"
    )); pid += 1

    dashboard = {
        "annotations": {"list": []},
        "editable": True,
        "fiscalYearStartMonth": 0,
        "graphTooltip": 2,
        "links": venue_links(),
        "liveNow": True,
        "panels": panels,
        "refresh": "10s",
        "schemaVersion": 39,
        "tags": ["hedgehog", "defi", "funding-rates", "main"],
        "templating": {
            "list": [{
                "current": {"selected": False, "text": "Prometheus", "value": "Prometheus"},
                "hide": 2,
                "includeAll": False,
                "name": "DS_PROMETHEUS",
                "options": [],
                "query": "prometheus",
                "type": "datasource",
            }],
        },
        "time": {"from": "now-6h", "to": "now"},
        "timepicker": {},
        "timezone": "browser",
        "title": "Hedgehog — Command Center",
        "uid": "hedgehog-main",
        "version": 1,
    }

    out = DASHBOARD_DIR / "hedgehog.json"
    out.write_text(json.dumps(dashboard, indent=2) + "\n")
    print(f"  wrote {out}")


# ── Venue Dashboard Styler ─────────────────────────────────────────────────

def style_panel(panel):
    """Apply consistent styling transforms to a single panel."""
    panel["transparent"] = True

    ptype = panel.get("type", "")

    if ptype == "timeseries":
        custom = panel.get("fieldConfig", {}).get("defaults", {}).get("custom", {})
        custom["lineInterpolation"] = "smooth"
        custom["gradientMode"] = "opacity"
        custom["showPoints"] = custom.get("showPoints", "never")
        custom["spanNulls"] = True
        custom["axisBorderShow"] = False
        if "lineWidth" not in custom or custom["lineWidth"] < 2:
            custom["lineWidth"] = 2
        if "fillOpacity" not in custom or custom["fillOpacity"] < 10:
            custom["fillOpacity"] = 15
        panel.setdefault("fieldConfig", {}).setdefault("defaults", {})["custom"] = custom

    elif ptype == "bargauge":
        opts = panel.get("options", {})
        opts["displayMode"] = "lcd"
        opts["showUnfilled"] = True
        panel["options"] = opts
        # Use a modern continuous color
        fd = panel.get("fieldConfig", {}).get("defaults", {})
        if fd.get("color", {}).get("mode") == "continuous-GrYlRd":
            fd["color"]["mode"] = "continuous-BlYlRd"

    elif ptype == "gauge":
        opts = panel.get("options", {})
        opts["showThresholdLabels"] = True
        opts["showThresholdMarkers"] = True
        panel["options"] = opts

    elif ptype == "heatmap":
        opts = panel.get("options", {})
        color = opts.get("color", {})
        color["scheme"] = "Spectral"
        color["steps"] = 128
        opts["color"] = color
        opts["cellGap"] = 1
        opts.setdefault("cellRadius", 2)
        panel["options"] = opts

    elif ptype == "stat":
        # Ensure stat panels have sparklines where appropriate
        opts = panel.get("options", {})
        if opts.get("colorMode") == "background":
            pass  # keep background mode for status indicators
        # Update color values to our palette
        fd = panel.get("fieldConfig", {}).get("defaults", {})
        fc = fd.get("color", {})
        if fc.get("fixedColor") == "green" or fc.get("fixedColor") == "#10b981":
            fc["fixedColor"] = C["emerald"]
        elif fc.get("fixedColor") == "#f59e0b":
            fc["fixedColor"] = C["amber"]
        elif fc.get("fixedColor") == "#06b6d4":
            fc["fixedColor"] = C["cyan"]
        elif fc.get("fixedColor") == "#8b5cf6":
            fc["fixedColor"] = C["violet"]
        elif fc.get("fixedColor") == "#3b82f6":
            fc["fixedColor"] = C["blue"]

    elif ptype == "state-timeline":
        opts = panel.get("options", {})
        opts["rowHeight"] = 0.75
        panel["options"] = opts

    elif ptype == "table":
        opts = panel.get("options", {})
        opts["cellHeight"] = "sm"
        panel["options"] = opts

    return panel


def style_venue_dashboard(path):
    """Apply modern styling to a venue dashboard JSON file."""
    with open(path) as f:
        db = json.load(f)

    # Dashboard-level improvements
    db["graphTooltip"] = 2
    db["liveNow"] = True
    db["refresh"] = "10s"

    # Update link icons
    for link in db.get("links", []):
        if link.get("icon") == "exchange-alt":
            link["icon"] = "bolt"
        if link.get("title") == "Main Dashboard":
            link["icon"] = "home"

    # Style all panels (including nested in collapsed rows)
    for panel in db.get("panels", []):
        if panel.get("type") == "row":
            for sub in panel.get("panels", []):
                style_panel(sub)
        else:
            style_panel(panel)

    with open(path, "w") as f:
        json.dump(db, f, indent=2)
        f.write("\n")
    print(f"  styled {path.name}")


# ── Entry Point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Hedgehog Dashboard Styler")
    print("=" * 40)

    print("\nGenerating main dashboard...")
    generate_main_dashboard()

    print("\nStyling venue dashboards...")
    for vf in sorted(DASHBOARD_DIR.glob("venue_*.json")):
        style_venue_dashboard(vf)

    print("\nDone.")
