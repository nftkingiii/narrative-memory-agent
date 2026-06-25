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
PROJECT_ROOT = Path(__file__).parent
DATA_DIR = Path(os.getenv("DATA_DIR", PROJECT_ROOT / "data"))
LOG_DIR = Path(os.getenv("LOG_DIR", PROJECT_ROOT / "logs"))
sys.path.insert(0, str(PROJECT_ROOT))

from agent.perception import get_full_snapshot
from agent.memory import (
    init_db, query_narrative, log_trade,
    close_trade, get_trade_log, update_narrative_outcome,
    record_new_narrative, get_running_narratives,
)
from agent.detection import detect_narratives
from agent.decision import decide_all
from agent.execution import execute_decision, monitor_open_positions
from agent.fallback import (
    process_closed_fallback_trades,
    record_fallback_entry,
    run_fallback_scan,
)
from agent.writeback import process_closed_trades


# ─────────────────────────────────────────────
# Logging Setup
# ─────────────────────────────────────────────

LOG_PATH = LOG_DIR / "agent.log"
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

STATE_PATH = DATA_DIR / "agent_state.json"


def restore_state():
    """Restore cycle and narrative timing state after a restart."""
    source = STATE_PATH
    if not source.exists():
        snapshot = PROJECT_ROOT / "submission" / "agent_state_snapshot.json"
        if not snapshot.exists():
            return
        source = snapshot
    try:
        saved = json.loads(source.read_text(encoding="utf-8-sig"))
        for key in (
            "cycle_count",
            "active_narrative",
            "active_narrative_day",
            "waiting_to_enter",
            "days_to_wait",
        ):
            if key in saved:
                setattr(STATE, key, saved[key])
    except (OSError, ValueError, TypeError) as exc:
        log.warning(f"Could not restore agent state: {exc}")


