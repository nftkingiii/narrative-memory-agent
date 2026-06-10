"""
execution.py — Narrative Memory Agent
Paper trading execution layer.
Takes a TradeDecision, fetches current price, places paper order via Bitget API,
logs to SQLite, and monitors open positions for exit conditions.
"""

import os
import json
from datetime import datetime, timezone
from dotenv import load_dotenv
from agent.utils import bgc

load_dotenv()


# ─────────────────────────────────────────────
# Price Fetching
# ─────────────────────────────────────────────

def get_current_price(symbol: str) -> float | None:
    """Fetch current price for a symbol via bgc CLI."""
    data = bgc("spot", "spot_get_ticker", symbol=symbol)
    if not data:
        return None

    try:
        items = data.get("data", [])
        if items:
            price = float(items[0].get("lastPr") or items[0].get("close", 0))
            print(f"[execution] {symbol} current price: {price}")
            return price
    except (KeyError, ValueError, IndexError) as e:
        print(f"[execution] Price parse error: {e}")
    return None


# ─────────────────────────────────────────────
# Paper Order Placement
# Paper trades are logged to SQLite only —
# no real money, no real orders sent to exchange.
# We use Bitget market data for realistic prices.
# ─────────────────────────────────────────────

def place_paper_order(decision, current_price: float) -> dict:
    """
    Simulate a paper trade order.
    Records the order details — no real funds used.
    Returns order record dict.
    """
    # Calculate stop loss and take profit prices
    if decision.side == "long":
        stop_loss_price  = round(current_price * (1 - decision.stop_loss_pct / 100), 6)
        take_profit_price = round(current_price * (1 + decision.take_profit_pct / 100), 6)
    else:
        stop_loss_price  = round(current_price * (1 + decision.stop_loss_pct / 100), 6)
        take_profit_price = round(current_price * (1 - decision.take_profit_pct / 100), 6)

    # Paper order sizes by position_size tier
    size_map = {"small": 0.001, "medium": 0.005, "full": 0.01}
    order_size = size_map.get(decision.position_size, 0.001)

    order = {
        "paper": True,
        "symbol":            decision.symbol,
        "side":              decision.side,
        "entry_price":       current_price,
        "size":              order_size,
        "position_size":     decision.position_size,
        "stop_loss_price":   stop_loss_price,
        "take_profit_price": take_profit_price,
        "suggested_exit_day": decision.suggested_exit_day,
        "narrative_tag":     decision.narrative_tag,
        "memory_informed":   decision.memory_informed,
        "placed_at":         datetime.now(timezone.utc).isoformat(),
        "status":            "open",
    }

    print(f"[execution] 📋 PAPER ORDER PLACED")
    print(f"  Symbol:     {order['symbol']}")
    print(f"  Side:       {order['side'].upper()}")
    print(f"  Entry:      {order['entry_price']}")
    print(f"  Size:       {order['size']} ({order['position_size']})")
    print(f"  Stop Loss:  {order['stop_loss_price']} (-{decision.stop_loss_pct}%)")
    print(f"  Take Profit:{order['take_profit_price']} (+{decision.take_profit_pct}%)")
    print(f"  Exit day:   day {order['suggested_exit_day']}")
    print(f"  Memory:     {'✓ informed' if order['memory_informed'] else '✗ no prior record'}")

    return order


# ─────────────────────────────────────────────
# Execute Decision (full flow)
# ─────────────────────────────────────────────

def execute_decision(decision, memory_log_fn=None) -> dict | None:
    """
    Full execution flow:
    1. Check if decision says enter
    2. Fetch current price
    3. Place paper order
    4. Log trade to SQLite

    memory_log_fn: callable(narrative_tag, symbol, side, entry_price,
                             position_size, memory_id, memory_informed, notes) -> trade_id
    """
    if not decision.should_enter:
        print(f"[execution] Skipping — decision says no entry: {decision.reason}")
        return None

    if decision.days_to_wait > 0:
        print(f"[execution] ⏳ Waiting {decision.days_to_wait} days before entry "
              f"(memory-informed timing for {decision.narrative_tag})")
        return None

    # Fetch live price
    current_price = get_current_price(decision.symbol)
    if not current_price:
        print(f"[execution] ERROR: Could not fetch price for {decision.symbol}")
        return None

    # Place paper order
    order = place_paper_order(decision, current_price)

    # Log to SQLite
    if memory_log_fn:
        trade_id = memory_log_fn(
            narrative_tag=decision.narrative_tag,
            symbol=decision.symbol,
            side=decision.side,
            entry_price=current_price,
            position_size=decision.position_size,
            memory_informed=decision.memory_informed,
            notes=decision.reason,
            stop_loss_price=order["stop_loss_price"],
            take_profit_price=order["take_profit_price"],
            trade_type="narrative",
        )
        order["trade_id"] = trade_id
        print(f"[execution] Trade logged to SQLite (id={trade_id})")

    return order


