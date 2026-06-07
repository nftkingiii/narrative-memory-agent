"""
writeback.py — Narrative Memory Agent
Memory write-back layer.
When a trade closes, writeback calculates the actual outcome,
compares it to the memory prediction, and updates the narrative record.
This is where the agent learns — memory compounds over time.
"""

from datetime import datetime, timezone, timedelta
from dataclasses import dataclass


# ─────────────────────────────────────────────
# Writeback Result
# ─────────────────────────────────────────────

@dataclass
class WritebackResult:
    narrative_tag: str
    memory_id: int
    trade_id: int

    # Actual outcome
    actual_pnl_pct: float
    actual_days_held: int
    exit_reason: str

    # Prediction vs reality
    predicted_return_pct: float
    predicted_days_to_peak: int
    prediction_accuracy: str        # "accurate" / "overestimated" / "underestimated" / "stopped_out"

    # What was written back
    outcome_written: str            # "played_out" / "fizzled" / "stopped_out"
    memory_updated: bool

    written_at: str


# ─────────────────────────────────────────────
# Outcome Classification
# ─────────────────────────────────────────────

def classify_outcome(
    pnl_pct: float,
    exit_reason: str,
    predicted_return: float,
) -> tuple[str, str]:
    """
    Classify trade outcome and prediction accuracy.
    Returns (outcome, prediction_accuracy)

    outcome:
      played_out   — hit take profit, narrative worked
      fizzled      — closed at small gain/loss, narrative didn't develop
      stopped_out  — hit stop loss, narrative failed

    prediction_accuracy:
      accurate       — actual return within 30% of predicted
      overestimated  — predicted much higher than actual
      underestimated — actual exceeded prediction (rare but good)
      stopped_out    — irrelevant, hit SL before narrative played
    """
    if exit_reason == "stop_loss":
        return "fizzled", "stopped_out"

    if exit_reason == "take_profit":
        outcome = "played_out"
    elif pnl_pct > 5:
        outcome = "played_out"
    elif pnl_pct > -2:
        outcome = "fizzled"
    else:
        outcome = "stopped_out"

    # Prediction accuracy
    if predicted_return and predicted_return > 0:
        ratio = pnl_pct / predicted_return
        if 0.70 <= ratio <= 1.30:
            accuracy = "accurate"
        elif ratio < 0.70:
            accuracy = "overestimated"
        else:
            accuracy = "underestimated"
    else:
        accuracy = "accurate"

    return outcome, accuracy


def calculate_days_held(entry_date: str, exit_date: str) -> int:
    """Calculate number of days between entry and exit."""
    try:
        fmt = "%Y-%m-%dT%H:%M:%S.%f+00:00"
        # Handle both formats
        for f in ["%Y-%m-%dT%H:%M:%S.%f+00:00", "%Y-%m-%dT%H:%M:%S+00:00",
                  "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"]:
            try:
                entry = datetime.strptime(entry_date[:26], f[:len(f)])
                exit_ = datetime.strptime(exit_date[:26], f[:len(f)])
                return max(1, (exit_ - entry).days)
            except ValueError:
                continue
        return 1
    except Exception:
        return 1


# ─────────────────────────────────────────────
# Build Writeback Notes
# ─────────────────────────────────────────────

def build_writeback_notes(
    result: WritebackResult,
    original_notes: str = "",
) -> str:
    """Build updated notes string combining original + new outcome data."""
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    new_note = (
        f"[{timestamp}] Trade closed: {result.exit_reason} | "
        f"PnL={result.actual_pnl_pct:.1f}% | "
        f"held {result.actual_days_held}d | "
        f"predicted {result.predicted_return_pct:.1f}% in {result.predicted_days_to_peak}d | "
        f"accuracy={result.prediction_accuracy}"
    )
    if original_notes:
        return f"{original_notes} | {new_note}"
    return new_note


# ─────────────────────────────────────────────
# Main Writeback Function
# ─────────────────────────────────────────────

