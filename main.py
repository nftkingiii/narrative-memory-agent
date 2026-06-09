"""
main.py — Narrative Memory Agent
The main agent loop. Runs all modules on a schedule:
  1. Perception — fetch market data
  2. Detection — find narrative signals
  3. Decision — decide whether to trade
  4. Execution — place paper orders
  5. Monitoring — check open positions
  6. Writeback — update memory from closed trades

Runs every hour by default. Logs everything to logs/agent.log
"""

import os
import sys
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

load_dotenv()

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from agent.perception import get_full_snapshot
from agent.memory import (
    init_db, query_narrative, log_trade,
    close_trade, get_trade_log, update_narrative_outcome,
    record_new_narrative, get_running_narratives,
)
from agent.detection import detect_narratives
from agent.decision import decide_all
from agent.execution import execute_decision, monitor_open_positions
from agent.fallback import run_fallback_scan, update_rule_performance
from agent.writeback import process_closed_trades


# ─────────────────────────────────────────────
# Logging Setup
# ─────────────────────────────────────────────

LOG_PATH = Path("logs/agent.log")
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("agent")


# ─────────────────────────────────────────────
# State
# Tracks current cycle state in memory
# ─────────────────────────────────────────────

class AgentState:
    def __init__(self):
        self.cycle_count = 0
        self.last_snapshot = None
        self.last_detection = []
        self.last_decision = None
        self.active_narrative = None
        self.active_narrative_day = 0   # days since narrative was first detected
        self.waiting_to_enter = False
        self.days_to_wait = 0
        self.fallback_signal = None

    def to_dict(self) -> dict:
        return {
            "cycle_count": self.cycle_count,
            "active_narrative": self.active_narrative,
            "active_narrative_day": self.active_narrative_day,
            "waiting_to_enter": self.waiting_to_enter,
            "days_to_wait": self.days_to_wait,
            "last_run": datetime.now(timezone.utc).isoformat(),
        }


STATE = AgentState()


# ─────────────────────────────────────────────
# Save State to JSON (for dashboard)
# ─────────────────────────────────────────────

STATE_PATH = Path("data/agent_state.json")

def save_state(extra: dict = None):
    """Save current agent state to JSON for dashboard consumption."""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    state = STATE.to_dict()
    if extra:
        state.update(extra)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


# ─────────────────────────────────────────────
# Main Agent Cycle
# ─────────────────────────────────────────────

