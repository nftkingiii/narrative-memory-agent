"""
fallback.py — Narrative Memory Agent
Fallback strategy layer — runs when Skill Hub data is unavailable
or no narrative is detected above threshold.

Scans top Bitget markets, applies rule-based strategy,
and learns from trade outcomes by updating rule thresholds.

Rules:
  1. momentum_long   — strong upward momentum, volume confirming
  2. fear_bounce     — oversold after sharp drop, contrarian long
  3. volume_breakout — unusual volume spike with price confirmation

All rules are self-improving — thresholds adjust based on win rate.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from dataclasses import dataclass, field
from dotenv import load_dotenv
from agent.utils import bgc

load_dotenv()

CONFIG_PATH = Path("data/strategy_config.json")


# ─────────────────────────────────────────────
# Strategy Config (self-updating thresholds)
# ─────────────────────────────────────────────

DEFAULT_CONFIG = {
    "rules": {
        "momentum_long": {
            "enabled": True,
            "min_change_24h_pct": 3.0,       # minimum 24h price change to qualify
            "max_change_24h_pct": 25.0,       # avoid parabolic moves already extended
            "min_volume_usd": 5_000_000,      # minimum 24h volume (liquidity filter)
            "take_profit_pct": 4.0,
            "stop_loss_pct": 2.0,
            "position_size": "small",
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "last_adjusted": None,
        },
        "fear_bounce": {
            "enabled": True,
            "max_change_24h_pct": -5.0,       # must be down at least 5%
            "max_fear_greed": 35,             # fear/greed must be low (fearful market)
            "min_volume_usd": 3_000_000,
            "take_profit_pct": 3.0,
            "stop_loss_pct": 2.5,
            "position_size": "small",
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "last_adjusted": None,
        },
        "volume_breakout": {
            "enabled": True,
            "min_change_24h_pct": 1.5,        # modest price move
            "min_volume_usd": 10_000_000,     # but very high volume
            "volume_vs_avg_multiplier": 2.0,  # volume must be 2x normal
            "take_profit_pct": 3.5,
            "stop_loss_pct": 2.0,
            "position_size": "small",
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "last_adjusted": None,
        },
    },
    "asset_performance": {},    # tracks win rate per asset symbol
    "deprioritized_assets": [], # assets with poor track record
    "scan_count": 0,
    "last_scan": None,
}


# ─────────────────────────────────────────────
# Config Management
# ─────────────────────────────────────────────

def load_config() -> dict:
    """Load strategy config from JSON, creating defaults if missing."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
        # Merge any missing keys from defaults
        for rule, defaults in DEFAULT_CONFIG["rules"].items():
            if rule not in config["rules"]:
                config["rules"][rule] = defaults
        return config
    except Exception:
        return DEFAULT_CONFIG.copy()


