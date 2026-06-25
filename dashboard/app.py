"""
dashboard/app.py — Narrative Memory Agent Dashboard v2
E8 Markets-inspired prop trading dashboard aesthetic
"""

import json
import os
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Narrative Memory Agent")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", PROJECT_ROOT / "data"))
LOG_DIR = Path(os.getenv("LOG_DIR", PROJECT_ROOT / "logs"))
DB_PATH     = DATA_DIR / "memory.db"
STATE_PATH  = DATA_DIR / "agent_state.json"
CONFIG_PATH = DATA_DIR / "strategy_config.json"
LOG_PATH    = LOG_DIR / "agent.log"
STARTING_BALANCE = 10_000.0
SIZE_ALLOCATION  = {"small": 0.05, "medium": 0.10, "full": 0.15, "none": 0.0}


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


@app.middleware("http")
async def disable_dynamic_cache(request, call_next):
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


def ensure_portfolio(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS portfolio (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER, balance_before REAL, balance_after REAL,
            trade_pnl_usd REAL, trade_pnl_pct REAL,
            position_size TEXT, allocated_usd REAL, timestamp TEXT
        );
        CREATE TABLE IF NOT EXISTS portfolio_state (
            id INTEGER PRIMARY KEY CHECK(id=1),
            current_balance REAL DEFAULT 10000,
            peak_balance REAL DEFAULT 10000,
            total_pnl_usd REAL DEFAULT 0,
            total_trades INTEGER DEFAULT 0,
            winning_trades INTEGER DEFAULT 0,
            updated_at TEXT
        );
    """)
    conn.commit()
    if conn.execute("SELECT COUNT(*) FROM portfolio_state").fetchone()[0] == 0:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("INSERT INTO portfolio_state VALUES(1,10000,10000,0,0,0,?)",(now,))
        conn.commit()


def sync_trades(conn):
    ensure_portfolio(conn)
    closed = conn.execute("""
        SELECT t.* FROM trade_log t
        LEFT JOIN portfolio p ON p.trade_id=t.id
        WHERE t.status='closed' AND t.pnl_pct IS NOT NULL AND p.id IS NULL
        ORDER BY t.updated_at ASC
    """).fetchall()
    for t in closed:
        row = conn.execute("SELECT * FROM portfolio_state WHERE id=1").fetchone()
        s = dict(row) if row else {"current_balance":10000,"peak_balance":10000,"total_pnl_usd":0,"total_trades":0,"winning_trades":0}
        bb = s["current_balance"]; pnl = t["pnl_pct"] or 0; ps = t["position_size"] or "small"
        alloc = bb * SIZE_ALLOCATION.get(ps, 0.01)
        pusd = alloc * (pnl/100); ba = bb + pusd
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("INSERT INTO portfolio VALUES(NULL,?,?,?,?,?,?,?,?)",
            (t["id"],bb,ba,pusd,pnl,ps,alloc,now))
        conn.execute("UPDATE portfolio_state SET current_balance=?,peak_balance=?,total_pnl_usd=?,total_trades=?,winning_trades=?,updated_at=? WHERE id=1",
            (ba,max(s["peak_balance"],ba),s["total_pnl_usd"]+pusd,s["total_trades"]+1,s["winning_trades"]+(1 if pnl>0 else 0),now))
        conn.commit()


@app.get("/api/state")
def get_state():
    if not STATE_PATH.exists(): return {"cycle_count":0,"active_narrative":None,"last_run":None}
    return json.loads(STATE_PATH.read_text())

@app.get("/api/narratives")
def get_narratives():
    if not DB_PATH.exists(): return []
    conn = db(); rows = conn.execute("SELECT * FROM narrative_memory ORDER BY first_detected DESC").fetchall(); conn.close()
    return [dict(r) for r in rows]

@app.get("/api/trades")
def get_trades():
    if not DB_PATH.exists(): return []
    conn = db(); rows = conn.execute("SELECT * FROM trade_log ORDER BY created_at DESC LIMIT 50").fetchall(); conn.close()
    return [dict(r) for r in rows]

@app.get("/api/config")
def get_config():
    if not CONFIG_PATH.exists(): return {}
    return json.loads(CONFIG_PATH.read_text())


@app.get("/api/logs")
def get_logs():
    if not LOG_PATH.exists():
        return {"lines": [], "updated_at": None}
    try:
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        return {
            "lines": lines[-150:],
            "updated_at": datetime.fromtimestamp(
                LOG_PATH.stat().st_mtime, tz=timezone.utc
            ).isoformat(),
        }
    except OSError as exc:
        return {"lines": [], "updated_at": None, "error": str(exc)}

@app.get("/api/portfolio")
def get_portfolio():
    if not DB_PATH.exists():
        return {"state":{"current_balance":10000,"peak_balance":10000,"total_pnl_usd":0,"total_trades":0,"winning_trades":0},"curve":[]}
    conn = db(); sync_trades(conn)
    s = dict(conn.execute("SELECT * FROM portfolio_state WHERE id=1").fetchone() or {})
    curve_rows = conn.execute("SELECT timestamp,balance_after as balance,trade_pnl_usd as pnl_usd,trade_pnl_pct as pnl_pct FROM portfolio ORDER BY timestamp ASC").fetchall()
    curve = [{"timestamp":"start","balance":10000,"pnl_usd":0,"pnl_pct":0}] + [dict(r) for r in curve_rows]
    conn.close()
    return {"state":s or {"current_balance":10000,"peak_balance":10000,"total_pnl_usd":0,"total_trades":0,"winning_trades":0},"curve":curve}

@app.get("/api/stats")
def get_stats():
    if not DB_PATH.exists(): return {}
    conn = db(); sync_trades(conn)
    total = conn.execute("SELECT COUNT(*) FROM trade_log").fetchone()[0]
    open_ = conn.execute("SELECT COUNT(*) FROM trade_log WHERE status='open'").fetchone()[0]
    closed = conn.execute("SELECT COUNT(*) FROM trade_log WHERE status='closed'").fetchone()[0]
    wins = conn.execute("SELECT COUNT(*) FROM trade_log WHERE status='closed' AND pnl_pct>0").fetchone()[0]
    avg = conn.execute("SELECT AVG(pnl_pct) FROM trade_log WHERE status='closed' AND pnl_pct IS NOT NULL").fetchone()[0]
    narr = conn.execute("SELECT COUNT(*) FROM narrative_memory").fetchone()[0]
    ensure_portfolio(conn)
    p = dict(conn.execute("SELECT * FROM portfolio_state WHERE id=1").fetchone() or {})
    conn.close()
    bal = p.get("current_balance", 10000)
    return {
        "total_trades":total,"open_trades":open_,"closed_trades":closed,
        "win_rate":round(wins/closed*100,1) if closed else 0,
        "avg_pnl":round(avg,2) if avg else 0,
        "narratives_in_memory":narr,
        "current_balance":round(bal,2),
        "total_return_pct":round((bal-10000)/10000*100,2),
        "total_pnl_usd":round(p.get("total_pnl_usd",0),2),
    }

@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(HTML)

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Narrative Memory Agent</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
:root{
  --bg:        #060e1a;
  --bg2:       #0a1628;
  --bg3:       #0f1e35;
  --bg4:       #162540;
  --teal:      #0d9488;
  --teal2:     #14b8a6;
  --teal3:     #5eead4;
  --blue:      #0ea5e9;
  --green:     #10b981;
  --green2:    #34d399;
  --red:       #ef4444;
  --red2:      #f87171;
  --amber:     #f59e0b;
  --amber2:    #fbbf24;
  --text:      #f0f4f8;
  --text2:     #94a3b8;
  --text3:     #475569;
  --border:    rgba(14,165,233,0.12);
  --border2:   rgba(14,165,233,0.06);
  --glass:     rgba(10,22,40,0.7);
  --mono:      'JetBrains Mono',monospace;
  --sans:      'Plus Jakarta Sans',sans-serif;
  --sidebar-w: 220px;
}
html{font-size:16px;}
body{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100dvh;-webkit-font-smoothing:antialiased;overflow-x:hidden;}

/* ── Background mesh ── */
body::before{
  content:'';position:fixed;inset:0;z-index:0;pointer-events:none;
  background:
    radial-gradient(ellipse 80% 50% at 20% 10%, rgba(13,148,136,0.08) 0%, transparent 60%),
    radial-gradient(ellipse 60% 40% at 80% 80%, rgba(14,165,233,0.06) 0%, transparent 60%);
}

/* ── Layout shell ── */
.app{display:flex;min-height:100dvh;position:relative;z-index:1;}

/* ── Sidebar ── */
.sidebar{
  width:var(--sidebar-w);flex-shrink:0;
  background:var(--bg2);
  border-right:1px solid var(--border2);
  display:flex;flex-direction:column;
  position:fixed;top:0;left:0;height:100dvh;z-index:100;
  transition:transform 280ms cubic-bezier(0.32,0.72,0,1);
}
.sidebar-logo{
  padding:24px 20px 20px;
  border-bottom:1px solid var(--border2);
  display:flex;align-items:center;gap:10px;
}
.sidebar-logo-icon{
  width:32px;height:32px;border-radius:8px;flex-shrink:0;
  background:linear-gradient(135deg,var(--teal),#0369a1);
  display:flex;align-items:center;justify-content:center;
  box-shadow:0 0 16px rgba(13,148,136,0.3);
}
.sidebar-logo-icon svg{width:16px;height:16px;}
.sidebar-logo-text{font-size:0.82rem;font-weight:700;letter-spacing:-0.01em;line-height:1.2;}
.sidebar-logo-sub{font-size:0.6rem;color:var(--text3);font-family:var(--mono);letter-spacing:0.08em;margin-top:2px;}

.sidebar-section{padding:20px 12px 8px;font-family:var(--mono);font-size:0.58rem;color:var(--text3);letter-spacing:0.12em;text-transform:uppercase;}
.nav-item{
  display:flex;align-items:center;gap:10px;
  padding:9px 12px;border-radius:8px;margin:1px 4px;
  font-size:0.82rem;font-weight:500;color:var(--text2);
  cursor:pointer;transition:all 180ms cubic-bezier(0.32,0.72,0,1);
  user-select:none;
}
.nav-item:hover{background:rgba(14,165,233,0.06);color:var(--text);}
.nav-item.active{background:rgba(13,148,136,0.12);color:var(--teal2);border:1px solid rgba(13,148,136,0.2);}
.nav-item svg{width:15px;height:15px;flex-shrink:0;opacity:0.8;}
.nav-item.active svg{opacity:1;}

.sidebar-bottom{margin-top:auto;padding:16px;border-top:1px solid var(--border2);}
.sidebar-status{display:flex;align-items:center;gap:8px;font-size:0.72rem;color:var(--text3);font-family:var(--mono);}
.status-dot{width:6px;height:6px;border-radius:50%;background:var(--green2);box-shadow:0 0 6px var(--green2);animation:pulse 2s ease-in-out infinite;flex-shrink:0;}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:0.4}}

/* ── Main content ── */
.main{
  margin-left:var(--sidebar-w);flex:1;
  display:flex;flex-direction:column;min-height:100dvh;
}

/* ── Topbar ── */
.topbar{
  padding:16px 28px;
  border-bottom:1px solid var(--border2);
  display:flex;align-items:center;justify-content:space-between;
  background:rgba(6,14,26,0.8);
  backdrop-filter:blur(12px);
  position:sticky;top:0;z-index:50;
}
.topbar-left{display:flex;flex-direction:column;gap:2px;}
.topbar-title{font-size:1rem;font-weight:700;letter-spacing:-0.02em;}
.topbar-sub{font-size:0.7rem;color:var(--text3);font-family:var(--mono);}
.topbar-right{display:flex;align-items:center;gap:12px;}
.topbar-chip{
  display:flex;align-items:center;gap:6px;
  background:var(--bg3);border:1px solid var(--border2);
  border-radius:20px;padding:5px 12px;
  font-family:var(--mono);font-size:0.65rem;color:var(--text2);
}
.topbar-chip.live{border-color:rgba(16,185,129,0.3);color:var(--green2);}

/* ── Content area ── */
.content{padding:24px 28px;flex:1;}

/* ── Balance header strip ── */
.balance-strip{
  display:flex;align-items:flex-end;justify-content:space-between;
  margin-bottom:24px;flex-wrap:wrap;gap:16px;
}
.balance-main{}
.balance-label{font-family:var(--mono);font-size:0.62rem;color:var(--text3);letter-spacing:0.1em;text-transform:uppercase;margin-bottom:4px;}
.balance-value{font-size:2.4rem;font-weight:700;letter-spacing:-0.04em;line-height:1;}
.balance-return{display:flex;align-items:center;gap:8px;margin-top:6px;flex-wrap:wrap;}
.balance-pnl{font-family:var(--mono);font-size:0.78rem;}
.balance-start{font-family:var(--mono);font-size:0.65rem;color:var(--text3);}
.balance-meta{display:flex;gap:16px;align-items:flex-end;flex-wrap:wrap;}
.bm-item{text-align:right;}
.bm-label{font-family:var(--mono);font-size:0.58rem;color:var(--text3);letter-spacing:0.08em;text-transform:uppercase;}
.bm-val{font-size:1rem;font-weight:600;font-family:var(--mono);}

/* ── Equity chart panel ── */
.chart-panel{
  background:var(--bg2);border:1px solid var(--border2);
  border-radius:16px;padding:20px;margin-bottom:20px;
  position:relative;overflow:hidden;
}
.chart-panel::before{
  content:'';position:absolute;top:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,rgba(13,148,136,0.4),transparent);
}
.chart-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:16px;}
.chart-title{font-family:var(--mono);font-size:0.65rem;color:var(--text2);letter-spacing:0.08em;text-transform:uppercase;display:flex;align-items:center;gap:8px;}
.chart-tabs{display:flex;gap:4px;}
.chart-tab{padding:4px 10px;border-radius:6px;font-family:var(--mono);font-size:0.62rem;cursor:pointer;transition:all 150ms;color:var(--text3);border:1px solid transparent;}
.chart-tab.active{background:rgba(13,148,136,0.12);color:var(--teal2);border-color:rgba(13,148,136,0.2);}
.chart-tab:hover:not(.active){color:var(--text2);}
#equity-canvas{width:100%;height:200px;display:block;}
.chart-tooltip{
  position:absolute;background:var(--bg3);border:1px solid var(--border);
  border-radius:8px;padding:8px 12px;pointer-events:none;
  font-family:var(--mono);font-size:0.68rem;line-height:1.6;
  opacity:0;transition:opacity 150ms;z-index:10;
  min-width:140px;
}

/* ── Metric cards row ── */
.metrics-row{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:20px;}
@media(max-width:1100px){.metrics-row{grid-template-columns:repeat(2,1fr);}}
@media(max-width:600px){.metrics-row{grid-template-columns:1fr;}}
.metric-card{
  background:var(--bg2);border:1px solid var(--border2);border-radius:14px;
  padding:16px 18px;transition:border-color 200ms cubic-bezier(0.32,0.72,0,1);
  position:relative;overflow:hidden;
}
.metric-card::after{content:'';position:absolute;inset:0;border-radius:14px;background:linear-gradient(135deg,rgba(255,255,255,0.02),transparent);pointer-events:none;}
.metric-card:hover{border-color:var(--border);}
.metric-icon{width:32px;height:32px;border-radius:8px;display:flex;align-items:center;justify-content:center;margin-bottom:12px;}
.metric-icon.teal{background:rgba(13,148,136,0.15);}
.metric-icon.green{background:rgba(16,185,129,0.15);}
.metric-icon.red{background:rgba(239,68,68,0.12);}
.metric-icon.amber{background:rgba(245,158,11,0.12);}
.metric-icon svg{width:15px;height:15px;}
.metric-label{font-family:var(--mono);font-size:0.6rem;color:var(--text3);letter-spacing:0.1em;text-transform:uppercase;margin-bottom:6px;}
.metric-value{font-size:1.5rem;font-weight:700;letter-spacing:-0.03em;line-height:1;}
.metric-sub{font-family:var(--mono);font-size:0.62rem;color:var(--text3);margin-top:4px;}

/* ── Stats bar ── */
.stats-bar{
  display:grid;grid-template-columns:repeat(6,1fr);gap:10px;
  background:var(--bg2);border:1px solid var(--border2);
  border-radius:14px;padding:16px 20px;margin-bottom:20px;
}
@media(max-width:900px){.stats-bar{grid-template-columns:repeat(3,1fr);}}
@media(max-width:500px){.stats-bar{grid-template-columns:repeat(2,1fr);}}
.stat-item{display:flex;flex-direction:column;gap:4px;}
.stat-item+.stat-item{border-left:1px solid var(--border2);padding-left:16px;}
@media(max-width:900px){.stat-item:nth-child(3n+1){border-left:none;padding-left:0;}}
.stat-key{font-family:var(--mono);font-size:0.58rem;color:var(--text3);letter-spacing:0.1em;text-transform:uppercase;}
.stat-val{font-size:1.1rem;font-weight:700;letter-spacing:-0.02em;}

/* ── Agent state bar ── */
.agent-bar{
  display:flex;align-items:center;gap:0;
  background:var(--bg2);border:1px solid var(--border);
  border-radius:14px;padding:0;margin-bottom:20px;
  overflow:hidden;flex-wrap:wrap;
}
.agent-item{padding:12px 18px;display:flex;flex-direction:column;gap:3px;flex:1;min-width:120px;}
.agent-item+.agent-item{border-left:1px solid var(--border2);}
.agent-key{font-family:var(--mono);font-size:0.58rem;color:var(--text3);letter-spacing:0.1em;text-transform:uppercase;}
.agent-val{font-family:var(--mono);font-size:0.78rem;color:var(--teal2);font-weight:500;}

/* ── Two-col grid ── */
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px;}
@media(max-width:900px){.grid-2{grid-template-columns:1fr;}}

/* ── Data panels ── */
.panel{background:var(--bg2);border:1px solid var(--border2);border-radius:14px;overflow:hidden;transition:border-color 200ms;}
.panel:hover{border-color:var(--border);}
.panel-header{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--border2);background:rgba(15,30,53,0.5);}
.panel-title{font-family:var(--mono);font-size:0.62rem;color:var(--text2);letter-spacing:0.1em;text-transform:uppercase;display:flex;align-items:center;gap:8px;}
.panel-badge{background:var(--bg3);border:1px solid var(--border2);border-radius:12px;padding:2px 8px;font-size:0.6rem;color:var(--text3);}

/* ── Table ── */
.table-wrap{overflow-x:auto;}
table{width:100%;border-collapse:collapse;}
th{font-family:var(--mono);font-size:0.58rem;color:var(--text3);letter-spacing:0.08em;text-transform:uppercase;text-align:left;padding:10px 16px;font-weight:500;border-bottom:1px solid var(--border2);white-space:nowrap;}
td{padding:10px 16px;font-size:0.78rem;color:var(--text2);border-bottom:1px solid var(--border2);font-family:var(--mono);white-space:nowrap;transition:background 120ms;}
tr:last-child td{border-bottom:none;}
tr:hover td{background:rgba(14,165,233,0.03);}

/* ── Tags ── */
.tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:0.6rem;font-family:var(--mono);letter-spacing:0.04em;font-weight:500;}
.tg{background:rgba(16,185,129,0.1);color:var(--green2);border:1px solid rgba(16,185,129,0.2);}
.tr{background:rgba(239,68,68,0.1);color:var(--red2);border:1px solid rgba(239,68,68,0.2);}
.tb{background:rgba(14,165,233,0.1);color:var(--blue);border:1px solid rgba(14,165,233,0.2);}
.ta{background:rgba(245,158,11,0.1);color:var(--amber2);border:1px solid rgba(245,158,11,0.2);}
.tg2{background:rgba(71,85,105,0.25);color:var(--text3);border:1px solid var(--border2);}

/* ── Rules panel ── */
.rule-row{display:flex;align-items:center;justify-content:space-between;padding:12px 18px;border-bottom:1px solid var(--border2);transition:background 120ms;flex-wrap:wrap;gap:8px;}
.rule-row:last-child{border-bottom:none;}
.rule-row:hover{background:rgba(14,165,233,0.03);}
.rule-left{display:flex;flex-direction:column;gap:3px;}
.rule-name{font-size:0.78rem;font-weight:600;color:var(--text);}
.rule-thresh{font-family:var(--mono);font-size:0.58rem;color:var(--text3);}
.rule-right{display:flex;align-items:center;gap:20px;flex-wrap:wrap;}
.rule-stat{display:flex;flex-direction:column;align-items:flex-end;gap:2px;}
.rs-label{font-size:0.55rem;color:var(--text3);font-family:var(--mono);letter-spacing:0.08em;text-transform:uppercase;}
.rs-val{font-size:0.78rem;font-family:var(--mono);font-weight:500;}

/* ── Pnl helpers ── */
.pos{color:var(--green2);} .neg{color:var(--red2);} .neu{color:var(--text3);}

/* ── Empty ── */
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;padding:40px 24px;color:var(--text3);gap:6px;}
.empty-icon{opacity:0.2;margin-bottom:4px;}
.empty-text{font-family:var(--mono);font-size:0.68rem;letter-spacing:0.06em;}

/* ── Progress bar ── */
.prog-bar{height:3px;border-radius:2px;background:var(--bg4);overflow:hidden;margin-top:6px;}
.prog-fill{height:100%;border-radius:2px;background:linear-gradient(90deg,var(--teal),var(--teal2));transition:width 600ms cubic-bezier(0.32,0.72,0,1);}

/* ── Refresh strip ── */
.refresh-strip{position:fixed;bottom:0;left:0;right:0;height:2px;background:var(--border2);z-index:200;}
.refresh-prog{height:100%;background:linear-gradient(90deg,var(--teal),var(--teal2));transition:width 1s linear;}

/* ── Mobile sidebar toggle ── */
.sidebar-toggle{display:none;position:fixed;top:14px;left:14px;z-index:200;width:36px;height:36px;border-radius:8px;background:var(--bg2);border:1px solid var(--border2);align-items:center;justify-content:center;cursor:pointer;}
@media(max-width:768px){
  .sidebar{transform:translateX(-100%);}
  .sidebar.open{transform:translateX(0);}
  .main{margin-left:0;}
  .sidebar-toggle{display:flex;}
  .topbar{padding-left:60px;}
  .content{padding:16px;}
  .metrics-row{grid-template-columns:repeat(2,1fr);}
  .stats-bar{grid-template-columns:repeat(2,1fr);}
  .stat-item+.stat-item{border-left:none;padding-left:0;}
}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation-duration:0.01ms!important;transition-duration:0.01ms!important;}}
</style>
</head>
<body>

<button class="sidebar-toggle" onclick="document.querySelector('.sidebar').classList.toggle('open')" aria-label="Toggle navigation">
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
</button>

<div class="app">

  <!-- Sidebar -->
  <aside class="sidebar">
    <div class="sidebar-logo">
      <div class="sidebar-logo-icon">
        <svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/>
        </svg>
      </div>
      <div>
        <div class="sidebar-logo-text">NMA</div>
        <div class="sidebar-logo-sub">BITGET HACKATHON S1</div>
      </div>
    </div>

    <div class="sidebar-section">Navigation</div>

    <nav style="padding:0 4px;">
      <div class="nav-item active" onclick="showSection('dashboard')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>
        Dashboard
      </div>
      <div class="nav-item" onclick="showSection('trades')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
        Trade Log
      </div>
      <div class="nav-item" onclick="showSection('memory')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
        Narrative Memory
      </div>
      <div class="nav-item" onclick="showSection('strategy')">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14M4.93 4.93a10 10 0 0 0 0 14.14"/></svg>
        Strategy Rules
      </div>
    </nav>

    <div class="sidebar-section" style="margin-top:8px;">Agent Info</div>
    <div style="padding:0 8px;">
      <div style="background:var(--bg3);border:1px solid var(--border2);border-radius:10px;padding:12px 14px;font-family:var(--mono);font-size:0.62rem;color:var(--text3);line-height:1.8;">
        <div>Track: <span style="color:var(--teal2);">Track 1</span></div>
        <div>Cycle: <span style="color:var(--text)" id="sb-cycle">--</span></div>
        <div>Narrative: <span style="color:var(--text)" id="sb-narrative">none</span></div>
        <div>Open trades: <span style="color:var(--amber2)" id="sb-open">--</span></div>
      </div>
    </div>

    <div class="sidebar-bottom">
      <div class="sidebar-status">
        <div class="status-dot"></div>
        <span id="sb-lastrun">Agent running</span>
      </div>
    </div>
  </aside>

  <!-- Main -->
  <main class="main">

    <div class="topbar">
      <div class="topbar-left">
        <div class="topbar-title">Portfolio Overview</div>
        <div class="topbar-sub" id="topbar-date">--</div>
      </div>
      <div class="topbar-right">
        <div class="topbar-chip" id="last-cycle-chip">Cycle --</div>
        <div class="topbar-chip live">
          <div class="status-dot" style="width:5px;height:5px;"></div>
          LIVE
        </div>
      </div>
    </div>

    <div class="content">

      <!-- DASHBOARD SECTION -->
      <div id="sec-dashboard">

        <!-- Balance strip -->
        <div class="balance-strip">
          <div class="balance-main">
            <div class="balance-label">Account Balance</div>
            <div class="balance-value" id="bal-value">$10,000.00</div>
            <div class="balance-return">
              <span class="balance-pnl" id="bal-return">+0.00%</span>
              <span class="balance-start">Starting $10,000 USDT</span>
            </div>
          </div>
          <div class="balance-meta">
            <div class="bm-item">
              <div class="bm-label">Peak</div>
              <div class="bm-val green" id="bal-peak">$10,000</div>
            </div>
            <div class="bm-item">
              <div class="bm-label">Max DD</div>
              <div class="bm-val" id="bal-dd">0.00%</div>
            </div>
            <div class="bm-item">
              <div class="bm-label">PnL</div>
              <div class="bm-val" id="bal-pnl-usd">$0.00</div>
            </div>
          </div>
        </div>

        <!-- Equity Chart -->
        <div class="chart-panel">
          <div class="chart-header">
            <div class="chart-title">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
              Equity Curve
            </div>
            <div style="font-family:var(--mono);font-size:0.6rem;color:var(--text3);">$10,000 starting · 30s refresh</div>
          </div>
          <canvas id="equity-canvas"></canvas>
          <div class="chart-tooltip" id="chart-tooltip"></div>
        </div>

        <!-- Metric cards -->
        <div class="metrics-row">
          <div class="metric-card">
            <div class="metric-icon teal">
              <svg viewBox="0 0 24 24" fill="none" stroke="#14b8a6" stroke-width="2"><path d="M12 20V10"/><path d="M18 20V4"/><path d="M6 20v-4"/></svg>
            </div>
            <div class="metric-label">Total Trades</div>
            <div class="metric-value blue" id="m-total">--</div>
            <div class="metric-sub" id="m-open-sub">-- open positions</div>
          </div>
          <div class="metric-card">
            <div class="metric-icon green">
              <svg viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2"><polyline points="20 6 9 17 4 12"/></svg>
            </div>
            <div class="metric-label">Win Rate</div>
            <div class="metric-value" id="m-winrate">--</div>
            <div class="metric-sub" id="m-wl-sub">-- W / -- L</div>
          </div>
          <div class="metric-card">
            <div class="metric-icon amber">
              <svg viewBox="0 0 24 24" fill="none" stroke="#f59e0b" stroke-width="2"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>
            </div>
            <div class="metric-label">Avg PnL / Trade</div>
            <div class="metric-value" id="m-avgpnl">--</div>
            <div class="metric-sub">per closed trade</div>
          </div>
          <div class="metric-card">
            <div class="metric-icon teal">
              <svg viewBox="0 0 24 24" fill="none" stroke="#14b8a6" stroke-width="2"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
            </div>
            <div class="metric-label">Memory Records</div>
            <div class="metric-value blue" id="m-memory">--</div>
            <div class="metric-sub">narrative patterns</div>
          </div>
        </div>

        <!-- Stats bar -->
        <div class="stats-bar">
          <div class="stat-item"><div class="stat-key">Cycle</div><div class="stat-val teal2" id="st-cycle" style="color:var(--teal2)">--</div></div>
          <div class="stat-item"><div class="stat-key">Active Narrative</div><div class="stat-val" id="st-narrative" style="font-size:0.82rem;font-family:var(--mono)">none</div></div>
          <div class="stat-item"><div class="stat-key">Narrative Day</div><div class="stat-val" id="st-narday">--</div></div>
          <div class="stat-item"><div class="stat-key">Entry Status</div><div class="stat-val" id="st-entry" style="font-size:0.82rem;font-family:var(--mono)">--</div></div>
          <div class="stat-item"><div class="stat-key">Open Positions</div><div class="stat-val" style="color:var(--amber2)" id="st-openpos">--</div></div>
          <div class="stat-item"><div class="stat-key">Last Detections</div><div class="stat-val" id="st-detections" style="font-size:0.72rem;font-family:var(--mono)">--</div></div>
        </div>

        <!-- Recent trades (compact) -->
        <div class="panel" style="margin-bottom:16px;">
          <div class="panel-header">
            <div class="panel-title">
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
              Recent Trades
              <span class="panel-badge" id="trade-count">0</span>
            </div>
            <div style="font-family:var(--mono);font-size:0.58rem;color:var(--text3);cursor:pointer;" onclick="showSection('trades')">View all →</div>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Symbol</th><th>Type</th><th>Entry</th><th>Current</th><th>Live PnL</th><th>Stop</th><th>Target</th><th>Updated</th><th>Status</th></tr></thead>
              <tbody id="trade-body-dash"><tr><td colspan="9"><div class="empty"><div class="empty-text">No trades yet</div></div></td></tr></tbody>
            </table>
          </div>
        </div>

        <div class="panel" style="margin-bottom:16px;">
          <div class="panel-header">
            <div class="panel-title">Agent Activity</div>
            <span class="panel-badge" id="logs-updated">--</span>
          </div>
          <div id="activity-log" style="height:260px;overflow:auto;padding:12px 16px;background:var(--bg);font-family:var(--mono);font-size:0.68rem;line-height:1.65;color:var(--text2);white-space:pre-wrap;">
            Loading agent activity...
          </div>
        </div>

      </div><!-- /dashboard -->

      <!-- TRADES SECTION -->
      <div id="sec-trades" style="display:none;">
        <div style="margin-bottom:20px;">
          <div style="font-size:1.1rem;font-weight:700;margin-bottom:4px;">Trade Log</div>
          <div style="font-family:var(--mono);font-size:0.65rem;color:var(--text3);">All paper trades — auto-refreshes every 30 seconds</div>
        </div>
        <div class="panel">
          <div class="panel-header">
            <div class="panel-title">
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
              All Trades
              <span class="panel-badge" id="trade-count-full">0</span>
            </div>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>ID</th><th>Symbol</th><th>Type</th><th>Side</th><th>Narrative</th><th>Entry</th><th>Current / Exit</th><th>Stop</th><th>Target</th><th>Size</th><th>PnL</th><th>Status</th></tr></thead>
              <tbody id="trade-body-full"><tr><td colspan="12"><div class="empty"><div class="empty-text">No trades yet</div></div></td></tr></tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- MEMORY SECTION -->
      <div id="sec-memory" style="display:none;">
        <div style="margin-bottom:20px;">
          <div style="font-size:1.1rem;font-weight:700;margin-bottom:4px;">Narrative Memory</div>
          <div style="font-family:var(--mono);font-size:0.65rem;color:var(--text3);">Historical narrative patterns — the agent's prior knowledge</div>
        </div>
        <div class="panel" style="margin-bottom:16px;">
          <div class="panel-header"><div class="panel-title">Active Narrative Memory</div></div>
          <div id="active-memory" style="padding:16px;color:var(--text2);font-family:var(--mono);font-size:0.72rem;">Loading active memory...</div>
        </div>
        <div class="panel">
          <div class="panel-header">
            <div class="panel-title">
              <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/></svg>
              Memory Records
              <span class="panel-badge" id="memory-count">0</span>
            </div>
          </div>
          <div class="table-wrap">
            <table>
              <thead><tr><th>Narrative</th><th>First Detected</th><th>Avg Return</th><th>Days to Peak</th><th>Optimal Entry</th><th>Sentiment</th><th>Outcome</th><th>Updated</th></tr></thead>
              <tbody id="memory-body"><tr><td colspan="8"><div class="empty"><div class="empty-text">Loading memory...</div></div></td></tr></tbody>
            </table>
          </div>
        </div>
      </div>

      <!-- STRATEGY SECTION -->
      <div id="sec-strategy" style="display:none;">
        <div style="margin-bottom:20px;">
          <div style="font-size:1.1rem;font-weight:700;margin-bottom:4px;">Fallback Strategy</div>
          <div style="font-family:var(--mono);font-size:0.65rem;color:var(--text3);">Self-learning rules — thresholds auto-adjust based on win rate every 5 trades</div>
        </div>
        <div class="panel" id="rules-body">
          <div class="empty"><div class="empty-text">Loading rules...</div></div>
        </div>
      </div>

    </div><!-- /content -->
  </main>
</div>

<div class="refresh-strip"><div class="refresh-prog" id="refresh-prog" style="width:100%"></div></div>

<script>
const REFRESH = 30000;
let curveData = [];
let apiErrors = [];

// ── Helpers ──────────────────────────────────────────────────────────────
async function api(url) {
  try {
    const r = await fetch(url, {cache:'no-store'});
    if (!r.ok) throw new Error(r.status);
    return await r.json();
  }
  catch(e) {
    console.error(url, e);
    apiErrors.push(url);
    return null;
  }
}
function fmtDate(iso) {
  if (!iso || iso === 'start') return '--';
  try { const d = new Date(iso); return d.toLocaleDateString('en-GB',{month:'short',day:'numeric'}) + ' ' + d.toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'}); }
  catch { return '--'; }
}
function fmtMoney(v, dec=2) { return v != null ? '$' + parseFloat(v).toLocaleString('en-US',{minimumFractionDigits:dec,maximumFractionDigits:dec}) : '--'; }
function fmtPct(v, sign=true) { if(v==null) return '--'; const n=parseFloat(v); return (sign&&n>0?'+':'')+n.toFixed(2)+'%'; }
function pnlHtml(v) {
  if(v==null) return '<span class="neu">--</span>';
  const n=parseFloat(v); const cls=n>0?'pos':n<0?'neg':'neu';
  return `<span class="${cls}">${n>0?'+':''}${n.toFixed(2)}%</span>`;
}
function outcomeTag(o) {
  const m = {played_out:'tg',fizzled:'tr',running:'ta',unknown:'tg2',stopped_out:'tr'};
  return `<span class="tag ${m[o]||'tg2'}">${(o||'--').replace(/_/g,' ')}</span>`;
}
function statusTag(s) { return s==='open'?'<span class="tag ta">open</span>':'<span class="tag tg2">closed</span>'; }
function sideTag(s) { return s==='long'?'<span class="tag tg">long</span>':'<span class="tag tr">short</span>'; }
function typeTag(s) { return s==='fallback'?'<span class="tag tg2">scouted</span>':'<span class="tag ta">narrative</span>'; }
function narLabel(t) { return (t||'--').replace(/_/g,' '); }

// ── Section navigation ────────────────────────────────────────────────────
function showSection(name) {
  ['dashboard','trades','memory','strategy'].forEach(s => {
    document.getElementById('sec-'+s).style.display = s===name ? '' : 'none';
  });
  document.querySelectorAll('.nav-item').forEach((el,i) => {
    el.classList.toggle('active', ['dashboard','trades','memory','strategy'][i]===name);
  });
  // Update topbar title
  const titles = {dashboard:'Portfolio Overview',trades:'Trade Log',memory:'Narrative Memory',strategy:'Strategy Rules'};
  document.querySelector('.topbar-title').textContent = titles[name] || 'Dashboard';
  // Close sidebar on mobile
  document.querySelector('.sidebar').classList.remove('open');
}

// ── Equity chart ──────────────────────────────────────────────────────────
function drawChart(curve) {
  curveData = curve || [];
  const canvas = document.getElementById('equity-canvas');
  if (!canvas) return;
  const dpr = window.devicePixelRatio || 1;
  const W = canvas.offsetWidth, H = 200;
  canvas.width = W * dpr; canvas.height = H * dpr;
  canvas.style.height = H + 'px';
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);

  if (!curve || curve.length < 2) {
    ctx.fillStyle = 'rgba(148,163,184,0.25)';
    ctx.font = '11px JetBrains Mono, monospace';
    ctx.textAlign = 'center';
    ctx.fillText('Equity curve appears after first closed trade', W/2, H/2);
    return;
  }

  const balances = curve.map(p => parseFloat(p.balance));
  const min = Math.min(...balances); const max = Math.max(...balances);
  const pad = {l:68,r:16,t:12,b:28};
  const cW = W-pad.l-pad.r, cH = H-pad.t-pad.b;
  const range = max - min || 100;
  const toX = i => pad.l + (i/(curve.length-1))*cW;
  const toY = v => pad.t + cH - ((v-min)/range)*cH;
  const isUp = balances[balances.length-1] >= 10000;
  const lineColor = isUp ? '#10b981' : '#ef4444';
  const fillColor0 = isUp ? 'rgba(16,185,129,0.2)' : 'rgba(239,68,68,0.18)';
  const fillColor1 = 'rgba(16,185,129,0.0)';

  // Grid
  ctx.strokeStyle = 'rgba(71,85,105,0.2)'; ctx.lineWidth = 1;
  [0,0.25,0.5,0.75,1].forEach(f => {
    const y = pad.t + f*cH;
    ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(pad.l+cW, y); ctx.stroke();
    const val = max - f*range;
    ctx.fillStyle = 'rgba(148,163,184,0.5)';
    ctx.font = '9px JetBrains Mono, monospace'; ctx.textAlign = 'right';
    ctx.fillText('$'+Math.round(val).toLocaleString(), pad.l-6, y+3);
  });

  // Baseline $10k
  const by = toY(10000);
  if (by >= pad.t && by <= pad.t+cH) {
    ctx.strokeStyle='rgba(148,163,184,0.2)'; ctx.lineWidth=1; ctx.setLineDash([3,4]);
    ctx.beginPath(); ctx.moveTo(pad.l,by); ctx.lineTo(pad.l+cW,by); ctx.stroke();
    ctx.setLineDash([]);
  }

  // Fill
  const grad = ctx.createLinearGradient(0,pad.t,0,pad.t+cH);
  grad.addColorStop(0,fillColor0); grad.addColorStop(1,fillColor1);
  ctx.beginPath(); ctx.moveTo(toX(0),toY(balances[0]));
  balances.forEach((_,i) => { if(i>0) ctx.lineTo(toX(i),toY(balances[i])); });
  ctx.lineTo(toX(curve.length-1),pad.t+cH); ctx.lineTo(toX(0),pad.t+cH);
  ctx.closePath(); ctx.fillStyle=grad; ctx.fill();

  // Line
  ctx.beginPath(); ctx.strokeStyle=lineColor; ctx.lineWidth=2; ctx.lineJoin='round';
  balances.forEach((b,i) => i===0 ? ctx.moveTo(toX(i),toY(b)) : ctx.lineTo(toX(i),toY(b)));
  ctx.stroke();

  // Points
  balances.forEach((b,i) => {
    if(i===0) return;
    ctx.beginPath(); ctx.arc(toX(i),toY(b),3,0,Math.PI*2);
    ctx.fillStyle = parseFloat(curve[i].pnl_pct||0)>=0?'#10b981':'#ef4444'; ctx.fill();
    ctx.strokeStyle='var(--bg2)'; ctx.lineWidth=1.5; ctx.stroke();
  });

  // Store data for tooltip
  canvas._chartMeta = {toX,toY,curve,balances,pad,cW,cH,W,H};
}

// Tooltip on hover
document.getElementById('equity-canvas').addEventListener('mousemove', function(e) {
  const meta = this._chartMeta;
  if (!meta || !curveData || curveData.length < 2) return;
  const rect = this.getBoundingClientRect();
  const mx = e.clientX - rect.left;
  const {toX,curve,balances,pad} = meta;
  let closest = 0, minDist = Infinity;
  balances.forEach((_,i) => { const dx = Math.abs(toX(i)-mx); if(dx<minDist){minDist=dx;closest=i;} });
  if (minDist > 30) { document.getElementById('chart-tooltip').style.opacity='0'; return; }
  const p = curve[closest];
  const tip = document.getElementById('chart-tooltip');
  const pnl = parseFloat(p.pnl_pct||0);
  tip.innerHTML = `<div style="color:var(--text2);margin-bottom:4px;">${p.timestamp==='start'?'Start':fmtDate(p.timestamp)}</div>
    <div style="color:var(--text);font-weight:600;">${fmtMoney(p.balance)}</div>
    <div style="color:${pnl>=0?'#10b981':'#ef4444'}">${pnl>=0?'+':''}${pnl.toFixed(2)}% this trade</div>`;
  const tx = toX(closest); const ty = meta.toY(parseFloat(p.balance));
  const tipW = 150;
  tip.style.left = (tx + tipW > meta.W ? tx - tipW - 8 : tx + 12) + 'px';
  tip.style.top = Math.max(0, ty - 20) + 'px';
  tip.style.opacity = '1';
});
document.getElementById('equity-canvas').addEventListener('mouseleave', () => {
  document.getElementById('chart-tooltip').style.opacity = '0';
});

// ── Renderers ─────────────────────────────────────────────────────────────
function renderPortfolio(data) {
  if (!data) return;
  const {state, curve} = data;
  const bal = state.current_balance ?? 10000;
  const peak = state.peak_balance ?? 10000;
  const ret = (bal-10000)/10000*100;
  const dd = peak > 0 ? (peak-bal)/peak*100 : 0;
  const wins = state.winning_trades ?? 0;
  const total = state.total_trades ?? 0;
  const pnlUsd = state.total_pnl_usd ?? 0;

  const balEl = document.getElementById('bal-value');
  balEl.textContent = fmtMoney(bal);
  balEl.style.color = ret>=0?'var(--green2)':ret<0?'var(--red2)':'var(--text)';

  const retEl = document.getElementById('bal-return');
  retEl.textContent = fmtPct(ret) + ' (' + (pnlUsd>=0?'+':'') + fmtMoney(pnlUsd) + ')';
  retEl.style.color = ret>=0?'var(--green2)':'var(--red2)';

  document.getElementById('bal-peak').textContent = fmtMoney(peak,0);
  const ddEl = document.getElementById('bal-dd');
  ddEl.textContent = dd.toFixed(2)+'%';
  ddEl.style.color = dd>5?'var(--red2)':dd>2?'var(--amber2)':'var(--text3)';
  const pnlEl = document.getElementById('bal-pnl-usd');
  pnlEl.textContent = (pnlUsd>=0?'+':'') + fmtMoney(pnlUsd);
  pnlEl.style.color = pnlUsd>=0?'var(--green2)':'var(--red2)';

  drawChart(curve);
}

function renderStats(stats) {
  if (!stats) return;
  document.getElementById('m-total').textContent = stats.total_trades ?? '--';
  document.getElementById('m-open-sub').textContent = (stats.open_trades??'--') + ' open positions';
  const wr = stats.closed_trades ? stats.win_rate : null;
  const wrEl = document.getElementById('m-winrate');
  wrEl.textContent = wr != null ? wr+'%' : '--';
  wrEl.className = 'metric-value ' + (wr>50?'pos':wr<50?'neg':'neu');
  const wins = stats.closed_trades ? Math.round((stats.win_rate/100)*stats.closed_trades) : 0;
  const losses = (stats.closed_trades||0) - wins;
  document.getElementById('m-wl-sub').textContent = wins+'W / '+losses+'L';
  const avgEl = document.getElementById('m-avgpnl');
  avgEl.textContent = stats.avg_pnl != null && stats.closed_trades ? fmtPct(stats.avg_pnl) : '--';
  avgEl.className = 'metric-value ' + (stats.avg_pnl>0?'pos':stats.avg_pnl<0?'neg':'neu');
  document.getElementById('m-memory').textContent = stats.narratives_in_memory ?? '--';

  document.getElementById('trade-count').textContent = stats.total_trades ?? 0;
  document.getElementById('trade-count-full').textContent = stats.total_trades ?? 0;
}

function renderState(state) {
  if (!state) return;
  const cycle = state.cycle_count ?? '--';
  document.getElementById('st-cycle').textContent = cycle;
  document.getElementById('sb-cycle').textContent = cycle;
  document.getElementById('last-cycle-chip').textContent = 'Cycle ' + cycle;
  const nar = state.active_narrative ? state.active_narrative.replace(/_/g,' ') : 'none';
  document.getElementById('st-narrative').textContent = nar;
  document.getElementById('sb-narrative').textContent = nar;
  document.getElementById('st-narday').textContent = state.active_narrative_day ?? '--';
  document.getElementById('st-entry').textContent = state.waiting_to_enter ? 'wait '+state.days_to_wait+'d' : 'ready';
  document.getElementById('st-openpos').textContent = state.open_trades ?? '--';
  document.getElementById('sb-open').textContent = state.open_trades ?? '--';
  const dets = state.last_detections;
  document.getElementById('st-detections').textContent = (dets&&dets.length) ? dets.join(', ') : 'none';
  if (state.last_run) {
    document.getElementById('topbar-date').textContent = 'Last updated: ' + fmtDate(state.last_run);
    document.getElementById('sb-lastrun').textContent = fmtDate(state.last_run);
  }
}

function renderTrades(trades, bodyId, cols) {
  const tbody = document.getElementById(bodyId);
  if (!trades||!trades.length) {
    tbody.innerHTML=`<tr><td colspan="${cols}"><div class="empty"><div class="empty-text">No trades yet</div></div></td></tr>`;
    return;
  }
  if (cols === 9) {
    tbody.innerHTML = trades.slice(0,10).map(t => `<tr>
      <td style="color:var(--text);font-weight:600;">${t.symbol}</td>
      <td>${typeTag(t.trade_type)}</td>
      <td>${t.entry_price??'--'}</td>
      <td>${t.status==='open'?(t.current_price??'--'):(t.exit_price??'--')}</td>
      <td>${pnlHtml(t.status==='open'?t.unrealized_pnl_pct:t.pnl_pct)}</td>
      <td>${t.stop_loss_price??'--'}</td>
      <td>${t.take_profit_price??'--'}</td>
      <td style="color:var(--text3);font-size:0.65rem;">${fmtDate(t.last_price_at||t.updated_at)}</td>
      <td>${statusTag(t.status)}</td>
    </tr>`).join('');
  } else {
    tbody.innerHTML = trades.map(t => `<tr>
      <td style="color:var(--text3);">#${t.id}</td>
      <td style="color:var(--text);font-weight:600;">${t.symbol}</td>
      <td>${typeTag(t.trade_type)}</td>
      <td>${sideTag(t.side)}</td>
      <td style="color:var(--text3);font-size:0.7rem;">${narLabel(t.narrative_tag)}</td>
      <td>${t.entry_price??'--'}</td>
      <td>${t.status==='open'?(t.current_price??'--'):(t.exit_price??'--')}</td>
      <td>${t.stop_loss_price??'--'}</td>
      <td>${t.take_profit_price??'--'}</td>
      <td><span class="tag tg2">${t.position_size||'--'}</span></td>
      <td>${pnlHtml(t.status==='open'?t.unrealized_pnl_pct:t.pnl_pct)}</td>
      <td>${statusTag(t.status)}</td>
    </tr>`).join('');
  }
}

function renderMemory(narratives) {
  const tbody = document.getElementById('memory-body');
  document.getElementById('memory-count').textContent = narratives ? narratives.length : 0;
  if (!narratives||!narratives.length) {
    tbody.innerHTML='<tr><td colspan="8"><div class="empty"><div class="empty-text">No memory records</div></div></td></tr>'; return;
  }
  tbody.innerHTML = narratives.map(n => `<tr>
    <td style="color:var(--text);font-weight:600;">${narLabel(n.narrative_tag)}</td>
    <td>${n.first_detected?n.first_detected.slice(0,10):'--'}</td>
    <td class="${n.avg_return_pct>0?'pos':n.avg_return_pct<0?'neg':'neu'}">${n.avg_return_pct!=null?(n.avg_return_pct>0?'+':'')+n.avg_return_pct.toFixed(1)+'%':'--'}</td>
    <td>${n.days_to_peak??'--'}d</td>
    <td>day ${n.optimal_entry_day??'--'}</td>
    <td>${n.fear_greed_at_detection??'--'}</td>
    <td>${outcomeTag(n.outcome)}</td>
    <td style="color:var(--text3);font-size:0.65rem;">${n.updated_at?n.updated_at.slice(0,10):'--'}</td>
  </tr>`).join('');
}

function renderActiveMemory(narratives, state) {
  const el = document.getElementById('active-memory');
  if (!narratives) {
    el.textContent = 'Could not load narrative memory.';
    return;
  }
  const active = narratives.find(n => n.outcome === 'running') ||
    narratives.find(n => n.narrative_tag === state?.active_narrative);
  if (!active) {
    el.textContent = 'No narrative is currently marked as running.';
    return;
  }
  el.innerHTML = `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;">
    <div><span style="color:var(--text3)">Narrative</span><br><span style="color:var(--text)">${narLabel(active.narrative_tag)}</span></div>
    <div><span style="color:var(--text3)">First detected</span><br><span style="color:var(--text)">${fmtDate(active.first_detected)}</span></div>
    <div><span style="color:var(--text3)">Fear & Greed</span><br><span style="color:var(--text)">${active.fear_greed_at_detection??'--'}</span></div>
    <div><span style="color:var(--text3)">News volume</span><br><span style="color:var(--text)">${active.news_volume_at_detection??'--'}</span></div>
    <div><span style="color:var(--text3)">Status</span><br>${outcomeTag(active.outcome)}</div>
  </div>`;
}

function renderLogs(data) {
  const el = document.getElementById('activity-log');
  const badge = document.getElementById('logs-updated');
  if (!data) {
    el.textContent = 'Could not load agent logs.';
    badge.textContent = 'error';
    return;
  }
  const lines = data.lines || [];
  el.textContent = lines.length ? lines.join('\n') : 'No agent log entries yet.';
  badge.textContent = data.updated_at ? fmtDate(data.updated_at) : '--';
  el.scrollTop = el.scrollHeight;
}

function renderRules(config) {
  const el = document.getElementById('rules-body');
  if (!config||!config.rules) { el.innerHTML='<div class="empty"><div class="empty-text">No config loaded</div></div>'; return; }
  const scans = config.scan_count ?? 0;
  const deprior = config.deprioritized_assets ?? [];
  const header = `<div class="panel-header">
    <div class="panel-title">
      <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14M4.93 4.93a10 10 0 0 0 0 14.14"/></svg>
      Fallback Rules
    </div>
    <div style="font-family:var(--mono);font-size:0.6rem;color:var(--text3);">${scans} scans · ${deprior.length} deprioritized assets</div>
  </div>`;
  const rows = Object.entries(config.rules).map(([name,r]) => {
    const wr = r.win_rate ?? 0; const wrPct = (wr*100).toFixed(1);
    const wrColor = wr>0.55?'var(--green2)':wr<0.40?'var(--red2)':'var(--amber2)';
    const ruleThresh = name==='momentum_long' ? `${r.min_change_24h_pct}% to ${r.max_change_24h_pct}% daily, ${r.min_volume_ratio}x own volume`
                   : name==='momentum_short' ? `${r.min_change_24h_pct}% to ${r.max_change_24h_pct}% daily, 20h downside breakdown`
                   : name==='fear_bounce' ? `${r.min_change_24h_pct}% to ${r.max_change_24h_pct}% daily, F&G <${r.max_fear_greed}`
                   : name==='volume_breakout' ? `20h high, ${r.volume_vs_avg_multiplier}x own volume`
                   : `${r.min_change_24h_pct}% to ${r.max_change_24h_pct}% daily, taker >${((r.min_taker_buy_ratio||0)*100).toFixed(0)}%`;
    const wl = (r.wins??0) + 'W / ' + (r.losses??0) + 'L';
    const prog = Math.min(100, (r.win_rate??0)*100);
    return `<div class="rule-row">
      <div class="rule-left">
        <div class="rule-name">${name.replace(/_/g,' ')}</div>
        <div class="rule-thresh">${ruleThresh}</div>
        <div class="prog-bar" style="width:140px;"><div class="prog-fill" style="width:${prog}%"></div></div>
      </div>
      <div class="rule-right">
        <div class="rule-stat"><div class="rs-label">Trades</div><div class="rs-val" style="color:var(--text)">${r.trades??0}</div></div>
        <div class="rule-stat"><div class="rs-label">W / L</div><div class="rs-val" style="color:var(--text)">${wl}</div></div>
        <div class="rule-stat"><div class="rs-label">Win Rate</div><div class="rs-val" style="color:${wrColor}">${r.trades?wrPct+'%':'--'}</div></div>
        <div class="rule-stat"><div class="rs-label">Exit Model</div><div class="rs-val" style="color:var(--text2)">${r.reward_risk_ratio||'--'}R, ${r.atr_stop_multiplier||'--'} ATR</div></div>
        <div class="rule-stat"><div class="rs-label">Status</div><div>${r.enabled!==false?'<span class="tag tg">on</span>':'<span class="tag tg2">off</span>'}</div></div>
      </div>
    </div>`;
  }).join('');
  el.innerHTML = header + rows;
}

// ── Refresh bar ───────────────────────────────────────────────────────────
function startRefresh() {
  const el = document.getElementById('refresh-prog');
  el.style.transition='none'; el.style.width='100%';
  setTimeout(()=>{ el.style.transition=`width ${REFRESH}ms linear`; el.style.width='0%'; },50);
}

function safeRender(name, render) {
  try {
    render();
  } catch (error) {
    console.error(`Failed to render ${name}`, error);
    apiErrors.push(`${name} render`);
  }
}

// ── Main load ─────────────────────────────────────────────────────────────
async function loadAll() {
  apiErrors = [];
  const [state,stats,trades,narratives,config,portfolio,logs] = await Promise.all([
    api('/api/state'), api('/api/stats'), api('/api/trades'),
    api('/api/narratives'), api('/api/config'), api('/api/portfolio'),
    api('/api/logs'),
  ]);
  safeRender('state', () => renderState(state));
  safeRender('stats', () => renderStats(stats));
  safeRender('recent trades', () => renderTrades(trades,'trade-body-dash',9));
  safeRender('trade log', () => renderTrades(trades,'trade-body-full',12));
  safeRender('memory', () => renderMemory(narratives));
  safeRender('active memory', () => renderActiveMemory(narratives, state));
  safeRender('logs', () => renderLogs(logs));
  safeRender('rules', () => renderRules(config));
  safeRender('portfolio', () => renderPortfolio(portfolio));
  if (apiErrors.length) {
    document.getElementById('topbar-date').textContent =
      'Data error: ' + apiErrors.join(', ');
  }
  startRefresh();
}

loadAll();
setInterval(loadAll, REFRESH);
window.addEventListener('resize', () => { if(curveData.length) drawChart(curveData); });
</script>
</body>
</html>"""