def run_cycle():
    """
    One full agent cycle:
    Perceive → Detect → Decide → Execute → Monitor → Writeback
    """
    STATE.cycle_count += 1
    cycle_start = datetime.now(timezone.utc)

    log.info(f"{'='*55}")
    log.info(f"CYCLE {STATE.cycle_count} — {cycle_start.strftime('%Y-%m-%d %H:%M UTC')}")
    log.info(f"{'='*55}")

    try:
        # ── Step 1: Perception ───────────────────────────
        log.info("Step 1: Perception")
        snapshot = get_full_snapshot()

        if "error" in snapshot:
            log.error(f"Perception failed: {snapshot['error']}")
            save_state({"last_error": snapshot["error"]})
            return

        STATE.last_snapshot = snapshot
        news_count = len(snapshot.get("news", []))
        log.info(f"  News: {news_count} articles | "
                 f"F&G: {snapshot.get('sentiment', {}).get('fear_greed_value')} | "
                 f"BTC dom: {snapshot.get('market_intel', {}).get('btc_dominance')}")

        # ── Step 2: Detection ────────────────────────────
        log.info("Step 2: Detection")
        detections = detect_narratives(snapshot, memory_query_fn=query_narrative)
        STATE.last_detection = detections

        # Run fallback scan if no narrative detected
        fallback_signal = None
        if not detections:
            log.info("  No narratives detected — running fallback scan")
            fear_greed = snapshot.get("sentiment", {}).get("fear_greed_value")
            fallback_signal = run_fallback_scan(fear_greed=fear_greed)
            if fallback_signal:
                log.info(f"  Fallback signal: {fallback_signal.symbol} | "
                         f"rule={fallback_signal.rule} | score={fallback_signal.score:.2f}")
            else:
                log.info("  No fallback signal found this cycle")
        else:
            log.info(f"  Detected: {[d.narrative_tag for d in detections]}")

        # ── Step 3: Decision ─────────────────────────────
        log.info("Step 3: Decision")
        open_trades = get_trade_log(status="open")

        MAX_POSITIONS = 3
        open_symbols = [t["symbol"] for t in open_trades]

        if len(open_trades) >= MAX_POSITIONS:
            log.info(f"  {len(open_trades)} open position(s) — max {MAX_POSITIONS} reached, skipping")
            STATE.last_decision = None
            fallback_signal = None
        elif detections:
            decisions = decide_all(detections, snapshot.get("sentiment", {}))
            STATE.last_decision = decisions[0] if decisions else None

            if STATE.last_decision and STATE.last_decision.should_enter:
                d = STATE.last_decision
                # Skip if already in this symbol
                if d.symbol in open_symbols:
                    log.info(f"  Already have open position in {d.symbol} — skipping")
                    STATE.last_decision = None
                    d = None
                # Cap size based on concurrent open positions
                if d:
                    if len(open_trades) >= 2:
                        d.position_size = "small"
                    elif len(open_trades) == 1 and d.position_size == "full":
                        d.position_size = "medium"

                # Track narrative entry timing
                if STATE.active_narrative != d.narrative_tag:
                    STATE.active_narrative = d.narrative_tag
                    STATE.active_narrative_day = 0
                    STATE.waiting_to_enter = d.days_to_wait > 0
                    STATE.days_to_wait = d.days_to_wait
                    log.info(f"  New narrative: {d.narrative_tag} | "
                             f"waiting {d.days_to_wait} days before entry")

                    # Record in memory as running
                    record_new_narrative(
                        tag=d.narrative_tag,
                        sentiment_score=snapshot.get("sentiment", {}).get("fear_greed_value"),
                        news_volume=detections[0].news_volume if detections else None,
                        funding_rate=snapshot.get("sentiment", {}).get("btc_funding_rate"),
                        fear_greed=snapshot.get("sentiment", {}).get("fear_greed_value"),
                        btc_dominance=snapshot.get("market_intel", {}).get("btc_dominance"),
                        notes=f"Detected by agent cycle {STATE.cycle_count}",
                    )
                else:
                    STATE.active_narrative_day += 1
                    log.info(f"  Narrative day {STATE.active_narrative_day} / "
                             f"wait {STATE.days_to_wait}")
        else:
            STATE.last_decision = None

        # ── Step 4: Execution ────────────────────────────
        log.info("Step 4: Execution")

        if STATE.last_decision and STATE.last_decision.should_enter:
            d = STATE.last_decision

            # Check if waiting period has passed
            if STATE.active_narrative_day >= STATE.days_to_wait:
                if STATE.waiting_to_enter:
                    log.info(f"  Wait period complete — entering {d.narrative_tag}")
                    STATE.waiting_to_enter = False
                    d.days_to_wait = 0  # override to force execution

                order = execute_decision(d, memory_log_fn=log_trade)
                if order:
                    log.info(f"  Order placed: {order['symbol']} {order['side']} "
                             f"@ {order['entry_price']} (size={order['position_size']})")
            else:
                remaining = STATE.days_to_wait - STATE.active_narrative_day
                log.info(f"  Waiting {remaining} more cycle(s) before entry")
        elif fallback_signal and fallback_signal.symbol not in open_symbols:
            # Execute fallback strategy signal
            log.info(f"  Executing fallback: {fallback_signal.symbol} | {fallback_signal.rule}")
            current_price = fallback_signal.last_price

            trade_id = log_trade(
                narrative_tag=f"fallback_{fallback_signal.rule}",
                symbol=fallback_signal.symbol,
                side="long",
                entry_price=current_price,
                position_size=fallback_signal.position_size,
                memory_informed=False,
                notes=fallback_signal.reason,
            )
            log.info(f"  Fallback trade logged: {fallback_signal.symbol} @ {current_price} "                     f"(id={trade_id})")
        else:
            log.info("  No entry signal this cycle")

        # ── Step 5: Monitor Open Positions ───────────────
        log.info("Step 5: Monitor")
        closed = monitor_open_positions(close_trade_fn=close_trade)
        if closed:
            log.info(f"  Closed {len(closed)} position(s)")
            for c in closed:
                log.info(f"    {c['symbol']} {c['exit_reason']} "
                         f"PnL={c['pnl_pct']:.2f}%")
        else:
            open_count = len(get_trade_log(status="open"))
            log.info(f"  {open_count} open position(s), no exits triggered")

        # ── Step 6: Writeback ────────────────────────────
        log.info("Step 6: Writeback")
        wb_results = process_closed_trades(
            get_trades_fn=get_trade_log,
            get_memory_fn=query_narrative,
            update_narrative_fn=update_narrative_outcome,
        )
        if wb_results:
            for r in wb_results:
                log.info(f"  Written back: {r.narrative_tag} -> "
                         f"{r.outcome_written} | PnL={r.actual_pnl_pct}% | "
                         f"accuracy={r.prediction_accuracy}")

        # ── Save State ───────────────────────────────────
        cycle_end = datetime.now(timezone.utc)
        duration = (cycle_end - cycle_start).total_seconds()

        save_state({
            "last_detections": [d.narrative_tag for d in detections],
            "open_trades": len(get_trade_log(status="open")),
            "closed_trades": len(get_trade_log(status="closed")),
            "cycle_duration_seconds": round(duration, 1),
        })

        log.info(f"Cycle {STATE.cycle_count} complete in {duration:.1f}s")
        log.info("")

    except Exception as e:
        log.exception(f"Cycle {STATE.cycle_count} failed: {e}")
        save_state({"last_error": str(e)})


# ─────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────

def main():
    log.info("Narrative Memory Agent starting...")
    log.info(f"   Log: {LOG_PATH}")
    log.info(f"   DB:  data/memory.db")

    # Init DB
    init_db()

    # Run one immediate cycle
    log.info("Running initial cycle...")
    run_cycle()

    # Schedule hourly cycles
    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        run_cycle,
        trigger=IntervalTrigger(hours=1),
        id="agent_cycle",
        name="Narrative Memory Agent Cycle",
        misfire_grace_time=300,
    )

    log.info("Scheduler started — running every hour. Press Ctrl+C to stop.")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        log.info("Agent stopped by user")
        scheduler.shutdown()


if __name__ == "__main__":
    main()