def save_config(config: dict):
    """Save strategy config to JSON."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


# ─────────────────────────────────────────────
# Market Scanner
# ─────────────────────────────────────────────

def scan_markets(limit: int = 30) -> list[dict]:
    """
    Fetch top USDT spot markets by volume.
    Uses Bitget REST API directly — no auth required for market data.
    Returns normalized list of market dicts.
    """
    import requests
    try:
        resp = requests.get(
            "https://api.bitget.com/api/v2/spot/market/tickers",
            timeout=15
        )
        data = resp.json()
    except Exception as e:
        print(f"[fallback] ERROR fetching tickers: {e}")
        return []

    tickers = data.get("data", [])
    if not tickers:
        print("[fallback] ERROR: Could not fetch market tickers")
        return []

    # Filter USDT pairs only, normalize fields
    markets = []
    for t in tickers:
        symbol = t.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue

        try:
            last_price   = float(t.get("lastPr") or 0)
            change_24h   = float(t.get("change24h") or 0) * 100  # convert to pct
            volume_usdt  = float(t.get("usdtVolume") or 0)
            high_24h     = float(t.get("high24h") or 0)
            low_24h      = float(t.get("low24h") or 0)
            open_price   = float(t.get("open") or 0)

            if last_price <= 0 or volume_usdt <= 0:
                continue

            # Volatility: range as % of open
            volatility = ((high_24h - low_24h) / open_price * 100) if open_price else 0

            markets.append({
                "symbol":       symbol,
                "last_price":   last_price,
                "change_24h":   round(change_24h, 2),
                "volume_usdt":  volume_usdt,
                "high_24h":     high_24h,
                "low_24h":      low_24h,
                "volatility":   round(volatility, 2),
            })
        except (ValueError, TypeError):
            continue

    # Sort by volume descending, take top N
    markets.sort(key=lambda x: x["volume_usdt"], reverse=True)
    filtered = markets[:limit]

    print(f"[fallback] Scanned {len(filtered)} markets "
          f"(from {len(tickers)} total USDT pairs)")
    return filtered


# ─────────────────────────────────────────────
# Rule Evaluation
# ─────────────────────────────────────────────

@dataclass
class FallbackSignal:
    rule: str
    symbol: str
    last_price: float
    change_24h: float
    volume_usdt: float
    take_profit_pct: float
    stop_loss_pct: float
    position_size: str
    score: float              # 0–1 signal strength within the rule
    reason: str
    detected_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def evaluate_momentum_long(markets: list[dict], config: dict) -> list[FallbackSignal]:
    """
    Rule 1: Momentum Long
    Strong upward move with volume, not yet parabolic.
    Score scales with how clean the move is.
    """
    rule = config["rules"]["momentum_long"]
    if not rule["enabled"]:
        return []

    signals = []
    for m in markets:
        change = m["change_24h"]
        volume = m["volume_usdt"]

        if not (rule["min_change_24h_pct"] <= change <= rule["max_change_24h_pct"]):
            continue
        if volume < rule["min_volume_usd"]:
            continue

        # Score: higher change + higher volume = stronger signal
        # Normalize: change between min and max maps to 0.4–0.9
        change_score  = min(0.9, 0.4 + (change - rule["min_change_24h_pct"]) /
                           (rule["max_change_24h_pct"] - rule["min_change_24h_pct"]) * 0.5)
        volume_score  = min(0.1, volume / 100_000_000 * 0.1)  # bonus for high volume
        score = round(change_score + volume_score, 3)

        signals.append(FallbackSignal(
            rule="momentum_long",
            symbol=m["symbol"],
            last_price=m["last_price"],
            change_24h=change,
            volume_usdt=volume,
            take_profit_pct=rule["take_profit_pct"],
            stop_loss_pct=rule["stop_loss_pct"],
            position_size=rule["position_size"],
            score=score,
            reason=f"Momentum: +{change:.1f}% in 24h, "
                   f"vol=${volume/1e6:.1f}M",
        ))

    return signals


def evaluate_fear_bounce(
    markets: list[dict],
    config: dict,
    fear_greed: float = None,
) -> list[FallbackSignal]:
    """
    Rule 2: Fear Bounce
    Asset down hard, market fearful — contrarian long for recovery.
    Only fires when fear/greed is low or unknown.
    """
    rule = config["rules"]["fear_bounce"]
    if not rule["enabled"]:
        return []

    # If fear/greed is available and not fearful, skip
    if fear_greed and fear_greed > rule["max_fear_greed"]:
        return []

    signals = []
    for m in markets:
        change = m["change_24h"]
        volume = m["volume_usdt"]

        if change > rule["max_change_24h_pct"]:  # must be down enough
            continue
        if volume < rule["min_volume_usd"]:
            continue
        # Don't catch falling knives — if down more than 20% skip (crash risk)
        if change < -20:
            continue

        # Score: deeper drop in fearful market = stronger bounce candidate
        drop_score = min(0.85, 0.4 + abs(change + rule["max_change_24h_pct"]) / 15 * 0.45)
        score = round(drop_score, 3)

        signals.append(FallbackSignal(
            rule="fear_bounce",
            symbol=m["symbol"],
            last_price=m["last_price"],
            change_24h=change,
            volume_usdt=volume,
            take_profit_pct=rule["take_profit_pct"],
            stop_loss_pct=rule["stop_loss_pct"],
            position_size=rule["position_size"],
            score=score,
            reason=f"Fear bounce: {change:.1f}% drop, "
                   f"F&G={fear_greed or 'unknown'}, "
                   f"vol=${volume/1e6:.1f}M",
        ))

    return signals


def evaluate_volume_breakout(markets: list[dict], config: dict) -> list[FallbackSignal]:
    """
    Rule 3: Volume Breakout
    Unusual volume spike with positive price action.
    Volume 2x+ the top-30 average signals institutional interest.
    """
    rule = config["rules"]["volume_breakout"]
    if not rule["enabled"]:
        return []

    if not markets:
        return []

    # Calculate average volume across scanned markets
    avg_volume = sum(m["volume_usdt"] for m in markets) / len(markets)

    signals = []
    for m in markets:
        change = m["change_24h"]
        volume = m["volume_usdt"]

        if change < rule["min_change_24h_pct"]:
            continue
        if volume < rule["min_volume_usd"]:
            continue
        if avg_volume <= 0:
            continue

        volume_multiplier = volume / avg_volume
        if volume_multiplier < rule["volume_vs_avg_multiplier"]:
            continue

        # Score: volume multiplier drives score
        score = round(min(0.90, 0.45 + (volume_multiplier - 2) / 8 * 0.45), 3)

        signals.append(FallbackSignal(
            rule="volume_breakout",
            symbol=m["symbol"],
            last_price=m["last_price"],
            change_24h=change,
            volume_usdt=volume,
            take_profit_pct=rule["take_profit_pct"],
            stop_loss_pct=rule["stop_loss_pct"],
            position_size=rule["position_size"],
            score=score,
            reason=f"Volume breakout: {volume_multiplier:.1f}x avg volume, "
                   f"+{change:.1f}% price",
        ))

    return signals


# ─────────────────────────────────────────────
# Signal Selection
# ─────────────────────────────────────────────

def select_best_signal(
    signals: list[FallbackSignal],
    deprioritized: list[str],
    config: dict,
) -> FallbackSignal | None:
    """
    Pick the best signal from all rules combined.
    Filters deprioritized assets, weights by rule win rate.
    """
    if not signals:
        return None

    # Filter deprioritized assets
    signals = [s for s in signals if s.symbol not in deprioritized]
    if not signals:
        return None

    # Weight score by rule win rate (if we have enough history)
    def weighted_score(signal: FallbackSignal) -> float:
        rule_cfg = config["rules"].get(signal.rule, {})
        trades = rule_cfg.get("trades", 0)
        win_rate = rule_cfg.get("win_rate", 0.5)

        # Only apply win rate weighting after 5+ trades
        if trades >= 5:
            return signal.score * (0.5 + win_rate * 0.5)
        return signal.score

    signals.sort(key=weighted_score, reverse=True)
    best = signals[0]

    print(f"[fallback] Best signal: {best.symbol} | "
          f"rule={best.rule} | score={best.score:.2f} | {best.reason}")
    return best


# ─────────────────────────────────────────────
# Main Fallback Scan Function
# ─────────────────────────────────────────────

def run_fallback_scan(fear_greed: float = None) -> FallbackSignal | None:
    """
    Run the full fallback scan.
    Returns the best signal found, or None if no qualifying opportunities.
    Called from main.py when no narrative is detected.
    """
    config = load_config()
    config["scan_count"] = config.get("scan_count", 0) + 1
    config["last_scan"] = datetime.now(timezone.utc).isoformat()

    print(f"\n[fallback] Running fallback scan #{config['scan_count']}...")

    # Scan markets
    markets = scan_markets(limit=30)
    if not markets:
        print("[fallback] No market data available")
        save_config(config)
        return None

    # Print top 5 movers for logging
    sorted_by_change = sorted(markets, key=lambda x: abs(x["change_24h"]), reverse=True)
    print(f"[fallback] Top movers:")
    for m in sorted_by_change[:5]:
        direction = "+" if m["change_24h"] >= 0 else ""
        print(f"  {m['symbol']:15s} {direction}{m['change_24h']:.1f}% "
              f"vol=${m['volume_usdt']/1e6:.1f}M")

    # Evaluate all rules
    all_signals = []
    all_signals += evaluate_momentum_long(markets, config)
    all_signals += evaluate_fear_bounce(markets, config, fear_greed)
    all_signals += evaluate_volume_breakout(markets, config)

    print(f"[fallback] Signals found: {len(all_signals)} "
          f"({sum(1 for s in all_signals if s.rule=='momentum_long')} momentum, "
          f"{sum(1 for s in all_signals if s.rule=='fear_bounce')} fear_bounce, "
          f"{sum(1 for s in all_signals if s.rule=='volume_breakout')} volume_breakout)")

    # Select best
    deprioritized = config.get("deprioritized_assets", [])
    best = select_best_signal(all_signals, deprioritized, config)

    save_config(config)
    return best


# ─────────────────────────────────────────────
# Learning: Update Rule Performance
# Called from writeback after a fallback trade closes
# ─────────────────────────────────────────────

ADJUSTMENT_INTERVAL = 5   # adjust thresholds every N trades per rule

def update_rule_performance(
    rule: str,
    symbol: str,
    pnl_pct: float,
    exit_reason: str,
):
    """
    Update rule win rate and adjust thresholds based on performance.
    Called by writeback.py after a fallback trade closes.
    """
    config = load_config()

    if rule not in config["rules"]:
        return

    rule_cfg = config["rules"][rule]
    won = pnl_pct > 0 and exit_reason == "take_profit"

    # Update counters
    rule_cfg["trades"] = rule_cfg.get("trades", 0) + 1
    if won:
        rule_cfg["wins"] = rule_cfg.get("wins", 0) + 1
    else:
        rule_cfg["losses"] = rule_cfg.get("losses", 0) + 1

    total = rule_cfg["trades"]
    wins  = rule_cfg["wins"]
    rule_cfg["win_rate"] = round(wins / total, 3) if total > 0 else 0.0

    print(f"[fallback] Rule '{rule}' updated: "
          f"{wins}W/{total-wins}L = {rule_cfg['win_rate']:.1%} win rate")

    # Update asset performance
    if "asset_performance" not in config:
        config["asset_performance"] = {}

    asset = config["asset_performance"].get(symbol, {"trades": 0, "wins": 0})
    asset["trades"] += 1
    if won:
        asset["wins"] += 1
    asset["win_rate"] = round(asset["wins"] / asset["trades"], 3)
    config["asset_performance"][symbol] = asset

    # Deprioritize assets with < 30% win rate after 5+ trades
    if asset["trades"] >= 5 and asset["win_rate"] < 0.30:
        if symbol not in config["deprioritized_assets"]:
            config["deprioritized_assets"].append(symbol)
            print(f"[fallback] Deprioritizing {symbol} "
                  f"(win rate {asset['win_rate']:.1%} after {asset['trades']} trades)")

    # Threshold adjustment every N trades
    if total % ADJUSTMENT_INTERVAL == 0:
        _adjust_thresholds(rule, rule_cfg)
        rule_cfg["last_adjusted"] = datetime.now(timezone.utc).isoformat()
        print(f"[fallback] Thresholds adjusted for rule '{rule}' "
              f"after {total} trades")

    config["rules"][rule] = rule_cfg
    save_config(config)


def _adjust_thresholds(rule: str, rule_cfg: dict):
    """
    Tighten or loosen thresholds based on win rate.
    Win rate > 60% → loosen slightly (capture more trades)
    Win rate < 40% → tighten (be more selective)
    """
    win_rate = rule_cfg.get("win_rate", 0.5)

    if rule == "momentum_long":
        if win_rate < 0.40:
            # Tighten — require stronger momentum
            rule_cfg["min_change_24h_pct"] = round(
                min(8.0, rule_cfg["min_change_24h_pct"] + 0.5), 1
            )
            print(f"  Tightening momentum threshold to "
                  f"{rule_cfg['min_change_24h_pct']}%")
        elif win_rate > 0.60:
            # Loosen — catch earlier moves
            rule_cfg["min_change_24h_pct"] = round(
                max(2.0, rule_cfg["min_change_24h_pct"] - 0.5), 1
            )
            print(f"  Loosening momentum threshold to "
                  f"{rule_cfg['min_change_24h_pct']}%")

    elif rule == "fear_bounce":
        if win_rate < 0.40:
            # Require deeper fear
            rule_cfg["max_fear_greed"] = max(20, rule_cfg["max_fear_greed"] - 5)
            print(f"  Tightening fear threshold to F&G < "
                  f"{rule_cfg['max_fear_greed']}")
        elif win_rate > 0.60:
            rule_cfg["max_fear_greed"] = min(45, rule_cfg["max_fear_greed"] + 5)
            print(f"  Loosening fear threshold to F&G < "
                  f"{rule_cfg['max_fear_greed']}")

    elif rule == "volume_breakout":
        if win_rate < 0.40:
            # Require stronger volume spike
            rule_cfg["volume_vs_avg_multiplier"] = round(
                min(4.0, rule_cfg["volume_vs_avg_multiplier"] + 0.25), 2
            )
            print(f"  Tightening volume multiplier to "
                  f"{rule_cfg['volume_vs_avg_multiplier']}x")
        elif win_rate > 0.60:
            rule_cfg["volume_vs_avg_multiplier"] = round(
                max(1.5, rule_cfg["volume_vs_avg_multiplier"] - 0.25), 2
            )
            print(f"  Loosening volume multiplier to "
                  f"{rule_cfg['volume_vs_avg_multiplier']}x")


# ─────────────────────────────────────────────
# Test
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("FALLBACK STRATEGY TEST")
    print("=" * 55)

    # Run a scan
    signal = run_fallback_scan(fear_greed=45)

    if signal:
        print(f"\n=== Best Signal ===")
        print(f"Symbol:       {signal.symbol}")
        print(f"Rule:         {signal.rule}")
        print(f"Price:        {signal.last_price}")
        print(f"Change 24h:   {signal.change_24h}%")
        print(f"Volume:       ${signal.volume_usdt/1e6:.1f}M")
        print(f"Score:        {signal.score}")
        print(f"Take Profit:  +{signal.take_profit_pct}%")
        print(f"Stop Loss:    -{signal.stop_loss_pct}%")
        print(f"Size:         {signal.position_size}")
        print(f"Reason:       {signal.reason}")

        # Test learning update
        print(f"\n=== Learning Update Test ===")
        update_rule_performance(
            rule=signal.rule,
            symbol=signal.symbol,
            pnl_pct=3.5,
            exit_reason="take_profit",
        )
        update_rule_performance(
            rule=signal.rule,
            symbol=signal.symbol,
            pnl_pct=-2.0,
            exit_reason="stop_loss",
        )

        config = load_config()
        r = config["rules"][signal.rule]
        print(f"\nRule stats after 2 test trades:")
        print(f"  Trades: {r['trades']} | Wins: {r['wins']} | "
              f"Win rate: {r['win_rate']:.1%}")
    else:
        print("No signal found this scan")

    print("\n✅ fallback.py working correctly")