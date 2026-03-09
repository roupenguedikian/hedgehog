import { useState, useEffect, useCallback, useRef } from "react";
import { LineChart, Line, AreaChart, Area, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell, RadialBarChart, RadialBar } from "recharts";
import { Shield, Activity, TrendingUp, TrendingDown, Zap, AlertTriangle, CheckCircle, XCircle, ChevronRight, ArrowUpRight, ArrowDownRight, Pause, Play, BarChart3, Eye, Bot, Cpu, Wallet, Globe, Clock, RefreshCw } from "lucide-react";

// ═══════════════════════════════════════════════════════════════════
// MOCK DATA ENGINE — simulates live bot state
// ═══════════════════════════════════════════════════════════════════

const VENUES = ["hyperliquid","lighter","aster","drift","dydx","apex","paradex","ethereal","injective"];
const SYMBOLS = ["BTC","ETH","SOL","ARB","DOGE","AVAX","LINK","SUI","PEPE","WIF"];
const VENUE_CHAINS = { hyperliquid:"HL L1", lighter:"ETH ZK", aster:"BNB", drift:"Solana", dydx:"Cosmos", apex:"zkLink", paradex:"Starknet", ethereal:"Converge", injective:"Cosmos" };
const VENUE_CYCLES = { hyperliquid:1, lighter:1, drift:1, dydx:1, paradex:1, injective:1, aster:8, apex:8, ethereal:8 };

const rand = (min, max) => Math.random() * (max - min) + min;
const pick = arr => arr[Math.floor(Math.random() * arr.length)];

function generateFundingRates() {
  const m = {};
  SYMBOLS.forEach(s => {
    m[s] = {};
    VENUES.forEach(v => {
      if (Math.random() > 0.15) {
        const base = s === "BTC" ? 0.012 : s === "ETH" ? 0.015 : rand(0.005, 0.04);
        m[s][v] = (base + rand(-0.015, 0.02)) * (Math.random() > 0.2 ? 1 : -1);
      }
    });
  });
  return m;
}

function generateOpportunities(rates) {
  const opps = [];
  Object.entries(rates).forEach(([sym, venues]) => {
    const entries = Object.entries(venues).filter(([,r]) => r != null);
    if (entries.length < 2) return;
    entries.sort((a, b) => b[1] - a[1]);
    const [shortV, shortR] = entries[0];
    const [longV, longR] = entries[entries.length - 1];
    const spread = shortR - longR;
    if (spread > 0.03) {
      opps.push({ symbol: sym, shortVenue: shortV, longVenue: longV, shortRate: shortR, longRate: longR, spread, netYield: spread * 0.85, confidence: Math.min(0.95, spread * 10) });
    }
  });
  return opps.sort((a, b) => b.netYield - a.netYield).slice(0, 8);
}

function generatePositions() {
  return [
    { id: "hedge_a1b2c3", symbol: "BTC", shortVenue: "hyperliquid", longVenue: "drift", sizeUsd: 25000, entryBasis: 12.50, netPnl: 342.18, fundingCollected: 412.30, feesPaid: 45.12, gasPaid: 0.00, duration: "3d 14h", shortRate: 0.0245, longRate: 0.0082 },
    { id: "hedge_d4e5f6", symbol: "ETH", shortVenue: "paradex", longVenue: "dydx", sizeUsd: 15000, entryBasis: 3.20, netPnl: 128.44, fundingCollected: 198.60, feesPaid: 52.16, gasPaid: 2.00, duration: "2d 6h", shortRate: 0.0312, longRate: 0.0105 },
    { id: "hedge_g7h8i9", symbol: "SOL", shortVenue: "lighter", longVenue: "injective", sizeUsd: 10000, entryBasis: 0.45, netPnl: -18.22, fundingCollected: 64.80, feesPaid: 78.02, gasPaid: 0.50, duration: "18h", shortRate: 0.0189, longRate: 0.0142 },
  ];
}

function generateVenueScores() {
  return VENUES.map(v => ({
    venue: v, chain: VENUE_CHAINS[v], cycle: VENUE_CYCLES[v] + "h",
    score: rand(0.3, 0.95), avgRate: rand(0.005, 0.035), liquidity: rand(500000, 50000000),
    feeScore: v === "lighter" || v === "paradex" ? 1.0 : v === "hyperliquid" ? 0.55 : rand(0.3, 0.8),
    uptime: rand(0.92, 1.0), tier: v === "hyperliquid" || v === "dydx" || v === "drift" ? 1 : v === "ethereal" ? 3 : 2,
  })).sort((a, b) => b.score - a.score);
}

