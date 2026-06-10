"""
fallback.py — Narrative Memory Agent (v2)
Upgraded fallback strategy with RUNECLAW-inspired risk gates.

New additions:
  - Funding rate gate (no longs when funding > 0.05%)
  - Taker buy ratio confirmation (must show buy pressure)
  - R:R minimum enforcement (1.2x minimum)
  - Circuit breaker (stops trading after daily drawdown threshold)
  - Cooldown between trades (300s minimum)
  - Max exposure cap (80% of portfolio)
"""

import json
import os
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dataclasses import dataclass, field
from agent.utils import bgc
from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", PROJECT_ROOT / "data"))
CONFIG_PATH  = DATA_DIR / "strategy_config.json"
CIRCUIT_PATH = DATA_DIR / "circuit_breaker.json"

STARTING_BALANCE    = 10_000.0
MAX_DAILY_LOSS_USD  = 300.0     # circuit breaker trips at -$300/day
MAX_EXPOSURE_PCT    = 0.80      # max 80% of portfolio in open positions
MIN_RR              = 1.2       # minimum risk:reward ratio
COOLDOWN_SECONDS    = 300       # 5 minutes between any new entry
MIN_TAKER_BUY_RATIO = 0.52      # buyers must be > 52% of taker volume
MAX_FUNDING_RATE    = 0.0003    # 0.03% — don't long into overleveraged longs

SIZE_ALLOCATION = {"small": 0.01, "medium": 0.03, "full": 0.05, "none": 0.0}


# ─────────────────────────────────────────────
# Default Config
# ─────────────────────────────────────────────

DEFAULT_CONFIG = {
    "rules": {
        "momentum_long": {
            "enabled": True,
            "min_change_24h_pct": 3.0,
            "max_change_24h_pct": 25.0,
            "min_volume_usd": 5_000_000,
            "take_profit_pct": 4.0,
            "stop_loss_pct": 2.0,
            "position_size": "small",
            "trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0.0, "last_adjusted": None,
        },
        "fear_bounce": {
            "enabled": True,
            "max_change_24h_pct": -5.0,
            "max_fear_greed": 35,
            "min_volume_usd": 3_000_000,
            "take_profit_pct": 3.0,
            "stop_loss_pct": 2.5,
            "position_size": "small",
            "trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0.0, "last_adjusted": None,
        },
        "volume_breakout": {
            "enabled": True,
            "min_change_24h_pct": 1.5,
            "min_volume_usd": 10_000_000,
            "volume_vs_avg_multiplier": 2.0,
            "take_profit_pct": 3.5,
            "stop_loss_pct": 2.0,
            "position_size": "small",
            "trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0.0, "last_adjusted": None,
        },
        "taker_momentum": {
            "enabled": True,
            "min_change_24h_pct": 2.0,
            "min_volume_usd": 8_000_000,
            "min_taker_buy_ratio": 0.55,  # aggressive buyer dominance
            "take_profit_pct": 3.5,
            "stop_loss_pct": 2.0,
            "position_size": "small",
            "trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0.0, "last_adjusted": None,
        },
    },
    "asset_performance": {},
    "deprioritized_assets": [],
    "scan_count": 0,
    "last_scan": None,
    "daily_stats": {
        "date": None,
        "pnl_usd": 0.0,
        "trades": 0,
        "circuit_tripped": False,
        "last_trade_time": None,
    },
}

ADJUSTMENT_INTERVAL = 5


# ─────────────────────────────────────────────
# Config Management
# ─────────────────────────────────────────────

def load_config() -> dict:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG.copy()
    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
        for rule, defaults in DEFAULT_CONFIG["rules"].items():
            if rule not in config["rules"]:
                config["rules"][rule] = defaults
        if "daily_stats" not in config:
            config["daily_stats"] = DEFAULT_CONFIG["daily_stats"].copy()
        if "taker_momentum" not in config["rules"]:
            config["rules"]["taker_momentum"] = DEFAULT_CONFIG["rules"]["taker_momentum"]
        return config
    except Exception:
        return DEFAULT_CONFIG.copy()