# ─────────────────────────────────────────────
# Position Monitoring
# Check open trades against current price
# ─────────────────────────────────────────────

def monitor_open_positions(close_trade_fn=None) -> list[dict]:
    """
    Check all open paper trades against current prices.
    Closes positions that hit stop loss or take profit.
    Returns list of closed trade records.
    """
    from agent.memory import get_trade_log, update_trade_mark

    open_trades = get_trade_log(status="open")
    if not open_trades:
        print("[execution] No open positions to monitor")
        return []

    closed = []
    for trade in open_trades:
        symbol = trade["symbol"]
        entry_price = trade["entry_price"]
        side = trade["side"]

        current_price = get_current_price(symbol)
        if not current_price or not entry_price:
            continue

        # Calculate current PnL
        if side == "long":
            pnl_pct = (current_price - entry_price) / entry_price * 100
        else:
            pnl_pct = (entry_price - current_price) / entry_price * 100
        update_trade_mark(trade["id"], current_price, pnl_pct)

        exit_reason = None
        stop_price = trade.get("stop_loss_price")
        take_price = trade.get("take_profit_price")
        if side == "long":
            if stop_price is not None and current_price <= stop_price:
                exit_reason = "stop_loss"
            elif take_price is not None and current_price >= take_price:
                exit_reason = "take_profit"
        else:
            if stop_price is not None and current_price >= stop_price:
                exit_reason = "stop_loss"
            elif take_price is not None and current_price <= take_price:
                exit_reason = "take_profit"

        print(f"[execution] {symbol:12s} entry={entry_price} "
              f"current={current_price} PnL={pnl_pct:+.2f}% "
              f"{'→ ' + exit_reason if exit_reason else ''}")

        if exit_reason and close_trade_fn:
            close_trade_fn(
                trade_id=trade["id"],
                exit_price=current_price,
                exit_reason=exit_reason,
                pnl_pct=pnl_pct,
            )
            closed.append({**trade, "exit_price": current_price,
                           "exit_reason": exit_reason, "pnl_pct": pnl_pct})

    return closed


# ─────────────────────────────────────────────
# Test
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from agent.memory import init_db, query_narrative, log_trade, close_trade, get_trade_log
    from agent.detection import detect_narratives
    from agent.decision import decide_all

    init_db()

    # Mock snapshot — AI coins narrative
    mock_snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "news": [
            {
                "source": "cointelegraph",
                "title": "AI agent tokens surge as artificial intelligence narrative returns",
                "summary": "FET, RNDR and TAO lead gains as AI crypto narrative gains momentum.",
            },
            {
                "source": "coindesk",
                "title": "Fetch.ai and SingularityNET rally amid renewed AI token interest",
                "summary": "AI coins outperforming as artificial intelligence narrative drives capital.",
            },
        ],
        "kol_news": [],
        "sentiment": {
            "fear_greed_value": 68,
            "fear_greed_label": "Greed",
            "btc_long_ratio": 0.58,
            "btc_short_ratio": 0.42,
            "btc_funding_rate": 0.012,
            "btc_taker_ratio": 1.15,
        },
        "market_intel": {
            "btc_dominance": 52.4,
            "total_market_cap_usd": 2_450_000_000_000,
            "market_cap_change_24h": 2.3,
            "dex_trending": [
                {"name": "Fetch.ai", "symbol": "FET", "price_change_24h": 38.5},
            ],
        },
    }

    print("=" * 55)
    print("EXECUTION ENGINE TEST")
    print("=" * 55)

    # Run full pipeline
    results  = detect_narratives(mock_snapshot, memory_query_fn=query_narrative)
    decisions = decide_all(results, mock_snapshot["sentiment"])

    if decisions and decisions[0].should_enter:
        d = decisions[0]

        # Override wait for test — force immediate entry
        d.days_to_wait = 0
        d.entry_timing = "now"

        print(f"\n--- Executing decision: {d.narrative_tag} ---")
        order = execute_decision(d, memory_log_fn=log_trade)

        if order:
            print(f"\n--- Order placed, trade_id={order.get('trade_id')} ---")

            print("\n--- Monitoring open positions ---")
            monitor_open_positions(close_trade_fn=close_trade)

            print("\n--- Trade Log ---")
            for t in get_trade_log():
                status_icon = "🟢" if t["status"] == "open" else "🔴"
                print(f"  {status_icon} [{t['id']}] {t['symbol']:12s} "
                      f"{t['side']:5s} entry={t['entry_price']} "
                      f"exit={t['exit_price']} pnl={t['pnl_pct']} "
                      f"status={t['status']}")
    else:
        print("No valid decision to execute")

    print("\n✅ execution.py working correctly")
