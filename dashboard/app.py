"""
dashboard/app.py — Narrative Memory Agent Dashboard
FastAPI backend serving the dashboard and data endpoints.
"""

import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Narrative Memory Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH    = Path("data/memory.db")
STATE_PATH = Path("data/agent_state.json")
CONFIG_PATH = Path("data/strategy_config.json")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.get("/api/state")
def get_state():
    if not STATE_PATH.exists():
        return {"cycle_count": 0, "active_narrative": None, "last_run": None}
    return json.loads(STATE_PATH.read_text())


@app.get("/api/narratives")
def get_narratives():
    if not DB_PATH.exists():
        return []
    conn = db()
    rows = conn.execute(
        "SELECT * FROM narrative_memory ORDER BY first_detected DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/trades")
def get_trades():
    if not DB_PATH.exists():
        return []
    conn = db()
    rows = conn.execute(
        "SELECT * FROM trade_log ORDER BY created_at DESC LIMIT 50"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.get("/api/config")
def get_config():
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text())


@app.get("/api/stats")
def get_stats():
    if not DB_PATH.exists():
        return {}
    conn = db()
    total_trades  = conn.execute("SELECT COUNT(*) FROM trade_log").fetchone()[0]
    open_trades   = conn.execute("SELECT COUNT(*) FROM trade_log WHERE status='open'").fetchone()[0]
    closed_trades = conn.execute("SELECT COUNT(*) FROM trade_log WHERE status='closed'").fetchone()[0]
    wins = conn.execute(
        "SELECT COUNT(*) FROM trade_log WHERE status='closed' AND pnl_pct > 0"
    ).fetchone()[0]
    avg_pnl_row = conn.execute(
        "SELECT AVG(pnl_pct) FROM trade_log WHERE status='closed' AND pnl_pct IS NOT NULL"
    ).fetchone()[0]
    narratives = conn.execute("SELECT COUNT(*) FROM narrative_memory").fetchone()[0]
    conn.close()
    return {
        "total_trades":  total_trades,
        "open_trades":   open_trades,
        "closed_trades": closed_trades,
        "win_rate": round(wins / closed_trades * 100, 1) if closed_trades else 0,
        "avg_pnl": round(avg_pnl_row, 2) if avg_pnl_row else 0,
        "narratives_in_memory": narratives,
    }


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(HTML)


