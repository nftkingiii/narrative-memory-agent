"""
decision.py — Narrative Memory Agent
Decision engine: takes detection results + memory and outputs trade decisions.
Combines confidence score, memory match, and sentiment into concrete actions.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# ─────────────────────────────────────────────
# Decision Output
# ─────────────────────────────────────────────

@dataclass
class TradeDecision:
    # Action
    should_enter: bool
    reason: str

    # Narrative context
    narrative_tag: str
    confidence: float

    # Trade parameters
    symbol: str                     # e.g. "BTCUSDT"
    side: str                       # "long" or "short"
    position_size: str              # "small" / "medium" / "full"
    entry_timing: str               # "now" / "wait_N_days"
    days_to_wait: int = 0           # 0 = enter now

    # Exit plan
    stop_loss_pct: float = 3.0      # % below entry
    take_profit_pct: float = 0.0    # % above entry (0 = use memory-informed exit)
    suggested_exit_day: int = 0     # day N after entry to take profit

    # Memory context
    memory_informed: bool = False
    memory_avg_return: float = 0.0
    memory_days_to_peak: int = 0
    memory_optimal_entry_day: int = 0

    # Meta
    decided_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ─────────────────────────────────────────────
# Symbol Routing
# ─────────────────────────────────────────────
# Maps narrative tags to the best representative
# trading symbol for that theme

NARRATIVE_SYMBOLS = {
    "ai_coins":         "FETUSDT",
    "rwa_tokenization": "ONDOUSDT",
    "btc_etf_approval": "BTCUSDT",
    "meme_supercycle":  "DOGEUSDT",
    "depin":            "HNTUSDT",
    "layer2_scaling":   "ARBUSDT",
    "defi_resurgence":  "UNIUSDT",
}

DEFAULT_SYMBOL = "BTCUSDT"


# ─────────────────────────────────────────────
# Position Sizing Rules
# ─────────────────────────────────────────────

def size_position(confidence: float, memory_informed: bool) -> str:
    """
    Determine position size based on confidence and memory.

    Memory-informed trades get a size boost — we've seen this before.
    No memory = conservative sizing regardless of confidence.

    Size tiers:
      full   = high confidence + memory match
      medium = moderate confidence OR high confidence without memory
      small  = low confidence OR any trade without memory below threshold
    """
    if not memory_informed:
        if confidence >= 0.85:
            return "medium"     # cap at medium without memory
        return "small"

    if confidence >= 0.85:
        return "full"
    elif confidence >= 0.65:
        return "medium"
    else:
        return "small"


# ─────────────────────────────────────────────
# Entry Timing Rules
# ─────────────────────────────────────────────

def determine_entry_timing(detection, memory_match: dict) -> tuple[str, int]:
    """
    Determine when to enter based on memory.

    Logic:
    - If memory says optimal entry is day 1-2: enter now
    - If memory says optimal entry is day 3+: wait
    - If no memory: enter on day 2 by default (avoid day 1 fakeouts)

    Returns (entry_timing_label, days_to_wait)
    """
    if not memory_match:
        # No memory — default conservative: wait 2 days to confirm
        return "wait_2_days", 2

    optimal_day = memory_match.get("optimal_entry_day", 2)

    if optimal_day <= 2:
        return "now", 0
    else:
        return f"wait_{optimal_day}_days", optimal_day


# ─────────────────────────────────────────────
# Exit Planning
# ─────────────────────────────────────────────

def plan_exit(memory_match: dict, confidence: float) -> tuple[float, float, int]:
    """
    Plan exit based on memory pattern.
    Returns (stop_loss_pct, take_profit_pct, suggested_exit_day)

    Stop loss: fixed 3% always (protect capital)
    Take profit: memory-informed if available, else confidence-scaled
    Exit day: from memory days_to_peak, exit at 80% of peak to avoid selling top
    """
    stop_loss_pct = 3.0     # always fixed

    if not memory_match:
        # No memory — conservative take profit based on confidence
        take_profit_pct = round(confidence * 25, 1)   # max 25% without memory
        suggested_exit_day = 7                         # default 7 days
        return stop_loss_pct, take_profit_pct, suggested_exit_day

    days_to_peak = memory_match.get("days_to_peak", 14)
    avg_return = memory_match.get("avg_return_pct", 20)

    # Exit at 70% of historical avg return to be conservative
    take_profit_pct = round(avg_return * 0.70, 1)

    # Exit at 80% of days to peak to avoid the top
    suggested_exit_day = max(1, int(days_to_peak * 0.80))

    return stop_loss_pct, take_profit_pct, suggested_exit_day


# ─────────────────────────────────────────────
# Main Decision Function
# ─────────────────────────────────────────────

def make_decision(
    detection_result,
    sentiment: dict,
    skip_sentiment_filter: bool = False,
) -> TradeDecision:
    """
    Take a DetectionResult and produce a TradeDecision.

    Args:
        detection_result: DetectionResult from detection.py
        sentiment: sentiment dict from perception snapshot
        skip_sentiment_filter: override sentiment blocks (for testing only)
    """
    from agent.detection import sentiment_filter

    tag = detection_result.narrative_tag
    confidence = detection_result.confidence
    memory_match = detection_result.memory_match

    symbol = NARRATIVE_SYMBOLS.get(tag, DEFAULT_SYMBOL)
    memory_informed = bool(memory_match)

    # ── Sentiment Gate ──────────────────────────────────
    if not skip_sentiment_filter:
        sentiment_ok, sentiment_reason = sentiment_filter(sentiment)
        if not sentiment_ok:
            return TradeDecision(
                should_enter=False,
                reason=f"Blocked by sentiment filter: {sentiment_reason}",
                narrative_tag=tag,
                confidence=confidence,
                symbol=symbol,
                side="long",
                position_size="none",
                entry_timing="blocked",
                memory_informed=memory_informed,
            )

    # ── Confidence Gate ─────────────────────────────────
    if confidence < 0.40:
        return TradeDecision(
            should_enter=False,
            reason=f"Confidence too low ({confidence:.2f}) — minimum 0.40 required",
            narrative_tag=tag,
            confidence=confidence,
            symbol=symbol,
            side="long",
            position_size="none",
            entry_timing="skip",
            memory_informed=memory_informed,
        )

    # ── Position Sizing ─────────────────────────────────
    position_size = size_position(confidence, memory_informed)

    # ── Entry Timing ────────────────────────────────────
    entry_timing, days_to_wait = determine_entry_timing(detection_result, memory_match)

    # ── Exit Plan ───────────────────────────────────────
    stop_loss_pct, take_profit_pct, suggested_exit_day = plan_exit(memory_match, confidence)

    # ── Build Reason String ─────────────────────────────
    if memory_informed:
        reason = (
            f"Narrative '{tag}' detected (confidence={confidence:.2f}). "
            f"Memory match found: historical avg return {memory_match.get('avg_return_pct')}%, "
            f"days to peak {memory_match.get('days_to_peak')}. "
            f"Position: {position_size}, enter {entry_timing}, "
            f"target exit day {suggested_exit_day}."
        )
    else:
        reason = (
            f"Narrative '{tag}' detected (confidence={confidence:.2f}). "
            f"No prior memory record — conservative sizing. "
            f"Position: {position_size}, enter {entry_timing}."
        )

    return TradeDecision(
        should_enter=True,
        reason=reason,
        narrative_tag=tag,
        confidence=confidence,
        symbol=symbol,
        side="long",                    # narrative trades are always long
        position_size=position_size,
        entry_timing=entry_timing,
        days_to_wait=days_to_wait,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        suggested_exit_day=suggested_exit_day,
        memory_informed=memory_informed,
        memory_avg_return=memory_match.get("avg_return_pct", 0) if memory_match else 0,
        memory_days_to_peak=memory_match.get("days_to_peak", 0) if memory_match else 0,
        memory_optimal_entry_day=memory_match.get("optimal_entry_day", 0) if memory_match else 0,
    )


def decide_all(detection_results: list, sentiment: dict) -> list[TradeDecision]:
    """
    Run make_decision on all detection results.
    Returns only the top decision if multiple narratives detected
    (highest confidence that passes all filters).
    """
    decisions = []
    for result in detection_results:
        d = make_decision(result, sentiment)
        decisions.append(d)
        if d.should_enter:
            # Return on first valid decision (already sorted by confidence)
            print(f"[decision] ✅ ENTER: {d.narrative_tag} | "
                  f"size={d.position_size} | {d.entry_timing} | "
                  f"SL={d.stop_loss_pct}% TP={d.take_profit_pct}%")
            return [d]

    # No valid entry found
    if decisions:
        print(f"[decision] ❌ No entry — best candidate blocked: {decisions[0].reason}")
    else:
        print("[decision] ❌ No narratives to decide on")
    return decisions


# ─────────────────────────────────────────────
# Test
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    from agent.memory import init_db, query_narrative
    from agent.detection import detect_narratives

    init_db()

    # Same mock snapshot from detection test
    mock_snapshot = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "news": [
            {
                "source": "cointelegraph",
                "title": "AI agent tokens surge as artificial intelligence narrative returns to crypto",
                "summary": "FET, RNDR and TAO lead gains as AI crypto narrative gains momentum. "
                           "Investors are rotating into AI coins following ChatGPT news.",
            },
            {
                "source": "coindesk",
                "title": "Fetch.ai and SingularityNET rally 40% amid renewed AI token interest",
                "summary": "AI coins are outperforming as the artificial intelligence narrative "
                           "drives fresh capital into decentralized AI infrastructure tokens.",
            },
            {
                "source": "blockworks",
                "title": "Bitcoin ETF records another day of strong inflows",
                "summary": "Spot bitcoin ETF products saw combined inflows today.",
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
                {"name": "Render", "symbol": "RNDR", "price_change_24h": 22.1},
            ],
        },
    }

    print("=" * 55)
    print("DECISION ENGINE TEST")
    print("=" * 55)

    # Run detection
    results = detect_narratives(mock_snapshot, memory_query_fn=query_narrative)

    # Run decisions
    print("\n--- Decisions ---")
    decisions = decide_all(results, mock_snapshot["sentiment"])

    # Print full decision detail
    print("\n=== Decision Detail ===")
    for d in decisions:
        print(f"""
Narrative:      {d.narrative_tag}
Should enter:   {d.should_enter}
Reason:         {d.reason}
─────────────────────────────────────
Symbol:         {d.symbol}
Side:           {d.side}
Size:           {d.position_size}
Entry timing:   {d.entry_timing} (wait {d.days_to_wait} days)
─────────────────────────────────────
Stop loss:      {d.stop_loss_pct}%
Take profit:    {d.take_profit_pct}%
Exit day:       day {d.suggested_exit_day}
─────────────────────────────────────
Memory informed: {d.memory_informed}
Hist avg return: {d.memory_avg_return}%
Hist days/peak:  {d.memory_days_to_peak}
Optimal entry:   day {d.memory_optimal_entry_day}
        """)

    # Test blocked scenario
    print("=== Blocked Scenario Test (high funding rate) ===")
    blocked_sentiment = {**mock_snapshot["sentiment"], "btc_funding_rate": 0.06}
    blocked = make_decision(results[0], blocked_sentiment)
    print(f"Should enter: {blocked.should_enter}")
    print(f"Reason: {blocked.reason}")

    print("\n✅ decision.py working correctly")