function generateNavHistory() {
  let nav = 50000;
  return Array.from({ length: 72 }, (_, i) => {
    nav += rand(-200, 350);
    return { hour: i, nav: Math.max(45000, nav), pnl: nav - 50000, time: `${72 - i}h ago` };
  }).reverse();
}

function generateAgentLog() {
  const actions = [
    { time: "2m ago", agent: "strategist", icon: "🧠", msg: "Identified BTC spread: Hyperliquid 2.45% vs Drift 0.82%. Recommending ENTER_HEDGE.", confidence: 0.87 },
    { time: "2m ago", agent: "risk", icon: "🛡️", msg: "APPROVED — within all parameters. Venue exposure 18.2%, margin util 42%.", confidence: 0.92 },
    { time: "2m ago", agent: "executor", icon: "⚡", msg: "Hedge opened: SHORT HL / LONG Drift. Slippage 3.2 bps. Fill: $25,000.", confidence: 1.0 },
    { time: "12m ago", agent: "strategist", icon: "🧠", msg: "SOL spread compressing on Lighter/Injective. Monitoring — no action yet.", confidence: 0.45 },
    { time: "35m ago", agent: "risk", icon: "🛡️", msg: "Oracle divergence check: 0.08% across venues. All clear.", confidence: 0.98 },
    { time: "1h ago", agent: "strategist", icon: "🧠", msg: "ETH rotation candidate: Paradex rate rising to 3.12%. Evaluating move from Aster.", confidence: 0.72 },
    { time: "1h ago", agent: "data", icon: "📊", msg: "Collected 78 rates from 9 venues. 5 opportunities above threshold.", confidence: 1.0 },
    { time: "2h ago", agent: "executor", icon: "⚡", msg: "ETH hedge opened: SHORT Paradex / LONG dYdX. Entry basis: $3.20.", confidence: 1.0 },
    { time: "3h ago", agent: "risk", icon: "🛡️", msg: "Gas check passed. All chains healthy. Bridge transit: 0% NAV.", confidence: 0.99 },
  ];
  return actions;
}

// ═══════════════════════════════════════════════════════════════════
// COMPONENTS
// ═══════════════════════════════════════════════════════════════════

const COLORS = {
  bg: "#0a0e17", bgCard: "#111827", bgCardHover: "#1a2236",
  border: "#1e293b", borderLight: "#334155",
  textPrimary: "#f1f5f9", textSecondary: "#94a3b8", textMuted: "#64748b",
  accent: "#f59e0b", accentDim: "#b45309",
  green: "#10b981", greenDim: "#065f46",
  red: "#ef4444", redDim: "#7f1d1d",
  blue: "#3b82f6", purple: "#8b5cf6", cyan: "#06b6d4",
};

function RateCell({ rate }) {
  if (rate == null) return <td style={{ padding: "6px 10px", background: "#0d1117", border: `1px solid ${COLORS.border}`, textAlign: "center", color: COLORS.textMuted, fontSize: 11 }}>—</td>;
  const pct = rate * 100;
  const intensity = Math.min(1, Math.abs(pct) / 4);
  const bg = pct >= 0
    ? `rgba(16,185,129,${intensity * 0.35})`
    : `rgba(239,68,68,${intensity * 0.35})`;
  const color = pct >= 0 ? COLORS.green : COLORS.red;
  return (
    <td style={{ padding: "6px 10px", background: bg, border: `1px solid ${COLORS.border}`, textAlign: "center", color, fontSize: 12, fontFamily: "'JetBrains Mono', monospace", fontWeight: Math.abs(pct) > 2 ? 700 : 400, transition: "all 0.3s" }}>
      {pct >= 0 ? "+" : ""}{pct.toFixed(2)}%
    </td>
  );
}

function StatusPill({ status, children }) {
  const styles = {
    active: { bg: "rgba(16,185,129,0.15)", color: COLORS.green, border: "rgba(16,185,129,0.3)" },
    warning: { bg: "rgba(245,158,11,0.15)", color: COLORS.accent, border: "rgba(245,158,11,0.3)" },
    danger: { bg: "rgba(239,68,68,0.15)", color: COLORS.red, border: "rgba(239,68,68,0.3)" },
    info: { bg: "rgba(59,130,246,0.15)", color: COLORS.blue, border: "rgba(59,130,246,0.3)" },
  };
  const s = styles[status] || styles.info;
  return <span style={{ display: "inline-flex", alignItems: "center", gap: 4, padding: "3px 10px", borderRadius: 999, background: s.bg, color: s.color, border: `1px solid ${s.border}`, fontSize: 11, fontWeight: 600, letterSpacing: "0.03em" }}>{children}</span>;
}