# ── HTML Dashboard ─────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Narrative Memory Agent</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:        #080b10;
    --bg2:       #0d1117;
    --bg3:       #111820;
    --border:    rgba(99,179,237,0.12);
    --border2:   rgba(99,179,237,0.06);
    --accent:    #38bdf8;
    --accent2:   #0ea5e9;
    --green:     #34d399;
    --red:       #f87171;
    --amber:     #fbbf24;
    --text:      #e2e8f0;
    --text2:     #94a3b8;
    --text3:     #475569;
    --mono:      'JetBrains Mono', monospace;
    --sans:      'Space Grotesk', sans-serif;
  }

  html { font-size: 16px; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    min-height: 100dvh;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }

  /* ── Scanline overlay ── */
  body::before {
    content: '';
    position: fixed; inset: 0; z-index: 0; pointer-events: none;
    background: repeating-linear-gradient(
      0deg,
      transparent,
      transparent 2px,
      rgba(0,0,0,0.03) 2px,
      rgba(0,0,0,0.03) 4px
    );
  }

  /* ── Layout ── */
  .shell {
    position: relative; z-index: 1;
    max-width: 1400px; margin: 0 auto;
    padding: 0 24px 48px;
  }

  /* ── Header ── */
  header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 24px 0 32px;
    border-bottom: 1px solid var(--border2);
    margin-bottom: 32px;
  }
  .logo {
    display: flex; align-items: center; gap: 12px;
  }
  .logo-icon {
    width: 36px; height: 36px; border-radius: 10px;
    background: linear-gradient(135deg, var(--accent2), #0369a1);
    display: flex; align-items: center; justify-content: center;
    box-shadow: 0 0 20px rgba(56,189,248,0.25);
  }
  .logo-icon svg { width: 18px; height: 18px; }
  .logo-text { font-size: 1rem; font-weight: 600; letter-spacing: -0.01em; }
  .logo-sub  { font-size: 0.72rem; color: var(--text3); font-family: var(--mono); margin-top: 1px; }

  .header-right { display: flex; align-items: center; gap: 16px; }
  .live-badge {
    display: flex; align-items: center; gap: 6px;
    font-family: var(--mono); font-size: 0.7rem;
    color: var(--green); letter-spacing: 0.08em;
  }
  .live-dot {
    width: 6px; height: 6px; border-radius: 50%;
    background: var(--green);
    box-shadow: 0 0 6px var(--green);
    animation: pulse 2s ease-in-out infinite;
  }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.4; } }

  .cycle-badge {
    font-family: var(--mono); font-size: 0.7rem;
    color: var(--text3); letter-spacing: 0.05em;
  }

  /* ── Stats row ── */
  .stats-row {
    display: grid;
    grid-template-columns: repeat(6, 1fr);
    gap: 12px;
    margin-bottom: 28px;
  }
  @media (max-width: 1024px) { .stats-row { grid-template-columns: repeat(3, 1fr); } }
  @media (max-width: 640px)  { .stats-row { grid-template-columns: repeat(2, 1fr); } }

  .stat-card {
    background: var(--bg2);
    border: 1px solid var(--border2);
    border-radius: 14px;
    padding: 16px 18px;
    transition: border-color 200ms cubic-bezier(0.32,0.72,0,1);
  }
  .stat-card:hover { border-color: var(--border); }
  .stat-label {
    font-family: var(--mono); font-size: 0.65rem;
    color: var(--text3); letter-spacing: 0.1em;
    text-transform: uppercase; margin-bottom: 8px;
  }
  .stat-value {
    font-size: 1.6rem; font-weight: 700;
    letter-spacing: -0.03em; line-height: 1;
  }
  .stat-value.green { color: var(--green); }
  .stat-value.red   { color: var(--red); }
  .stat-value.blue  { color: var(--accent); }
  .stat-value.amber { color: var(--amber); }

  /* ── Agent state bar ── */
  .state-bar {
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 16px 24px;
    margin-bottom: 28px;
    display: flex; align-items: center; gap: 24px;
    flex-wrap: wrap;
  }
  .state-item { display: flex; flex-direction: column; gap: 3px; }
  .state-key  { font-family: var(--mono); font-size: 0.62rem; color: var(--text3); letter-spacing: 0.1em; text-transform: uppercase; }
  .state-val  { font-family: var(--mono); font-size: 0.82rem; color: var(--accent); }
  .state-divider { width: 1px; height: 32px; background: var(--border2); flex-shrink: 0; }

  /* ── Grid ── */
  .grid-2 {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
    margin-bottom: 20px;
  }
  @media (max-width: 900px) { .grid-2 { grid-template-columns: 1fr; } }

  .grid-3 {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 20px;
  }
  @media (max-width: 1100px) { .grid-3 { grid-template-columns: 1fr 1fr; } }
  @media (max-width: 700px)  { .grid-3 { grid-template-columns: 1fr; } }

  /* ── Panel ── */
  .panel {
    background: var(--bg2);
    border: 1px solid var(--border2);
    border-radius: 16px;
    overflow: hidden;
    transition: border-color 200ms cubic-bezier(0.32,0.72,0,1);
  }
  .panel:hover { border-color: var(--border); }
  .panel-header {
    display: flex; align-items: center; justify-content: space-between;
    padding: 16px 20px;
    border-bottom: 1px solid var(--border2);
  }
  .panel-title {
    font-family: var(--mono); font-size: 0.7rem;
    color: var(--text2); letter-spacing: 0.1em;
    text-transform: uppercase; display: flex; align-items: center; gap: 8px;
  }
  .panel-count {
    background: var(--bg3); border: 1px solid var(--border2);
    border-radius: 20px; padding: 2px 8px;
    font-size: 0.65rem; color: var(--text3);
  }
  .panel-body { padding: 0; }

  /* ── Table ── */
  table { width: 100%; border-collapse: collapse; }
  th {
    font-family: var(--mono); font-size: 0.62rem;
    color: var(--text3); letter-spacing: 0.08em;
    text-transform: uppercase; text-align: left;
    padding: 10px 16px; font-weight: 500;
    border-bottom: 1px solid var(--border2);
    background: var(--bg3);
  }
  td {
    padding: 11px 16px;
    font-size: 0.82rem; color: var(--text2);
    border-bottom: 1px solid var(--border2);
    font-family: var(--mono);
    transition: background 150ms;
  }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(56,189,248,0.03); }

  /* ── Tags / Badges ── */
  .tag {
    display: inline-block; padding: 2px 8px;
    border-radius: 4px; font-size: 0.65rem;
    font-family: var(--mono); letter-spacing: 0.05em;
    font-weight: 500;
  }
  .tag-green  { background: rgba(52,211,153,0.12); color: var(--green); border: 1px solid rgba(52,211,153,0.2); }
  .tag-red    { background: rgba(248,113,113,0.12); color: var(--red);   border: 1px solid rgba(248,113,113,0.2); }
  .tag-blue   { background: rgba(56,189,248,0.12);  color: var(--accent); border: 1px solid rgba(56,189,248,0.2); }
  .tag-amber  { background: rgba(251,191,36,0.12);  color: var(--amber);  border: 1px solid rgba(251,191,36,0.2); }
  .tag-gray   { background: rgba(71,85,105,0.3);    color: var(--text3);  border: 1px solid var(--border2); }

  /* ── Confidence bar ── */
  .conf-bar-wrap { display: flex; align-items: center; gap: 10px; }
  .conf-bar-bg {
    flex: 1; height: 4px; border-radius: 2px;
    background: var(--bg3); overflow: hidden;
    min-width: 60px;
  }
  .conf-bar-fill {
    height: 100%; border-radius: 2px;
    background: linear-gradient(90deg, var(--accent2), var(--accent));
    transition: width 600ms cubic-bezier(0.32,0.72,0,1);
  }
  .conf-val { font-size: 0.7rem; color: var(--text3); min-width: 30px; text-align: right; }

  /* ── PnL ── */
  .pnl-pos { color: var(--green); }
  .pnl-neg { color: var(--red); }
  .pnl-neu { color: var(--text3); }

  /* ── Rules panel ── */
  .rule-row {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px 20px;
    border-bottom: 1px solid var(--border2);
    transition: background 150ms;
  }
  .rule-row:last-child { border-bottom: none; }
  .rule-row:hover { background: rgba(56,189,248,0.03); }
  .rule-name { font-family: var(--mono); font-size: 0.78rem; color: var(--text); }
  .rule-stats { display: flex; align-items: center; gap: 16px; }
  .rule-stat  { display: flex; flex-direction: column; align-items: flex-end; gap: 2px; }
  .rule-stat-label { font-size: 0.58rem; color: var(--text3); font-family: var(--mono); letter-spacing: 0.08em; }
  .rule-stat-val   { font-size: 0.82rem; font-family: var(--mono); }

  /* ── Empty state ── */
  .empty {
    display: flex; flex-direction: column; align-items: center;
    justify-content: center; padding: 48px 24px;
    color: var(--text3); gap: 8px;
  }
  .empty-icon { font-size: 1.5rem; opacity: 0.3; margin-bottom: 4px; }
  .empty-text { font-family: var(--mono); font-size: 0.72rem; letter-spacing: 0.06em; }

  /* ── Skeleton loader ── */
  .skeleton {
    background: linear-gradient(90deg, var(--bg3) 25%, var(--bg2) 50%, var(--bg3) 75%);
    background-size: 200% 100%;
    animation: shimmer 1.5s infinite;
    border-radius: 4px;
  }
  @keyframes shimmer { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }

  /* ── Refresh indicator ── */
  .refresh-bar {
    position: fixed; bottom: 0; left: 0; right: 0; height: 2px;
    background: var(--border2); z-index: 100;
  }
  .refresh-fill {
    height: 100%;
    background: linear-gradient(90deg, var(--accent2), var(--accent));
    transition: width 1s linear;
  }

  /* ── Scrollable table wrapper ── */
  .table-wrap { overflow-x: auto; }

  /* ── Footer ── */
  .footer {
    margin-top: 48px; padding-top: 24px;
    border-top: 1px solid var(--border2);
    display: flex; align-items: center; justify-content: space-between;
    flex-wrap: wrap; gap: 12px;
  }
  .footer-text { font-family: var(--mono); font-size: 0.65rem; color: var(--text3); letter-spacing: 0.06em; }

  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation-duration: 0.01ms !important; transition-duration: 0.01ms !important; }
  }