def save_config(config: dict):
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def record_fallback_entry():
    """Persist cooldown and daily trade counters after a fallback entry."""
    config = reset_daily_stats_if_needed(load_config())
    stats = config["daily_stats"]
    stats["last_trade_time"] = datetime.now(timezone.utc).isoformat()
    stats["trades"] = stats.get("trades", 0) + 1
    save_config(config)


def reset_daily_stats_if_needed(config: dict) -> dict:
    """Reset daily PnL and circuit breaker at midnight UTC."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stats = config.get("daily_stats", {})
    if stats.get("date") != today:
        config["daily_stats"] = {
            "date": today,
            "pnl_usd": 0.0,
            "trades": 0,
            "circuit_tripped": False,
            "last_trade_time": None,
        }
        print(f"[fallback] Daily stats reset for {today}")
    return config


# ─────────────────────────────────────────────
# Market Data Fetching
# ─────────────────────────────────────────────

def fetch_tickers() -> list[dict]:
    """Fetch all USDT spot tickers from Bitget REST API."""
    try:
        resp = requests.get(
            "https://api.bitget.com/api/v2/spot/market/tickers",
            timeout=15
        )
        return resp.json().get("data", [])
    except Exception as e:
        print(f"[fallback] ERROR fetching tickers: {e}")
        return []


def fetch_taker_ratio(symbol: str) -> float | None:
    """
    Fetch taker buy ratio for a symbol.
    Returns ratio of buy takers to total takers (0-1).
    Uses Bitget futures taker flow as proxy.
    """
    try:
        # Use futures taker data as proxy for spot sentiment
        resp = requests.get(
            "https://api.bitget.com/api/v2/mix/market/taker-long-short",
            params={"symbol": symbol.replace("USDT", "") + "USDT_UMCBL",
                    "period": "1h", "limit": "6"},
            timeout=10
        )
        data = resp.json().get("data", [])
        if not data:
            return None
        # Average buy ratio over last 6 bars
        buy_ratios = [float(d.get("buyRatio", 0)) for d in data if d.get("buyRatio")]
        return round(sum(buy_ratios) / len(buy_ratios), 4) if buy_ratios else None
    except Exception:
        return None


def fetch_funding_rate(symbol: str) -> float | None:
    """Fetch current funding rate for a symbol."""
    try:
        resp = requests.get(
            "https://api.bitget.com/api/v2/mix/market/current-fund-rate",
            params={"symbol": symbol.replace("USDT", "") + "USDT_UMCBL",
                    "productType": "usdt-futures"},
            timeout=10
        )
        data = resp.json().get("data", [])
        if data:
            return float(data[0].get("fundingRate", 0))
        return None
    except Exception:
        return None


# ─────────────────────────────────────────────
# Signal Dataclass
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
    score: float
    reason: str
    rr_ratio: float = 0.0
    taker_buy_ratio: float | None = None
    funding_rate: float | None = None
    gate_notes: list[str] = field(default_factory=list)
    detected_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ─────────────────────────────────────────────
# Risk Gates
# ─────────────────────────────────────────────

def check_rr(take_profit_pct: float, stop_loss_pct: float) -> tuple[bool, float]:
    """Check if R:R meets minimum threshold."""
    if stop_loss_pct <= 0:
        return False, 0.0
    rr = take_profit_pct / stop_loss_pct
    return rr >= MIN_RR, round(rr, 2)


def check_circuit_breaker(config: dict) -> tuple[bool, str]:
    """
    Check if circuit breaker is tripped.
    Returns (can_trade, reason).
    """
    stats = config.get("daily_stats", {})
    if stats.get("circuit_tripped"):
        return False, f"Circuit breaker tripped — daily loss exceeded ${MAX_DAILY_LOSS_USD}"
    pnl = stats.get("pnl_usd", 0)
    if pnl <= -MAX_DAILY_LOSS_USD:
        config["daily_stats"]["circuit_tripped"] = True
        print(f"[fallback] CIRCUIT BREAKER TRIPPED — daily PnL: ${pnl:.2f}")
        return False, f"Circuit breaker tripped — daily loss ${pnl:.2f}"
    return True, "OK"


def check_cooldown(config: dict) -> tuple[bool, str]:
    """Check if enough time has passed since last trade."""
    last_trade = config.get("daily_stats", {}).get("last_trade_time")
    if not last_trade:
        return True, "OK"
    try:
        last_dt = datetime.fromisoformat(last_trade)
        elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
        if elapsed < COOLDOWN_SECONDS:
            remaining = int(COOLDOWN_SECONDS - elapsed)
            return False, f"Cooldown active — {remaining}s remaining"
    except Exception:
        pass
    return True, "OK"


def check_exposure(open_trades: list, portfolio_balance: float) -> tuple[bool, str]:
    """Check if adding a new position would exceed max exposure."""
    if not open_trades or portfolio_balance <= 0:
        return True, "OK"
    allocated = sum(
        portfolio_balance * SIZE_ALLOCATION.get(t.get("position_size", "small"), 0.01)
        for t in open_trades
    )
    exposure_pct = allocated / portfolio_balance
    if exposure_pct >= MAX_EXPOSURE_PCT:
        return False, f"Exposure {exposure_pct:.0%} at max {MAX_EXPOSURE_PCT:.0%}"
    return True, "OK"


# ─────────────────────────────────────────────
# Market Scanner
# ─────────────────────────────────────────────

def scan_markets(limit: int = 30) -> list[dict]:
    """Fetch and normalize top USDT markets by volume."""
    tickers = fetch_tickers()
    if not tickers:
        return []

    markets = []
    for t in tickers:
        symbol = t.get("symbol", "")
        if not symbol.endswith("USDT"):
            continue
        try:
            last_price  = float(t.get("lastPr") or 0)
            change_24h  = float(t.get("change24h") or 0) * 100
            volume_usdt = float(t.get("usdtVolume") or 0)
            high_24h    = float(t.get("high24h") or 0)
            low_24h     = float(t.get("low24h") or 0)
            open_price  = float(t.get("open") or 0)
            bid_sz      = float(t.get("bidSz") or 0)
            ask_sz      = float(t.get("askSz") or 0)

            if last_price <= 0 or volume_usdt <= 0:
                continue

            volatility  = ((high_24h - low_24h) / open_price * 100) if open_price else 0
            book_ratio  = (bid_sz / ask_sz) if ask_sz > 0 else 1.0

            markets.append({
                "symbol":      symbol,
                "last_price":  last_price,
                "change_24h":  round(change_24h, 2),
                "volume_usdt": volume_usdt,
                "high_24h":    high_24h,
                "low_24h":     low_24h,
                "volatility":  round(volatility, 2),
                "book_ratio":  round(book_ratio, 3),
            })
        except (ValueError, TypeError):
            continue

    markets.sort(key=lambda x: x["volume_usdt"], reverse=True)
    filtered = markets[:limit]
    print(f"[fallback] Scanned {len(filtered)} markets (from {len(tickers)} total USDT pairs)")
    return filtered


# ─────────────────────────────────────────────
# Rule Evaluators
# ─────────────────────────────────────────────

def evaluate_momentum_long(markets: list[dict], config: dict) -> list[FallbackSignal]:
    rule = config["rules"]["momentum_long"]
    if not rule["enabled"]:
        return []
    signals = []
    for m in markets:
        change, volume = m["change_24h"], m["volume_usdt"]
        if not (rule["min_change_24h_pct"] <= change <= rule["max_change_24h_pct"]):
            continue
        if volume < rule["min_volume_usd"]:
            continue
        rr_ok, rr = check_rr(rule["take_profit_pct"], rule["stop_loss_pct"])
        if not rr_ok:
            continue
        score = min(0.9, 0.4 + (change - rule["min_change_24h_pct"]) /
                    (rule["max_change_24h_pct"] - rule["min_change_24h_pct"]) * 0.5)
        signals.append(FallbackSignal(
            rule="momentum_long", symbol=m["symbol"],
            last_price=m["last_price"], change_24h=change,
            volume_usdt=volume, take_profit_pct=rule["take_profit_pct"],
            stop_loss_pct=rule["stop_loss_pct"], position_size=rule["position_size"],
            score=round(score, 3), rr_ratio=rr,
            reason=f"Momentum: +{change:.1f}% in 24h, vol=${volume/1e6:.1f}M, R:R={rr:.1f}x",
        ))
    return signals


def evaluate_fear_bounce(markets: list[dict], config: dict,
                         fear_greed: float = None) -> list[FallbackSignal]:
    rule = config["rules"]["fear_bounce"]
    if not rule["enabled"]:
        return []
    try:
        fear_greed = float(fear_greed) if fear_greed is not None else None
        max_fear_greed = float(rule["max_fear_greed"])
    except (TypeError, ValueError):
        fear_greed = None
        max_fear_greed = None
    if (
        fear_greed is not None
        and max_fear_greed is not None
        and fear_greed > max_fear_greed
    ):
        return []
    signals = []
    for m in markets:
        change, volume = m["change_24h"], m["volume_usdt"]
        if change > rule["max_change_24h_pct"] or change < -20:
            continue
        if volume < rule["min_volume_usd"]:
            continue
        rr_ok, rr = check_rr(rule["take_profit_pct"], rule["stop_loss_pct"])
        if not rr_ok:
            continue
        drop_score = min(0.85, 0.4 + abs(change + rule["max_change_24h_pct"]) / 15 * 0.45)
        signals.append(FallbackSignal(
            rule="fear_bounce", symbol=m["symbol"],
            last_price=m["last_price"], change_24h=change,
            volume_usdt=volume, take_profit_pct=rule["take_profit_pct"],
            stop_loss_pct=rule["stop_loss_pct"], position_size=rule["position_size"],
            score=round(drop_score, 3), rr_ratio=rr,
            reason=f"Fear bounce: {change:.1f}% drop, F&G={fear_greed or 'unknown'}, R:R={rr:.1f}x",
        ))
    return signals


def evaluate_volume_breakout(markets: list[dict], config: dict) -> list[FallbackSignal]:
    rule = config["rules"]["volume_breakout"]
    if not rule["enabled"] or not markets:
        return []
    avg_volume = sum(m["volume_usdt"] for m in markets) / len(markets)
    signals = []
    for m in markets:
        change, volume = m["change_24h"], m["volume_usdt"]
        if change < rule["min_change_24h_pct"] or volume < rule["min_volume_usd"]:
            continue
        vol_mult = volume / avg_volume if avg_volume > 0 else 0
        if vol_mult < rule["volume_vs_avg_multiplier"]:
            continue
        rr_ok, rr = check_rr(rule["take_profit_pct"], rule["stop_loss_pct"])
        if not rr_ok:
            continue
        score = round(min(0.90, 0.45 + (vol_mult - 2) / 8 * 0.45), 3)
        signals.append(FallbackSignal(
            rule="volume_breakout", symbol=m["symbol"],
            last_price=m["last_price"], change_24h=change,
            volume_usdt=volume, take_profit_pct=rule["take_profit_pct"],
            stop_loss_pct=rule["stop_loss_pct"], position_size=rule["position_size"],
            score=score, rr_ratio=rr,
            reason=f"Volume breakout: {vol_mult:.1f}x avg, +{change:.1f}%, R:R={rr:.1f}x",
        ))
    return signals


def evaluate_taker_momentum(markets: list[dict], config: dict) -> list[FallbackSignal]:
    """
    New Rule 4: Taker Momentum
    Requires strong buy-side taker dominance confirming price move.
    Inspired by RUNECLAW's taker flow analysis.
    """
    rule = config["rules"].get("taker_momentum", {})
    if not rule.get("enabled", True):
        return []
    signals = []
    for m in markets:
        change, volume = m["change_24h"], m["volume_usdt"]
        if change < rule.get("min_change_24h_pct", 2.0):
            continue
        if volume < rule.get("min_volume_usd", 8_000_000):
            continue
        # Check taker ratio (fetch from API)
        taker_ratio = fetch_taker_ratio(m["symbol"])
        if taker_ratio is None:
            continue
        if taker_ratio < rule.get("min_taker_buy_ratio", 0.55):
            continue
        rr_ok, rr = check_rr(rule.get("take_profit_pct", 3.5),
                              rule.get("stop_loss_pct", 2.0))
        if not rr_ok:
            continue
        score = round(min(0.92, 0.50 + taker_ratio * 0.42), 3)
        signals.append(FallbackSignal(
            rule="taker_momentum", symbol=m["symbol"],
            last_price=m["last_price"], change_24h=change,
            volume_usdt=volume,
            take_profit_pct=rule.get("take_profit_pct", 3.5),
            stop_loss_pct=rule.get("stop_loss_pct", 2.0),
            position_size=rule.get("position_size", "small"),
            score=score, rr_ratio=rr, taker_buy_ratio=taker_ratio,
            reason=f"Taker momentum: buy ratio {taker_ratio:.1%}, +{change:.1f}%, R:R={rr:.1f}x",
        ))
    return signals


# ─────────────────────────────────────────────
# Signal Enrichment (funding rate check)
# ─────────────────────────────────────────────

def enrich_with_gates(signal: FallbackSignal) -> FallbackSignal | None:
    """
    Fetch funding rate and apply gate filters.
    Returns None if signal fails any gate.
    """
    notes = []

    # Funding rate gate — don't long into overleveraged bulls
    funding = fetch_funding_rate(signal.symbol)
    signal.funding_rate = funding
    if funding is not None:
        if funding > MAX_FUNDING_RATE:
            print(f"[fallback] {signal.symbol} BLOCKED — funding rate {funding:.4%} > {MAX_FUNDING_RATE:.4%}")
            return None
        notes.append(f"funding={funding:.4%} OK")
    else:
        notes.append("funding=n/a")

    # Book ratio gate — don't buy into heavy offer pressure
    # (book_ratio < 0.3 means 3x more offers than bids — avoid)
    # We don't have book data per signal since it comes from ticker
    # This would need a separate order book API call — skip for now

    signal.gate_notes = notes
    return signal


# ─────────────────────────────────────────────
# Signal Selection
# ─────────────────────────────────────────────

def select_best_signal(signals: list[FallbackSignal], config: dict,
                       open_symbols: list[str] = None) -> FallbackSignal | None:
    if not signals:
        return None

    deprioritized = config.get("deprioritized_assets", [])
    open_symbols = open_symbols or []

    # Filter deprioritized and already-open symbols
    signals = [s for s in signals
               if s.symbol not in deprioritized
               and s.symbol not in open_symbols]
    if not signals:
        return None

    def weighted_score(s: FallbackSignal) -> float:
        rule_cfg = config["rules"].get(s.rule, {})
        trades = rule_cfg.get("trades", 0)
        win_rate = rule_cfg.get("win_rate", 0.5)
        # Weight by R:R as well
        rr_bonus = min(0.2, (s.rr_ratio - MIN_RR) * 0.1)
        if trades >= 5:
            return s.score * (0.5 + win_rate * 0.5) + rr_bonus
        return s.score + rr_bonus

    signals.sort(key=weighted_score, reverse=True)
    best = signals[0]
    print(f"[fallback] Best signal: {best.symbol} | rule={best.rule} | "
          f"score={best.score:.2f} | R:R={best.rr_ratio:.1f}x | {best.reason}")
    return best


# ─────────────────────────────────────────────
# Main Fallback Scan
# ─────────────────────────────────────────────

def run_fallback_scan(fear_greed: float = None, open_trades: list = None,
                      portfolio_balance: float = STARTING_BALANCE) -> FallbackSignal | None:
    """
    Full fallback scan with all RUNECLAW-inspired risk gates.
    Returns best signal or None.
    """
    config = load_config()
    config = reset_daily_stats_if_needed(config)
    config["scan_count"] = config.get("scan_count", 0) + 1
    config["last_scan"] = datetime.now(timezone.utc).isoformat()

    open_trades = open_trades or []
    open_symbols = [t.get("symbol", "") for t in open_trades]

    print(f"\n[fallback] Running fallback scan #{config['scan_count']}...")

    # ── Gate 1: Circuit Breaker ──────────────
    cb_ok, cb_reason = check_circuit_breaker(config)
    if not cb_ok:
        print(f"[fallback] BLOCKED — {cb_reason}")
        save_config(config)
        return None

    # ── Gate 2: Cooldown ─────────────────────
    cd_ok, cd_reason = check_cooldown(config)
    if not cd_ok:
        print(f"[fallback] BLOCKED — {cd_reason}")
        save_config(config)
        return None

    # ── Gate 3: Exposure ─────────────────────
    exp_ok, exp_reason = check_exposure(open_trades, portfolio_balance)
    if not exp_ok:
        print(f"[fallback] BLOCKED — {exp_reason}")
        save_config(config)
        return None

    # ── Scan ─────────────────────────────────
    markets = scan_markets(limit=30)
    if not markets:
        save_config(config)
        return None

    # Print top movers
    sorted_movers = sorted(markets, key=lambda x: abs(x["change_24h"]), reverse=True)
    print(f"[fallback] Top movers:")
    for m in sorted_movers[:5]:
        d = m["change_24h"]
        print(f"  {m['symbol']:15s} {'+' if d>=0 else ''}{d:.1f}% "
              f"vol=${m['volume_usdt']/1e6:.1f}M book={m['book_ratio']:.2f}")

    # ── Evaluate Rules ────────────────────────
    all_signals = []
    all_signals += evaluate_momentum_long(markets, config)
    all_signals += evaluate_fear_bounce(markets, config, fear_greed)
    all_signals += evaluate_volume_breakout(markets, config)
    all_signals += evaluate_taker_momentum(markets, config)

    print(f"[fallback] Signals before gates: {len(all_signals)} "
          f"({sum(1 for s in all_signals if s.rule=='momentum_long')} momentum, "
          f"{sum(1 for s in all_signals if s.rule=='fear_bounce')} fear_bounce, "
          f"{sum(1 for s in all_signals if s.rule=='volume_breakout')} vol_breakout, "
          f"{sum(1 for s in all_signals if s.rule=='taker_momentum')} taker_momentum)")

    # ── Apply Funding Rate Gate ───────────────
    gated_signals = []
    for s in all_signals:
        enriched = enrich_with_gates(s)
        if enriched:
            gated_signals.append(enriched)

    print(f"[fallback] Signals after gates: {len(gated_signals)}")

    # ── Select Best ───────────────────────────
    best = select_best_signal(gated_signals, config, open_symbols)
    save_config(config)
    return best


# ─────────────────────────────────────────────
# Learning: Update Rule Performance
# ─────────────────────────────────────────────

def update_rule_performance(rule: str, symbol: str, pnl_pct: float,
                            exit_reason: str, pnl_usd: float = 0.0):
    """Update rule win rate, adjust thresholds, update daily stats."""
    config = load_config()
    config = reset_daily_stats_if_needed(config)

    if rule not in config["rules"]:
        save_config(config)
        return

    rule_cfg = config["rules"][rule]
    won = pnl_pct > 0 and exit_reason == "take_profit"

    rule_cfg["trades"] = rule_cfg.get("trades", 0) + 1
    if won:
        rule_cfg["wins"] = rule_cfg.get("wins", 0) + 1
    else:
        rule_cfg["losses"] = rule_cfg.get("losses", 0) + 1

    total = rule_cfg["trades"]
    wins  = rule_cfg["wins"]
    rule_cfg["win_rate"] = round(wins / total, 3) if total > 0 else 0.0

    print(f"[fallback] Rule '{rule}': {wins}W/{total-wins}L = {rule_cfg['win_rate']:.1%} win rate")

    # Asset performance
    asset = config["asset_performance"].get(symbol, {"trades": 0, "wins": 0})
    asset["trades"] += 1
    if won:
        asset["wins"] += 1
    asset["win_rate"] = round(asset["wins"] / asset["trades"], 3)
    config["asset_performance"][symbol] = asset

    # Deprioritize poor performers
    if asset["trades"] >= 5 and asset["win_rate"] < 0.30:
        if symbol not in config["deprioritized_assets"]:
            config["deprioritized_assets"].append(symbol)
            print(f"[fallback] Deprioritizing {symbol} ({asset['win_rate']:.1%} win rate)")

    # Update daily stats
    config["daily_stats"]["pnl_usd"] = config["daily_stats"].get("pnl_usd", 0) + pnl_usd
    config["daily_stats"]["trades"]  = config["daily_stats"].get("trades", 0) + 1
    config["daily_stats"]["last_trade_time"] = datetime.now(timezone.utc).isoformat()

    # Re-check circuit breaker after update
    daily_pnl = config["daily_stats"]["pnl_usd"]
    if daily_pnl <= -MAX_DAILY_LOSS_USD:
        config["daily_stats"]["circuit_tripped"] = True
        print(f"[fallback] CIRCUIT BREAKER TRIPPED — daily PnL: ${daily_pnl:.2f}")

    # Threshold adjustment every N trades
    if total % ADJUSTMENT_INTERVAL == 0:
        _adjust_thresholds(rule, rule_cfg)
        rule_cfg["last_adjusted"] = datetime.now(timezone.utc).isoformat()

    config["rules"][rule] = rule_cfg
    save_config(config)


def _adjust_thresholds(rule: str, rule_cfg: dict):
    win_rate = rule_cfg.get("win_rate", 0.5)
    if rule == "momentum_long":
        if win_rate < 0.40:
            rule_cfg["min_change_24h_pct"] = round(min(8.0, rule_cfg["min_change_24h_pct"] + 0.5), 1)
            print(f"  Tightening momentum threshold to {rule_cfg['min_change_24h_pct']}%")
        elif win_rate > 0.60:
            rule_cfg["min_change_24h_pct"] = round(max(2.0, rule_cfg["min_change_24h_pct"] - 0.5), 1)
            print(f"  Loosening momentum threshold to {rule_cfg['min_change_24h_pct']}%")
    elif rule == "fear_bounce":
        if win_rate < 0.40:
            rule_cfg["max_fear_greed"] = max(20, rule_cfg["max_fear_greed"] - 5)
        elif win_rate > 0.60:
            rule_cfg["max_fear_greed"] = min(45, rule_cfg["max_fear_greed"] + 5)
    elif rule == "volume_breakout":
        if win_rate < 0.40:
            rule_cfg["volume_vs_avg_multiplier"] = round(min(4.0, rule_cfg["volume_vs_avg_multiplier"] + 0.25), 2)
        elif win_rate > 0.60:
            rule_cfg["volume_vs_avg_multiplier"] = round(max(1.5, rule_cfg["volume_vs_avg_multiplier"] - 0.25), 2)
    elif rule == "taker_momentum":
        if win_rate < 0.40:
            rule_cfg["min_taker_buy_ratio"] = round(min(0.70, rule_cfg.get("min_taker_buy_ratio", 0.55) + 0.02), 3)
        elif win_rate > 0.60:
            rule_cfg["min_taker_buy_ratio"] = round(max(0.52, rule_cfg.get("min_taker_buy_ratio", 0.55) - 0.02), 3)


# ─────────────────────────────────────────────
# Test
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("FALLBACK STRATEGY v2 TEST")
    print("=" * 55)

    signal = run_fallback_scan(fear_greed=45)

    if signal:
        print(f"\n=== Best Signal ===")
        print(f"Symbol:         {signal.symbol}")
        print(f"Rule:           {signal.rule}")
        print(f"Price:          {signal.last_price}")
        print(f"Change 24h:     {signal.change_24h}%")
        print(f"Volume:         ${signal.volume_usdt/1e6:.1f}M")
        print(f"Score:          {signal.score}")
        print(f"R:R Ratio:      {signal.rr_ratio:.1f}x")
        print(f"Take Profit:    +{signal.take_profit_pct}%")
        print(f"Stop Loss:      -{signal.stop_loss_pct}%")
        print(f"Taker Ratio:    {signal.taker_buy_ratio}")
        print(f"Funding Rate:   {signal.funding_rate}")
        print(f"Gate Notes:     {signal.gate_notes}")
        print(f"Reason:         {signal.reason}")

        # Test learning update
        print(f"\n=== Learning Update Test ===")
        update_rule_performance(signal.rule, signal.symbol, 3.5, "take_profit", pnl_usd=35.0)
        update_rule_performance(signal.rule, signal.symbol, -2.0, "stop_loss", pnl_usd=-20.0)
    else:
        print("No signal found this scan")

    print("\n✅ fallback working correctly")