def partition_open_trades(open_trades: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split open positions into the reserved narrative and fallback lanes."""
    narrative_trades = []
    fallback_trades = []
    for trade in open_trades:
        trade_type = trade.get("trade_type")
        if not trade_type:
            trade_type = (
                "fallback"
                if trade.get("narrative_tag", "").startswith("fallback_")
                else "narrative"
            )
        if trade_type == "fallback":
            fallback_trades.append(trade)
        else:
            narrative_trades.append(trade)
    return narrative_trades, fallback_trades


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

        if detections:
            log.info(f"  Detected: {[d.narrative_tag for d in detections]}")
        else:
            log.info("  No narratives detected")

        # ── Step 3: Decision ─────────────────────────────
        log.info("Step 3: Decision")
        open_trades = get_trade_log(status="open")

        MAX_POSITIONS = 3
        MAX_NARRATIVE_POSITIONS = 1
        MAX_FALLBACK_POSITIONS = 2
        open_symbols = [t["symbol"] for t in open_trades]
        narrative_trades, fallback_trades = partition_open_trades(open_trades)

        if len(open_trades) >= MAX_POSITIONS:
            log.info(f"  {len(open_trades)} open position(s) — max {MAX_POSITIONS} reached, skipping")
            STATE.last_decision = None
        elif detections and len(narrative_trades) < MAX_NARRATIVE_POSITIONS:
            decisions = decide_all(detections, snapshot.get("sentiment", {}))
            STATE.last_decision = decisions[0] if decisions else None

            if STATE.last_decision and STATE.last_decision.should_enter:
                d = STATE.last_decision
                try:
                    market_change = float(
                        snapshot.get("market_intel", {}).get("market_cap_change_24h")
                    )
                    fear_greed = float(
                        snapshot.get("sentiment", {}).get("fear_greed_value")
                    )
                except (TypeError, ValueError):
                    market_change = None
                    fear_greed = None
                if (
                    d.side == "long"
                    and market_change is not None
                    and fear_greed is not None
                    and market_change <= -2.0
                    and fear_greed <= 30
                ):
                    log.info(
                        f"  Narrative long deferred: broad market {market_change:.1f}% "
                        f"with F&G={fear_greed:.0f}"
                    )
                    STATE.last_decision = None
                    d = None
                # Skip if already in this symbol
                if d and d.symbol in open_symbols:
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
                if d and STATE.active_narrative != d.narrative_tag:
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
                elif d:
                    STATE.active_narrative_day += 1
                    log.info(f"  Narrative day {STATE.active_narrative_day} / "
                             f"wait {STATE.days_to_wait}")
        else:
            STATE.last_decision = None
            if detections and narrative_trades:
                log.info("  Narrative slot occupied; monitoring the existing narrative trade")

        fallback_signal = None
        if (
            len(open_trades) < MAX_POSITIONS
            and len(fallback_trades) < MAX_FALLBACK_POSITIONS
        ):
            log.info(
                f"  Fallback capacity: {len(fallback_trades)}/{MAX_FALLBACK_POSITIONS}; "
                "running scout"
            )
            fear_greed = snapshot.get("sentiment", {}).get("fear_greed_value")
            fallback_signal = run_fallback_scan(
                fear_greed=fear_greed,
                open_trades=open_trades,
            )
            if fallback_signal:
                log.info(f"  Fallback signal: {fallback_signal.symbol} | "
                         f"rule={fallback_signal.rule} | score={fallback_signal.score:.2f}")
            else:
                log.info("  No fallback signal found this cycle")
        else:
            log.info(
                f"  Fallback slots full: {len(fallback_trades)}/{MAX_FALLBACK_POSITIONS}"
            )

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
        if fallback_signal and fallback_signal.symbol not in open_symbols:
            # Execute fallback strategy signal
            log.info(f"  Executing fallback: {fallback_signal.symbol} | {fallback_signal.rule}")
            current_price = fallback_signal.last_price
            if fallback_signal.side == "short":
                stop_loss_price = round(
                    current_price * (1 + fallback_signal.stop_loss_pct / 100), 8
                )
                take_profit_price = round(
                    current_price * (1 - fallback_signal.take_profit_pct / 100), 8
                )
            else:
                stop_loss_price = round(
                    current_price * (1 - fallback_signal.stop_loss_pct / 100), 8
                )
                take_profit_price = round(
                    current_price * (1 + fallback_signal.take_profit_pct / 100), 8
                )

            trade_id = log_trade(
                narrative_tag=f"fallback_{fallback_signal.rule}",
                symbol=fallback_signal.symbol,
                side=fallback_signal.side,
                entry_price=current_price,
                position_size=fallback_signal.position_size,
                memory_informed=False,
                notes=fallback_signal.reason,
                stop_loss_price=stop_loss_price,
                take_profit_price=take_profit_price,
                initial_risk_pct=fallback_signal.stop_loss_pct,
                trade_type="fallback",
            )
            record_fallback_entry()
            log.info(
                f"  Fallback trade logged: {fallback_signal.symbol} {fallback_signal.side} @ "
                f"{current_price} (id={trade_id})"
            )
        elif not (STATE.last_decision and STATE.last_decision.should_enter):
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
        fallback_updates = process_closed_fallback_trades(
            get_trade_log(status="closed")
        )
        if fallback_updates:
            log.info(
                f"  Updated fallback learning from "
                f"{fallback_updates} closed trade(s)"
            )
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


def monitor_positions_job():
    """Refresh marks and enforce exits independently of the hourly scan."""
    try:
        open_count = len(get_trade_log(status="open"))
        if not open_count:
            return
        log.info(f"Live monitor: refreshing {open_count} open position(s)")
        closed = monitor_open_positions(close_trade_fn=close_trade)
        process_closed_fallback_trades(closed)
        for trade in closed:
            log.info(
                f"Live monitor closed {trade['symbol']} "
                f"{trade['exit_reason']} PnL={trade['pnl_pct']:.2f}%"
            )
    except Exception:
        log.exception("Live position monitor failed")


# ─────────────────────────────────────────────
# Entry Point
# ─────────────────────────────────────────────

def main():
    log.info("Narrative Memory Agent starting...")
    log.info(f"   Log: {LOG_PATH}")
    log.info(f"   DB:  {DATA_DIR / 'memory.db'}")

    # Init DB
    init_db()
    restore_state()

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
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        monitor_positions_job,
        trigger=IntervalTrigger(minutes=1),
        id="position_monitor",
        name="Live Position Monitor",
        misfire_grace_time=30,
        max_instances=1,
        coalesce=True,
    )

    log.info("Scheduler started — running every hour. Press Ctrl+C to stop.")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        log.info("Agent stopped by user")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