</style>
</head>
<body>

<div class="shell">

  <!-- Header -->
  <header>
    <div class="logo">
      <div class="logo-icon">
        <svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 2L2 7l10 5 10-5-10-5z"/>
          <path d="M2 17l10 5 10-5"/>
          <path d="M2 12l10 5 10-5"/>
        </svg>
      </div>
      <div>
        <div class="logo-text">Narrative Memory Agent</div>
        <div class="logo-sub">BITGET AI HACKATHON S1 // TRACK 1</div>
      </div>
    </div>
    <div class="header-right">
      <div class="cycle-badge" id="last-run">--</div>
      <div class="live-badge">
        <div class="live-dot"></div>
        LIVE
      </div>
    </div>
  </header>

  <!-- Stats Row -->
  <div class="stats-row" id="stats-row">
    <div class="stat-card"><div class="stat-label">Total Trades</div><div class="stat-value blue" id="s-total">--</div></div>
    <div class="stat-card"><div class="stat-label">Open</div><div class="stat-value amber" id="s-open">--</div></div>
    <div class="stat-card"><div class="stat-label">Closed</div><div class="stat-value" id="s-closed">--</div></div>
    <div class="stat-card"><div class="stat-label">Win Rate</div><div class="stat-value green" id="s-winrate">--</div></div>
    <div class="stat-card"><div class="stat-label">Avg PnL</div><div class="stat-value" id="s-avgpnl">--</div></div>
    <div class="stat-card"><div class="stat-label">Memory Records</div><div class="stat-value blue" id="s-narratives">--</div></div>
  </div>

  <!-- Agent State Bar -->
  <div class="state-bar" id="state-bar">
    <div class="state-item">
      <div class="state-key">Cycle</div>
      <div class="state-val" id="st-cycle">--</div>
    </div>
    <div class="state-divider"></div>
    <div class="state-item">
      <div class="state-key">Active Narrative</div>
      <div class="state-val" id="st-narrative">none</div>
    </div>
    <div class="state-divider"></div>
    <div class="state-item">
      <div class="state-key">Narrative Day</div>
      <div class="state-val" id="st-narday">--</div>
    </div>
    <div class="state-divider"></div>
    <div class="state-item">
      <div class="state-key">Entry Status</div>
      <div class="state-val" id="st-entry">--</div>
    </div>
    <div class="state-divider"></div>
    <div class="state-item">
      <div class="state-key">Open Positions</div>
      <div class="state-val" id="st-open">--</div>
    </div>
    <div class="state-divider"></div>
    <div class="state-item">
      <div class="state-key">Last Detections</div>
      <div class="state-val" id="st-detections">--</div>
    </div>
  </div>

  <!-- Top Row: Trade Log + Memory -->
  <div class="grid-2" style="margin-bottom:20px;">

    <!-- Trade Log -->
    <div class="panel">
      <div class="panel-header">
        <div class="panel-title">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
          Trade Log
          <span class="panel-count" id="trade-count">0</span>
        </div>
      </div>
      <div class="panel-body table-wrap">
        <table>
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Side</th>
              <th>Narrative</th>
              <th>Entry</th>
              <th>Exit</th>
              <th>PnL</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody id="trade-body">
            <tr><td colspan="7"><div class="empty"><div class="empty-text">Loading trades...</div></div></td></tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Narrative Memory -->
    <div class="panel">
      <div class="panel-header">
        <div class="panel-title">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
          Narrative Memory
          <span class="panel-count" id="memory-count">0</span>
        </div>
      </div>
      <div class="panel-body table-wrap">
        <table>
          <thead>
            <tr>
              <th>Narrative</th>
              <th>Avg Return</th>
              <th>Days/Peak</th>
              <th>Entry Day</th>
              <th>Outcome</th>
            </tr>
          </thead>
          <tbody id="memory-body">
            <tr><td colspan="5"><div class="empty"><div class="empty-text">Loading memory...</div></div></td></tr>
          </tbody>
        </table>
      </div>
    </div>

  </div>

  <!-- Bottom Row: Fallback Rules -->
  <div class="panel" style="margin-bottom:20px;">
    <div class="panel-header">
      <div class="panel-title">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14M4.93 4.93a10 10 0 0 0 0 14.14"/></svg>
        Fallback Strategy — Self-Learning Rules
      </div>
    </div>
    <div class="panel-body" id="rules-body">
      <div class="empty"><div class="empty-text">Loading rules...</div></div>
    </div>
  </div>