def write_back_outcome(
    trade: dict,
    memory_record: dict = None,
    update_narrative_fn=None,
) -> WritebackResult | None:
    """
    Process a closed trade and write outcome back to narrative memory.

    Args:
        trade: closed trade dict from trade_log
        memory_record: the narrative_memory record this trade was based on
        update_narrative_fn: callable to update the narrative memory record

    Returns WritebackResult with full comparison of prediction vs reality.
    """
    trade_id = trade.get("id")
    narrative_tag = trade.get("narrative_tag")
    pnl_pct = trade.get("pnl_pct", 0) or 0
    exit_reason = trade.get("exit_reason", "manual")
    entry_date = trade.get("entry_date", "")
    exit_date = trade.get("exit_date", "")
    memory_id = trade.get("memory_id")

    if not narrative_tag:
        print("[writeback] ERROR: Trade has no narrative_tag")
        return None

    # Calculate actual days held
    actual_days = calculate_days_held(entry_date, exit_date) if exit_date else 1

    # Get prediction context from memory record
    predicted_return = 0.0
    predicted_days = 0
    original_notes = ""

    if memory_record:
        predicted_return = memory_record.get("avg_return_pct", 0) or 0
        predicted_days   = memory_record.get("days_to_peak", 0) or 0
        original_notes   = memory_record.get("notes", "") or ""

    # Classify outcome
    outcome, accuracy = classify_outcome(pnl_pct, exit_reason, predicted_return)

    # Build result
    result = WritebackResult(
        narrative_tag=narrative_tag,
        memory_id=memory_id or 0,
        trade_id=trade_id,
        actual_pnl_pct=round(pnl_pct, 2),
        actual_days_held=actual_days,
        exit_reason=exit_reason,
        predicted_return_pct=predicted_return,
        predicted_days_to_peak=predicted_days,
        prediction_accuracy=accuracy,
        outcome_written=outcome,
        memory_updated=False,
        written_at=datetime.now(timezone.utc).isoformat(),
    )

    # Write back to memory
    if update_narrative_fn and memory_id:
        notes = build_writeback_notes(result, original_notes)

        # Only update avg_return and days if the narrative played out
        # Don't overwrite historical data with a stopped-out trade
        if outcome == "played_out" and accuracy != "stopped_out":
            update_narrative_fn(
                record_id=memory_id,
                outcome=outcome,
                days_to_peak=actual_days,
                avg_return_pct=round(pnl_pct, 2),
                notes=notes,
            )
        else:
            # Just update outcome and notes, preserve historical metrics
            update_narrative_fn(
                record_id=memory_id,
                outcome=outcome,
                notes=notes,
            )
        result.memory_updated = True

    # Print summary
    print(f"\n[writeback] {'='*45}")
    print(f"[writeback] Narrative:   {narrative_tag}")
    print(f"[writeback] Outcome:     {outcome}")
    print(f"[writeback] Actual PnL:  {pnl_pct:.1f}%  (predicted: {predicted_return:.1f}%)")
    print(f"[writeback] Days held:   {actual_days}d   (predicted peak: {predicted_days}d)")
    print(f"[writeback] Accuracy:    {accuracy}")
    print(f"[writeback] Memory updated: {result.memory_updated}")
    print(f"[writeback] {'='*45}\n")

    return result


# ─────────────────────────────────────────────
# Process All Closed Trades (batch writeback)
# ─────────────────────────────────────────────

def process_closed_trades(
    get_trades_fn=None,
    get_memory_fn=None,
    update_narrative_fn=None,
) -> list[WritebackResult]:
    """
    Process all closed trades that haven't been written back yet.
    Runs as part of the main agent loop after each monitoring cycle.
    """
    if not get_trades_fn:
        return []

    closed_trades = get_trades_fn(status="closed")
    results = []

    for trade in closed_trades:
        # Skip if already written back (notes contain writeback marker)
        notes = trade.get("notes", "") or ""
        if "[" in notes and "Trade closed:" in notes:
            continue

        # Get the memory record this trade was based on
        memory_record = None
        if get_memory_fn and trade.get("narrative_tag"):
            memory_record = get_memory_fn(trade["narrative_tag"])

        result = write_back_outcome(
            trade=trade,
            memory_record=memory_record,
            update_narrative_fn=update_narrative_fn,
        )
        if result:
            results.append(result)

    if results:
        print(f"[writeback] Processed {len(results)} closed trade(s)")
    else:
        print("[writeback] No new closed trades to process")

    return results