function MetricCard({ icon: Icon, label, value, sub, trend, color = COLORS.accent }) {
  return (
    <div style={{ background: COLORS.bgCard, border: `1px solid ${COLORS.border}`, borderRadius: 12, padding: "18px 20px", display: "flex", flexDirection: "column", gap: 8, transition: "border-color 0.2s", minWidth: 0 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <div style={{ width: 32, height: 32, borderRadius: 8, background: `${color}18`, display: "flex", alignItems: "center", justifyContent: "center" }}>
            <Icon size={16} color={color} />
          </div>
          <span style={{ color: COLORS.textSecondary, fontSize: 12, fontWeight: 500, textTransform: "uppercase", letterSpacing: "0.06em" }}>{label}</span>
        </div>
        {trend != null && (
          <span style={{ display: "flex", alignItems: "center", gap: 2, color: trend >= 0 ? COLORS.green : COLORS.red, fontSize: 12, fontWeight: 600 }}>
            {trend >= 0 ? <ArrowUpRight size={14} /> : <ArrowDownRight size={14} />}
            {Math.abs(trend).toFixed(1)}%
          </span>
        )}
      </div>
      <div style={{ fontSize: 26, fontWeight: 700, color: COLORS.textPrimary, fontFamily: "'JetBrains Mono', monospace", letterSpacing: "-0.02em" }}>{value}</div>
      {sub && <div style={{ fontSize: 12, color: COLORS.textMuted }}>{sub}</div>}
    </div>
  );
}

function TierBadge({ tier }) {
  const c = tier === 1 ? COLORS.green : tier === 2 ? COLORS.accent : COLORS.red;
  return <span style={{ fontSize: 10, fontWeight: 700, color: c, background: `${c}18`, padding: "2px 6px", borderRadius: 4, letterSpacing: "0.05em" }}>T{tier}</span>;
}

// ═══════════════════════════════════════════════════════════════════
// MAIN DASHBOARD
// ═══════════════════════════════════════════════════════════════════

export default function HedgeHogDashboard() {
  const [rates, setRates] = useState(generateFundingRates);
  const [positions, setPositions] = useState(generatePositions);
  const [opportunities, setOpportunities] = useState([]);
  const [venueScores, setVenueScores] = useState(generateVenueScores);
  const [navHistory, setNavHistory] = useState(generateNavHistory);
  const [agentLog, setAgentLog] = useState(generateAgentLog);
  const [cycle, setCycle] = useState(847);
  const [running, setRunning] = useState(true);
  const [activeTab, setActiveTab] = useState("overview");
  const [selectedSymbol, setSelectedSymbol] = useState(null);
  const timerRef = useRef(null);

  const refreshData = useCallback(() => {
    const newRates = generateFundingRates();
    setRates(newRates);
    setOpportunities(generateOpportunities(newRates));
    setVenueScores(generateVenueScores());
    setCycle(c => c + 1);
    setNavHistory(prev => {
      const last = prev[prev.length - 1];
      const newNav = last.nav + rand(-150, 300);
      return [...prev.slice(1), { hour: last.hour + 1, nav: newNav, pnl: newNav - 50000, time: "now" }];
    });
  }, []);

  useEffect(() => {
    refreshData();
    if (running) {
      timerRef.current = setInterval(refreshData, 8000);
    }
    return () => clearInterval(timerRef.current);
  }, [running, refreshData]);

  const totalNav = navHistory[navHistory.length - 1]?.nav || 50000;
  const totalPnl = positions.reduce((s, p) => s + p.netPnl, 0);
  const totalFunding = positions.reduce((s, p) => s + p.fundingCollected, 0);
  const healthyVenues = 7;

  const sectionStyle = { background: COLORS.bgCard, border: `1px solid ${COLORS.border}`, borderRadius: 14, overflow: "hidden" };
  const headerStyle = { padding: "14px 20px", borderBottom: `1px solid ${COLORS.border}`, display: "flex", alignItems: "center", justifyContent: "space-between" };
  const titleStyle = { fontSize: 13, fontWeight: 700, color: COLORS.textPrimary, textTransform: "uppercase", letterSpacing: "0.08em", display: "flex", alignItems: "center", gap: 8 };

  return (
    <div style={{ minHeight: "100vh", background: COLORS.bg, color: COLORS.textPrimary, fontFamily: "'DM Sans', -apple-system, sans-serif" }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap');
        * { box-sizing: border-box; margin: 0; padding: 0; }
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: ${COLORS.bg}; }
        ::-webkit-scrollbar-thumb { background: ${COLORS.border}; border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: ${COLORS.borderLight}; }
        @keyframes pulse-dot { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
        @keyframes slide-in { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: translateY(0); } }
        .card-hover:hover { border-color: ${COLORS.borderLight} !important; }
        table { border-collapse: collapse; width: 100%; }
      `}</style>

      {/* ── TOP BAR ──────────────────────────────────────────────── */}
      <header style={{ background: "linear-gradient(180deg, #111827 0%, #0a0e17 100%)", borderBottom: `1px solid ${COLORS.border}`, padding: "0 28px", height: 64, display: "flex", alignItems: "center", justifyContent: "space-between", position: "sticky", top: 0, zIndex: 50, backdropFilter: "blur(12px)" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
          <div style={{ fontSize: 28, lineHeight: 1 }}>🦔</div>
          <div>
            <div style={{ fontSize: 17, fontWeight: 700, letterSpacing: "-0.02em", color: COLORS.accent }}>HedgeHog</div>
            <div style={{ fontSize: 10, color: COLORS.textMuted, letterSpacing: "0.1em", textTransform: "uppercase" }}>DeFi Funding Rate Hedge Bot</div>
          </div>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 20 }}>
          <div style={{ display: "flex", alignItems: "center", gap: 6, color: COLORS.textSecondary, fontSize: 12 }}>
            <Clock size={13} />
            <span style={{ fontFamily: "'JetBrains Mono', monospace" }}>Cycle #{cycle}</span>
          </div>
          <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <div style={{ width: 7, height: 7, borderRadius: "50%", background: running ? COLORS.green : COLORS.red, animation: running ? "pulse-dot 2s infinite" : "none" }} />
            <span style={{ fontSize: 12, color: running ? COLORS.green : COLORS.red, fontWeight: 600 }}>{running ? "LIVE" : "PAUSED"}</span>
          </div>
          <StatusPill status="active"><Globe size={11} /> {healthyVenues}/9 Venues</StatusPill>
          <button onClick={() => setRunning(r => !r)} style={{ display: "flex", alignItems: "center", gap: 6, padding: "7px 14px", borderRadius: 8, border: `1px solid ${COLORS.border}`, background: COLORS.bgCard, color: COLORS.textSecondary, cursor: "pointer", fontSize: 12, fontWeight: 600 }}>
            {running ? <Pause size={13} /> : <Play size={13} />}
            {running ? "Pause" : "Resume"}
          </button>
        </div>
      </header>

      {/* ── NAV TABS ─────────────────────────────────────────────── */}
      <div style={{ padding: "0 28px", borderBottom: `1px solid ${COLORS.border}`, display: "flex", gap: 0, background: COLORS.bg }}>
        {[["overview", "Overview", Eye], ["positions", "Positions", BarChart3], ["venues", "Venues", Globe], ["agents", "Agents", Bot]].map(([key, label, Icon]) => (
          <button key={key} onClick={() => setActiveTab(key)} style={{
            padding: "12px 20px", display: "flex", alignItems: "center", gap: 7, border: "none", background: "transparent", cursor: "pointer",
            color: activeTab === key ? COLORS.accent : COLORS.textMuted, fontSize: 13, fontWeight: 600,
            borderBottom: activeTab === key ? `2px solid ${COLORS.accent}` : "2px solid transparent", transition: "all 0.2s",
          }}>
            <Icon size={14} /> {label}
          </button>
        ))}
      </div>

      <main style={{ padding: 28, maxWidth: 1600, margin: "0 auto" }}>

        {/* ── METRIC CARDS ────────────────────────────────────────── */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: 16, marginBottom: 24, animation: "slide-in 0.3s ease-out" }}>
          <MetricCard icon={Wallet} label="Portfolio NAV" value={`$${totalNav.toLocaleString("en", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`} sub={`${positions.length} active hedges`} trend={0.68} color={COLORS.accent} />
          <MetricCard icon={TrendingUp} label="Net P&L" value={`${totalPnl >= 0 ? "+" : ""}$${totalPnl.toFixed(2)}`} sub="Since inception" trend={totalPnl >= 0 ? 1.2 : -0.8} color={totalPnl >= 0 ? COLORS.green : COLORS.red} />
          <MetricCard icon={Zap} label="Funding Collected" value={`$${totalFunding.toFixed(2)}`} sub="Across all positions" color={COLORS.green} />
          <MetricCard icon={Activity} label="Best Spread" value={opportunities[0] ? `${(opportunities[0].spread * 100).toFixed(1)}%` : "—"} sub={opportunities[0] ? `${opportunities[0].symbol}: ${opportunities[0].shortVenue} → ${opportunities[0].longVenue}` : "Scanning..."} color={COLORS.cyan} />
          <MetricCard icon={Shield} label="Risk Status" value="HEALTHY" sub="Drawdown: 0.9% | Margin: 42%" color={COLORS.green} />
        </div>

        {/* ── PORTFOLIO CHART ─────────────────────────────────────── */}
        {activeTab === "overview" && (
          <div style={{ ...sectionStyle, marginBottom: 24 }} className="card-hover">
            <div style={headerStyle}>
              <div style={titleStyle}><TrendingUp size={15} color={COLORS.accent} /> Portfolio Performance</div>
              <div style={{ display: "flex", gap: 8 }}>
                {["24h", "7d", "30d", "All"].map(t => (
                  <button key={t} style={{ padding: "4px 12px", borderRadius: 6, border: `1px solid ${t === "7d" ? COLORS.accent : COLORS.border}`, background: t === "7d" ? `${COLORS.accent}15` : "transparent", color: t === "7d" ? COLORS.accent : COLORS.textMuted, fontSize: 11, fontWeight: 600, cursor: "pointer" }}>{t}</button>
                ))}
              </div>
            </div>
            <div style={{ padding: "12px 8px 8px" }}>
              <ResponsiveContainer width="100%" height={220}>
                <AreaChart data={navHistory}>
                  <defs>
                    <linearGradient id="navGrad" x1="0" y1="0" x2="0" y2="1">
                      <stop offset="0%" stopColor={COLORS.accent} stopOpacity={0.25} />
                      <stop offset="100%" stopColor={COLORS.accent} stopOpacity={0} />
                    </linearGradient>
                  </defs>
                  <CartesianGrid strokeDasharray="3 3" stroke={COLORS.border} />
                  <XAxis dataKey="time" tick={{ fill: COLORS.textMuted, fontSize: 10 }} axisLine={false} tickLine={false} interval={11} />
                  <YAxis tick={{ fill: COLORS.textMuted, fontSize: 10 }} axisLine={false} tickLine={false} tickFormatter={v => `$${(v/1000).toFixed(0)}k`} domain={["dataMin - 500", "dataMax + 500"]} />
                  <Tooltip contentStyle={{ background: COLORS.bgCard, border: `1px solid ${COLORS.border}`, borderRadius: 8, fontSize: 12, fontFamily: "'JetBrains Mono', monospace" }} labelStyle={{ color: COLORS.textMuted }} />
                  <Area type="monotone" dataKey="nav" stroke={COLORS.accent} strokeWidth={2} fill="url(#navGrad)" dot={false} />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}

        <div style={{ display: "grid", gridTemplateColumns: activeTab === "overview" ? "1fr 1fr" : "1fr", gap: 20, marginBottom: 24 }}>

          {/* ── FUNDING RATE HEATMAP ────────────────────────────────── */}
          {(activeTab === "overview" || activeTab === "venues") && (
            <div style={{ ...sectionStyle, gridColumn: activeTab === "venues" ? "1 / -1" : undefined }} className="card-hover">
              <div style={headerStyle}>
                <div style={titleStyle}><Activity size={15} color={COLORS.cyan} /> Funding Rate Heatmap</div>
                <div style={{ fontSize: 11, color: COLORS.textMuted }}>Annualized • Updated {Math.floor(rand(5,55))}s ago</div>
              </div>
              <div style={{ overflow: "auto", padding: 2 }}>
                <table>
                  <thead>
                    <tr>
                      <th style={{ padding: "10px 12px", textAlign: "left", fontSize: 11, color: COLORS.textMuted, fontWeight: 600, position: "sticky", left: 0, background: COLORS.bgCard, zIndex: 2, letterSpacing: "0.05em" }}>ASSET</th>
                      {VENUES.map(v => (
                        <th key={v} style={{ padding: "10px 8px", textAlign: "center", fontSize: 10, color: COLORS.textSecondary, fontWeight: 600, letterSpacing: "0.04em", textTransform: "uppercase", whiteSpace: "nowrap" }}>
                          {v.slice(0, 6)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {SYMBOLS.slice(0, activeTab === "venues" ? 10 : 6).map(sym => (
                      <tr key={sym} onClick={() => setSelectedSymbol(sym === selectedSymbol ? null : sym)} style={{ cursor: "pointer" }}>
                        <td style={{ padding: "8px 12px", fontWeight: 700, fontSize: 13, color: selectedSymbol === sym ? COLORS.accent : COLORS.textPrimary, position: "sticky", left: 0, background: COLORS.bgCard, zIndex: 1, borderBottom: `1px solid ${COLORS.border}` }}>
                          {sym}
                        </td>
                        {VENUES.map(v => <RateCell key={v} rate={rates[sym]?.[v]} />)}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div style={{ padding: "10px 16px", borderTop: `1px solid ${COLORS.border}`, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
                <div style={{ display: "flex", gap: 16, fontSize: 10, color: COLORS.textMuted }}>
                  <span><span style={{ display: "inline-block", width: 10, height: 10, borderRadius: 2, background: "rgba(16,185,129,0.5)", marginRight: 4 }} />Positive (shorts collect)</span>
                  <span><span style={{ display: "inline-block", width: 10, height: 10, borderRadius: 2, background: "rgba(239,68,68,0.5)", marginRight: 4 }} />Negative (longs collect)</span>
                </div>
                <span style={{ fontSize: 10, color: COLORS.textMuted }}>Click row to select</span>
              </div>
            </div>
          )}

          {/* ── OPPORTUNITIES ──────────────────────────────────────── */}
          {(activeTab === "overview" || activeTab === "positions") && (
            <div style={sectionStyle} className="card-hover">
              <div style={headerStyle}>
                <div style={titleStyle}><Zap size={15} color={COLORS.green} /> Top Opportunities</div>
                <StatusPill status="active">{opportunities.length} found</StatusPill>
              </div>
              <div style={{ maxHeight: activeTab === "positions" ? 500 : 380, overflow: "auto" }}>
                {opportunities.map((opp, i) => (
                  <div key={i} style={{ padding: "14px 20px", borderBottom: `1px solid ${COLORS.border}`, display: "flex", alignItems: "center", justifyContent: "space-between", animation: `slide-in 0.3s ease-out ${i * 0.05}s both` }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 14 }}>
                      <div style={{ width: 36, height: 36, borderRadius: 10, background: `${COLORS.accent}12`, display: "flex", alignItems: "center", justifyContent: "center", fontWeight: 800, fontSize: 13, color: COLORS.accent }}>{opp.symbol.slice(0, 3)}</div>
                      <div>
                        <div style={{ fontSize: 13, fontWeight: 600, color: COLORS.textPrimary }}>
                          <span style={{ color: COLORS.red }}>SHORT</span> {opp.shortVenue} → <span style={{ color: COLORS.green }}>LONG</span> {opp.longVenue}
                        </div>
                        <div style={{ fontSize: 11, color: COLORS.textMuted, marginTop: 2 }}>
                          {(opp.shortRate * 100).toFixed(2)}% vs {(opp.longRate * 100).toFixed(2)}% • Conf: {(opp.confidence * 100).toFixed(0)}%
                        </div>
                      </div>
                    </div>
                    <div style={{ textAlign: "right" }}>
                      <div style={{ fontSize: 16, fontWeight: 700, color: COLORS.green, fontFamily: "'JetBrains Mono', monospace" }}>+{(opp.netYield * 100).toFixed(1)}%</div>
                      <div style={{ fontSize: 10, color: COLORS.textMuted }}>net annual</div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        {/* ── ACTIVE POSITIONS ─────────────────────────────────────── */}
        {(activeTab === "overview" || activeTab === "positions") && (
          <div style={{ ...sectionStyle, marginBottom: 24 }} className="card-hover">
            <div style={headerStyle}>
              <div style={titleStyle}><BarChart3 size={15} color={COLORS.purple} /> Active Hedge Positions</div>
              <div style={{ fontSize: 12, color: COLORS.textMuted }}>{positions.length} positions</div>
            </div>
            <div style={{ overflow: "auto" }}>
              <table>
                <thead>
                  <tr style={{ borderBottom: `1px solid ${COLORS.border}` }}>
                    {["Pair", "Short Venue", "Long Venue", "Size", "Funding", "Fees + Gas", "Net P&L", "Duration", "Status"].map(h => (
                      <th key={h} style={{ padding: "12px 16px", textAlign: h === "Net P&L" || h === "Size" || h === "Funding" || h === "Fees + Gas" ? "right" : "left", fontSize: 11, color: COLORS.textMuted, fontWeight: 600, letterSpacing: "0.05em", textTransform: "uppercase", whiteSpace: "nowrap" }}>{h}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {positions.map(pos => (
                    <tr key={pos.id} style={{ borderBottom: `1px solid ${COLORS.border}`, transition: "background 0.15s" }}>
                      <td style={{ padding: "14px 16px" }}>
                        <div style={{ fontWeight: 700, fontSize: 14 }}>{pos.symbol}</div>
                        <div style={{ fontSize: 10, color: COLORS.textMuted, fontFamily: "'JetBrains Mono', monospace" }}>{pos.id}</div>
                      </td>
                      <td style={{ padding: "14px 16px" }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                          <span style={{ color: COLORS.red, fontSize: 10, fontWeight: 700 }}>SHORT</span>
                          <span style={{ fontWeight: 600, fontSize: 13 }}>{pos.shortVenue}</span>
                        </div>
                        <div style={{ fontSize: 11, color: COLORS.green, fontFamily: "'JetBrains Mono', monospace" }}>+{(pos.shortRate * 100).toFixed(2)}%</div>
                      </td>
                      <td style={{ padding: "14px 16px" }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
                          <span style={{ color: COLORS.green, fontSize: 10, fontWeight: 700 }}>LONG</span>
                          <span style={{ fontWeight: 600, fontSize: 13 }}>{pos.longVenue}</span>
                        </div>
                        <div style={{ fontSize: 11, color: COLORS.red, fontFamily: "'JetBrains Mono', monospace" }}>-{(pos.longRate * 100).toFixed(2)}%</div>
                      </td>
                      <td style={{ padding: "14px 16px", textAlign: "right", fontFamily: "'JetBrains Mono', monospace", fontSize: 13, fontWeight: 600 }}>${pos.sizeUsd.toLocaleString()}</td>
                      <td style={{ padding: "14px 16px", textAlign: "right", fontFamily: "'JetBrains Mono', monospace", fontSize: 13, color: COLORS.green, fontWeight: 600 }}>+${pos.fundingCollected.toFixed(2)}</td>
                      <td style={{ padding: "14px 16px", textAlign: "right", fontFamily: "'JetBrains Mono', monospace", fontSize: 12, color: COLORS.red }}>-${(pos.feesPaid + pos.gasPaid).toFixed(2)}</td>
                      <td style={{ padding: "14px 16px", textAlign: "right" }}>
                        <span style={{ fontFamily: "'JetBrains Mono', monospace", fontSize: 15, fontWeight: 700, color: pos.netPnl >= 0 ? COLORS.green : COLORS.red }}>
                          {pos.netPnl >= 0 ? "+" : ""}${pos.netPnl.toFixed(2)}
                        </span>
                      </td>
                      <td style={{ padding: "14px 16px", fontFamily: "'JetBrains Mono', monospace", fontSize: 12, color: COLORS.textSecondary }}>{pos.duration}</td>
                      <td style={{ padding: "14px 16px" }}><StatusPill status={pos.netPnl >= 0 ? "active" : "warning"}>{pos.netPnl >= 0 ? "Earning" : "Underwater"}</StatusPill></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}

        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 20, marginBottom: 24 }}>

          {/* ── VENUE LEADERBOARD ──────────────────────────────────── */}
          {(activeTab === "overview" || activeTab === "venues") && (
            <div style={sectionStyle} className="card-hover">
              <div style={headerStyle}>
                <div style={titleStyle}><Globe size={15} color={COLORS.blue} /> Venue Scores</div>
                <div style={{ fontSize: 11, color: COLORS.textMuted }}>Composite ranking</div>
              </div>
              <div style={{ maxHeight: 400, overflow: "auto" }}>
                {venueScores.map((vs, i) => (
                  <div key={vs.venue} style={{ padding: "12px 20px", borderBottom: `1px solid ${COLORS.border}`, display: "flex", alignItems: "center", gap: 14, animation: `slide-in 0.3s ease-out ${i * 0.04}s both` }}>
                    <div style={{ width: 28, height: 28, borderRadius: 8, background: i < 3 ? `${COLORS.accent}20` : `${COLORS.border}`, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 12, fontWeight: 800, color: i < 3 ? COLORS.accent : COLORS.textMuted }}>
                      {i + 1}
                    </div>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                        <span style={{ fontWeight: 700, fontSize: 13 }}>{vs.venue}</span>
                        <TierBadge tier={vs.tier} />
                        <span style={{ fontSize: 10, color: COLORS.textMuted, background: `${COLORS.border}`, padding: "1px 6px", borderRadius: 4 }}>{vs.chain}</span>
                      </div>
                      <div style={{ marginTop: 6, height: 4, borderRadius: 2, background: COLORS.border, overflow: "hidden" }}>
                        <div style={{ height: "100%", borderRadius: 2, width: `${vs.score * 100}%`, background: `linear-gradient(90deg, ${COLORS.accent}, ${COLORS.green})`, transition: "width 0.5s" }} />
                      </div>
                    </div>
                    <div style={{ textAlign: "right", minWidth: 60 }}>
                      <div style={{ fontSize: 16, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", color: COLORS.textPrimary }}>{vs.score.toFixed(3)}</div>
                      <div style={{ fontSize: 10, color: COLORS.textMuted }}>{vs.cycle} cycle</div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {/* ── AGENT ACTIVITY ─────────────────────────────────────── */}
          {(activeTab === "overview" || activeTab === "agents") && (
            <div style={sectionStyle} className="card-hover">
              <div style={headerStyle}>
                <div style={titleStyle}><Cpu size={15} color={COLORS.purple} /> Agent Activity</div>
                <StatusPill status="info"><RefreshCw size={11} /> Every 60s</StatusPill>
              </div>
              <div style={{ maxHeight: 400, overflow: "auto" }}>
                {agentLog.map((entry, i) => {
                  const agentColors = { strategist: COLORS.cyan, risk: COLORS.green, executor: COLORS.accent, data: COLORS.blue };
                  const c = agentColors[entry.agent] || COLORS.textMuted;
                  return (
                    <div key={i} style={{ padding: "12px 20px", borderBottom: `1px solid ${COLORS.border}`, display: "flex", gap: 12, animation: `slide-in 0.3s ease-out ${i * 0.04}s both` }}>
                      <div style={{ width: 32, height: 32, borderRadius: 8, background: `${c}15`, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 16, flexShrink: 0 }}>{entry.icon}</div>
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 3 }}>
                          <span style={{ fontSize: 11, fontWeight: 700, color: c, textTransform: "uppercase", letterSpacing: "0.05em" }}>{entry.agent}</span>
                          <span style={{ fontSize: 10, color: COLORS.textMuted }}>{entry.time}</span>
                          {entry.confidence > 0.8 && <CheckCircle size={11} color={COLORS.green} />}
                        </div>
                        <div style={{ fontSize: 12, color: COLORS.textSecondary, lineHeight: 1.5 }}>{entry.msg}</div>
                      </div>
                      <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", minWidth: 40 }}>
                        <div style={{ width: 28, height: 28, borderRadius: "50%", background: COLORS.bg, border: `2px solid ${c}`, display: "flex", alignItems: "center", justifyContent: "center" }}>
                          <span style={{ fontSize: 9, fontWeight: 800, color: c }}>{(entry.confidence * 100).toFixed(0)}</span>
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>

        {/* ── RISK DASHBOARD ───────────────────────────────────────── */}
        {(activeTab === "overview" || activeTab === "agents") && (
          <div style={{ ...sectionStyle, marginBottom: 24 }} className="card-hover">
            <div style={headerStyle}>
              <div style={titleStyle}><Shield size={15} color={COLORS.green} /> Risk Dashboard</div>
              <StatusPill status="active"><CheckCircle size={11} /> All Systems Nominal</StatusPill>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(200px, 1fr))", gap: 1, background: COLORS.border }}>
              {[
                { label: "Drawdown", value: "0.92%", max: "5.00%", pct: 18.4, color: COLORS.green },
                { label: "Max Venue Exposure", value: "18.2%", max: "20.0%", pct: 91, color: COLORS.accent },
                { label: "Chain Concentration", value: "24.1%", max: "35.0%", pct: 68.8, color: COLORS.blue },
                { label: "Margin Utilization", value: "42.0%", max: "60.0%", pct: 70, color: COLORS.cyan },
                { label: "Oracle Divergence", value: "0.08%", max: "0.50%", pct: 16, color: COLORS.green },
                { label: "Bridge In-Transit", value: "0.0%", max: "10.0%", pct: 0, color: COLORS.green },
              ].map(r => (
                <div key={r.label} style={{ padding: "16px 20px", background: COLORS.bgCard }}>
                  <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
                    <span style={{ fontSize: 11, color: COLORS.textMuted, fontWeight: 600, textTransform: "uppercase", letterSpacing: "0.04em" }}>{r.label}</span>
                    <span style={{ fontSize: 11, color: COLORS.textMuted }}>{r.max} max</span>
                  </div>
                  <div style={{ fontSize: 22, fontWeight: 700, fontFamily: "'JetBrains Mono', monospace", color: r.pct > 85 ? COLORS.red : r.pct > 65 ? COLORS.accent : COLORS.green, marginBottom: 8 }}>
                    {r.value}
                  </div>
                  <div style={{ height: 4, borderRadius: 2, background: COLORS.border }}>
                    <div style={{ height: "100%", borderRadius: 2, width: `${r.pct}%`, background: r.pct > 85 ? COLORS.red : r.pct > 65 ? COLORS.accent : r.color, transition: "width 0.5s" }} />
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ── FOOTER ──────────────────────────────────────────────── */}
        <footer style={{ textAlign: "center", padding: "24px 0 12px", color: COLORS.textMuted, fontSize: 11, letterSpacing: "0.04em" }}>
          🦔 HedgeHog v1.0 • DeFi Funding Rate Hedge Bot • 9 Venues • {SYMBOLS.length} Assets • Powered by Claude AI
        </footer>
      </main>
    </div>
  );
}