</div><!-- /shell -->

<!-- Refresh bar -->
<div class="refresh-bar"><div class="refresh-fill" id="refresh-fill" style="width:100%"></div></div>

<script>
  const REFRESH_INTERVAL = 30000; // 30 seconds
  let refreshTimer = null;
  let fillTimer = null;

  // ── Fetch helpers ──────────────────────────────────────────

  async function fetchJSON(url) {
    try {
      const r = await fetch(url);
      if (!r.ok) throw new Error(r.status);
      return await r.json();
    } catch(e) {
      console.error('Fetch error:', url, e);
      return null;
    }
  }

  // ── Formatters ─────────────────────────────────────────────

  function fmtDate(iso) {
    if (!iso) return '--';
    const d = new Date(iso);
    return d.toLocaleDateString('en-GB', { month:'short', day:'numeric' })
      + ' ' + d.toLocaleTimeString('en-GB', { hour:'2-digit', minute:'2-digit' });
  }

  function fmtPnl(v) {
    if (v === null || v === undefined) return '<span class="pnl-neu">--</span>';
    const n = parseFloat(v);
    const cls = n > 0 ? 'pnl-pos' : n < 0 ? 'pnl-neg' : 'pnl-neu';
    const sign = n > 0 ? '+' : '';
    return `<span class="${cls}">${sign}${n.toFixed(2)}%</span>`;
  }

  function outcomeTag(o) {
    const map = {
      played_out: 'tag-green',
      fizzled:    'tag-red',
      running:    'tag-amber',
      unknown:    'tag-gray',
      stopped_out:'tag-red',
    };
    return `<span class="tag ${map[o] || 'tag-gray'}">${(o||'--').replace('_',' ')}</span>`;
  }

  function statusTag(s) {
    return s === 'open'
      ? '<span class="tag tag-amber">open</span>'
      : '<span class="tag tag-gray">closed</span>';
  }

  function sideTag(s) {
    return s === 'long'
      ? '<span class="tag tag-green">long</span>'
      : '<span class="tag tag-red">short</span>';
  }

  function narrativeLabel(tag) {
    return (tag || '--').replace(/_/g, ' ');
  }

  // ── Renderers ──────────────────────────────────────────────

  function renderStats(stats) {
    if (!stats) return;
    document.getElementById('s-total').textContent    = stats.total_trades ?? '--';
    document.getElementById('s-open').textContent     = stats.open_trades  ?? '--';
    document.getElementById('s-closed').textContent   = stats.closed_trades ?? '--';
    document.getElementById('s-winrate').textContent  = stats.closed_trades ? stats.win_rate + '%' : '--';
    const avgEl = document.getElementById('s-avgpnl');
    avgEl.textContent = stats.closed_trades ? (stats.avg_pnl > 0 ? '+' : '') + stats.avg_pnl + '%' : '--';
    avgEl.className = 'stat-value ' + (stats.avg_pnl > 0 ? 'green' : stats.avg_pnl < 0 ? 'red' : '');
    document.getElementById('s-narratives').textContent = stats.narratives_in_memory ?? '--';
  }

  function renderState(state) {
    if (!state) return;
    document.getElementById('st-cycle').textContent     = state.cycle_count ?? '--';
    document.getElementById('st-narrative').textContent = state.active_narrative
      ? narrativeLabel(state.active_narrative) : 'none';
    document.getElementById('st-narday').textContent    = state.active_narrative_day ?? '--';
    document.getElementById('st-entry').textContent     = state.waiting_to_enter
      ? 'waiting ' + state.days_to_wait + 'd' : 'ready';
    document.getElementById('st-open').textContent      = state.open_trades ?? '--';
    const dets = state.last_detections;
    document.getElementById('st-detections').textContent = (dets && dets.length)
      ? dets.join(', ') : 'none';

    if (state.last_run) {
      document.getElementById('last-run').textContent = 'Last run: ' + fmtDate(state.last_run);
    }
  }

  function renderTrades(trades) {
    const tbody = document.getElementById('trade-body');
    document.getElementById('trade-count').textContent = trades ? trades.length : 0;
    if (!trades || !trades.length) {
      tbody.innerHTML = '<tr><td colspan="7"><div class="empty"><div class="empty-text">No trades yet</div></div></td></tr>';
      return;
    }
    tbody.innerHTML = trades.map(t => `
      <tr>
        <td style="color:var(--text);font-weight:500;">${t.symbol}</td>
        <td>${sideTag(t.side)}</td>
        <td style="color:var(--text3);font-size:0.72rem;">${narrativeLabel(t.narrative_tag)}</td>
        <td>${t.entry_price ?? '--'}</td>
        <td>${t.exit_price ?? '--'}</td>
        <td>${fmtPnl(t.pnl_pct)}</td>
        <td>${statusTag(t.status)}</td>
      </tr>
    `).join('');
  }

  function renderMemory(narratives) {
    const tbody = document.getElementById('memory-body');
    document.getElementById('memory-count').textContent = narratives ? narratives.length : 0;
    if (!narratives || !narratives.length) {
      tbody.innerHTML = '<tr><td colspan="5"><div class="empty"><div class="empty-text">No memory records</div></div></td></tr>';
      return;
    }
    tbody.innerHTML = narratives.map(n => `
      <tr>
        <td style="color:var(--text);font-weight:500;">${narrativeLabel(n.narrative_tag)}</td>
        <td class="${n.avg_return_pct > 0 ? 'pnl-pos' : 'pnl-neu'}">
          ${n.avg_return_pct != null ? (n.avg_return_pct > 0 ? '+' : '') + n.avg_return_pct.toFixed(1) + '%' : '--'}
        </td>
        <td>${n.days_to_peak ?? '--'}d</td>
        <td>day ${n.optimal_entry_day ?? '--'}</td>
        <td>${outcomeTag(n.outcome)}</td>
      </tr>
    `).join('');
  }

  function renderRules(config) {
    const el = document.getElementById('rules-body');
    if (!config || !config.rules) {
      el.innerHTML = '<div class="empty"><div class="empty-text">No config data</div></div>';
      return;
    }
    const rules = Object.entries(config.rules);
    el.innerHTML = rules.map(([name, r]) => {
      const wr = r.win_rate ?? 0;
      const wrPct = (wr * 100).toFixed(1);
      const wrColor = wr > 0.55 ? 'var(--green)' : wr < 0.40 ? 'var(--red)' : 'var(--amber)';
      const enabled = r.enabled !== false;
      return `
        <div class="rule-row">
          <div>
            <div class="rule-name">${name.replace(/_/g,' ')}</div>
            <div style="font-family:var(--mono);font-size:0.65rem;color:var(--text3);margin-top:3px;">
              ${getRuleThreshold(name, r)}
            </div>
          </div>
          <div class="rule-stats">
            <div class="rule-stat">
              <div class="rule-stat-label">Trades</div>
              <div class="rule-stat-val" style="color:var(--text)">${r.trades ?? 0}</div>
            </div>
            <div class="rule-stat">
              <div class="rule-stat-label">W / L</div>
              <div class="rule-stat-val" style="color:var(--text)">${r.wins ?? 0} / ${r.losses ?? 0}</div>
            </div>
            <div class="rule-stat">
              <div class="rule-stat-label">Win Rate</div>
              <div class="rule-stat-val" style="color:${wrColor}">${r.trades ? wrPct + '%' : '--'}</div>
            </div>
            <div class="rule-stat">
              <div class="rule-stat-label">TP / SL</div>
              <div class="rule-stat-val" style="color:var(--text2)">+${r.take_profit_pct}% / -${r.stop_loss_pct}%</div>
            </div>
            <div class="rule-stat">
              <div class="rule-stat-label">Status</div>
              <div>${enabled ? '<span class="tag tag-green">on</span>' : '<span class="tag tag-gray">off</span>'}</div>
            </div>
          </div>
        </div>
      `;
    }).join('');
  }

  function getRuleThreshold(name, r) {
    if (name === 'momentum_long')
      return `min change: +${r.min_change_24h_pct}%  max: +${r.max_change_24h_pct}%  vol: $${(r.min_volume_usd/1e6).toFixed(0)}M`;
    if (name === 'fear_bounce')
      return `max drop: ${r.max_change_24h_pct}%  F&G threshold: <${r.max_fear_greed}  vol: $${(r.min_volume_usd/1e6).toFixed(0)}M`;
    if (name === 'volume_breakout')
      return `min change: +${r.min_change_24h_pct}%  volume: ${r.volume_vs_avg_multiplier}x avg  vol: $${(r.min_volume_usd/1e6).toFixed(0)}M`;
    return '';
  }

  // ── Refresh ────────────────────────────────────────────────

  function startRefreshBar() {
    const fill = document.getElementById('refresh-fill');
    fill.style.transition = 'none';
    fill.style.width = '100%';
    setTimeout(() => {
      fill.style.transition = `width ${REFRESH_INTERVAL}ms linear`;
      fill.style.width = '0%';
    }, 50);
  }

  async function loadAll() {
    const [state, stats, trades, narratives, config] = await Promise.all([
      fetchJSON('/api/state'),
      fetchJSON('/api/stats'),
      fetchJSON('/api/trades'),
      fetchJSON('/api/narratives'),
      fetchJSON('/api/config'),
    ]);
    renderState(state);
    renderStats(stats);
    renderTrades(trades);
    renderMemory(narratives);
    renderRules(config);
    startRefreshBar();
  }

  // Initial load
  loadAll();

  // Auto-refresh
  setInterval(loadAll, REFRESH_INTERVAL);
</script>
</body>
</html>"""