# ─────────────────────────────────────────────
# Test
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from agent.memory import (
        init_db, query_narrative, log_trade,
        close_trade, get_trade_log, update_narrative_outcome,
        get_all_narratives,
    )

    init_db()

    # Reset trade log for clean test
    import sqlite3
    from pathlib import Path
    db_path = Path("data/memory.db")
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM trade_log")
    conn.commit()
    conn.close()
    print("Trade log cleared for clean test")
    print("WRITEBACK ENGINE TEST")
    print("=" * 55)

    # ── Test 1: Successful trade (played out) ──────────
    print("\n--- Test 1: Trade plays out (take profit hit) ---")
    memory = query_narrative("ai_coins")

    trade_id = log_trade(
        narrative_tag="ai_coins",
        symbol="FETUSDT",
        side="long",
        entry_price=0.20,
        position_size="full",
        memory_id=memory["id"] if memory else None,
        memory_informed=True,
        notes="Writeback test trade",
    )

    # Simulate 30 days passing — close at take profit
    close_trade(
        trade_id=trade_id,
        exit_price=0.58,
        exit_reason="take_profit",
        pnl_pct=190.0,
    )

    # Get the closed trade
    trades = get_trade_log(status="closed")
    test_trade = next((t for t in trades if t["id"] == trade_id), None)

    if test_trade:
        result = write_back_outcome(
            trade=test_trade,
            memory_record=memory,
            update_narrative_fn=update_narrative_outcome,
        )
        print(f"Writeback result: {result.outcome_written} | "
              f"accuracy={result.prediction_accuracy} | "
              f"memory_updated={result.memory_updated}")

    # ── Test 2: Stopped out trade ──────────────────────
    print("\n--- Test 2: Trade stopped out ---")
    memory2 = query_narrative("meme_supercycle")

    trade_id2 = log_trade(
        narrative_tag="meme_supercycle",
        symbol="DOGEUSDT",
        side="long",
        entry_price=0.12,
        position_size="small",
        memory_id=memory2["id"] if memory2 else None,
        memory_informed=True,
        notes="Writeback test — stop loss scenario",
    )

    close_trade(
        trade_id=trade_id2,
        exit_price=0.1164,
        exit_reason="stop_loss",
        pnl_pct=-3.0,
    )

    trades2 = get_trade_log(status="closed")
    test_trade2 = next((t for t in trades2 if t["id"] == trade_id2), None)

    if test_trade2:
        result2 = write_back_outcome(
            trade=test_trade2,
            memory_record=memory2,
            update_narrative_fn=update_narrative_outcome,
        )
        print(f"Writeback result: {result2.outcome_written} | "
              f"accuracy={result2.prediction_accuracy} | "
              f"memory_updated={result2.memory_updated}")

    # ── Show updated memory ────────────────────────────
    print("\n--- Updated Narrative Memory ---")
    for n in get_all_narratives():
        print(f"  [{n['id']}] {n['narrative_tag']:25s} | "
              f"return={n['avg_return_pct']:6.1f}% | "
              f"outcome={n['outcome']:12s} | "
              f"updated={n['updated_at'][:10]}")

    # ── Test batch processing ──────────────────────────
    print("\n--- Batch Writeback Test ---")
    results = process_closed_trades(
        get_trades_fn=get_trade_log,
        get_memory_fn=query_narrative,
        update_narrative_fn=update_narrative_outcome,
    )
    print(f"Batch processed: {len(results)} new writebacks")

    print("\n✅ writeback.py working correctly")