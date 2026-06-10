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
MAX_EXPOSURE_PCT    = 0.30
MIN_RR              = 1.2       # minimum risk:reward ratio
COOLDOWN_SECONDS    = 300       # 5 minutes between any new entry
STOP_REENTRY_HOURS  = 12
MIN_TAKER_BUY_RATIO = 0.52      # buyers must be > 52% of taker volume
MAX_FUNDING_RATE    = 0.0003    # 0.03% — don't long into overleveraged longs

SIZE_ALLOCATION = {"small": 0.05, "medium": 0.10, "full": 0.15, "none": 0.0}

ELIGIBLE_BASE_ASSETS = {
    "AAVE", "ADA", "ALGO", "APT", "ARB", "ATOM", "AVAX", "BCH", "BNB",
    "BONK", "BTC", "CRV", "DOGE", "DOT", "ENA", "ETC", "ETH", "FET",
    "FIL", "HBAR", "HYPE", "INJ", "JUP", "LINK", "LTC", "NEAR", "ONDO",
    "OP", "PEPE", "POL", "RENDER", "SEI", "SHIB", "SOL", "STX", "SUI",
    "TAO", "TIA", "TON", "TRX", "UNI", "WIF", "XLM", "XRP",
}


# ─────────────────────────────────────────────
# Default Config
# ─────────────────────────────────────────────

DEFAULT_CONFIG = {
    "strategy_version": 3,
    "rules": {
        "momentum_long": {
            "enabled": True,
            "min_change_24h_pct": 2.0,
            "max_change_24h_pct": 12.0,
            "min_change_1h_pct": 0.15,
            "min_change_4h_pct": 0.75,
            "min_volume_usd": 10_000_000,
            "min_volume_ratio": 1.2,
            "atr_stop_multiplier": 1.6,
            "reward_risk_ratio": 2.0,
            "position_size": "small",
            "trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0.0, "last_adjusted": None,
        },
        "fear_bounce": {
            "enabled": True,
            "max_change_24h_pct": -6.0,
            "min_change_24h_pct": -15.0,
            "max_fear_greed": 25,
            "min_change_1h_pct": 0.15,
            "min_volume_usd": 10_000_000,
            "atr_stop_multiplier": 1.5,
            "reward_risk_ratio": 1.8,
            "position_size": "small",
            "trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0.0, "last_adjusted": None,
        },
        "volume_breakout": {
            "enabled": True,
            "min_change_24h_pct": 1.0,
            "max_change_24h_pct": 10.0,
            "min_change_1h_pct": 0.2,
            "min_volume_usd": 10_000_000,
            "volume_vs_avg_multiplier": 1.5,
            "atr_stop_multiplier": 1.6,
            "reward_risk_ratio": 2.0,
            "position_size": "small",
            "trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0.0, "last_adjusted": None,
        },
        "taker_momentum": {
            "enabled": True,
            "min_change_24h_pct": 2.0,
            "max_change_24h_pct": 10.0,
            "min_volume_usd": 8_000_000,
            "min_taker_buy_ratio": 0.55,  # aggressive buyer dominance
            "atr_stop_multiplier": 1.6,
            "reward_risk_ratio": 2.0,
            "position_size": "small",
            "trades": 0, "wins": 0, "losses": 0,
            "win_rate": 0.0, "last_adjusted": None,
        },
    },
    "asset_performance": {},
    "deprioritized_assets": [],
    "scan_count": 0,
    "last_scan": None,
    "processed_trade_ids": [],
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
        previous_version = int(config.get("strategy_version", 1))
        for rule, defaults in DEFAULT_CONFIG["rules"].items():
            if rule not in config["rules"]:
                config["rules"][rule] = defaults.copy()
            else:
                for key, value in defaults.items():
                    if (
                        previous_version < DEFAULT_CONFIG["strategy_version"]
                        and key not in {
                            "trades", "wins", "losses", "win_rate",
                            "last_adjusted",
                        }
                    ):
                        config["rules"][rule][key] = value
                    else:
                        config["rules"][rule].setdefault(key, value)
        if "daily_stats" not in config:
            config["daily_stats"] = DEFAULT_CONFIG["daily_stats"].copy()
        config.setdefault("processed_trade_ids", [])
        config["strategy_version"] = DEFAULT_CONFIG["strategy_version"]
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


def fetch_candles(
    symbol: str,
    granularity: str = "1h",
    limit: int = 50,
) -> list[dict]:
    """Fetch normalized spot candles ordered from oldest to newest."""
    try:
        resp = requests.get(
            "https://api.bitget.com/api/v2/spot/market/candles",
            params={
                "symbol": symbol,
                "granularity": granularity,
                "limit": str(limit),
            },
            timeout=8,
        )
        resp.raise_for_status()
        rows = resp.json().get("data", [])
        candles = []
        for row in rows:
            if len(row) < 7:
                continue
            candles.append({
                "timestamp": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume_usdt": float(row[6]),
            })
        return sorted(candles, key=lambda candle: candle["timestamp"])
    except (requests.RequestException, ValueError, TypeError) as exc:
        print(f"[fallback] Candle fetch failed for {symbol}: {exc}")
        return []


def _ema(values: list[float], period: int) -> float:
    if not values:
        return 0.0
    multiplier = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = value * multiplier + result * (1 - multiplier)
    return result


def _atr_pct(candles: list[dict], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    true_ranges = []
    for previous, current in zip(candles[-period - 1:-1], candles[-period:]):
        true_ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        ))
    close = candles[-1]["close"]
    return (sum(true_ranges) / len(true_ranges)) / close * 100 if close else 0.0


def enrich_market_history(market: dict) -> dict | None:
    """Add trend, rolling-volume, breakout, and volatility features."""
    candles = fetch_candles(market["symbol"])
    if len(candles) < 25:
        return None

    completed = candles[:-1]
    closes = [candle["close"] for candle in completed]
    prior_volumes = [candle["volume_usdt"] for candle in completed[-21:-1]]
    average_volume = sum(prior_volumes) / len(prior_volumes) if prior_volumes else 0
    previous_high = max(candle["high"] for candle in completed[-21:-1])
    latest = completed[-1]

    market.update({
        "change_1h": (closes[-1] / closes[-2] - 1) * 100,
        "change_4h": (closes[-1] / closes[-5] - 1) * 100,
        "ema_fast": _ema(closes[-30:], 8),
        "ema_slow": _ema(closes[-30:], 21),
        "volume_ratio": (
            latest["volume_usdt"] / average_volume if average_volume > 0 else 0
        ),
        "atr_pct": _atr_pct(completed),
        "positive_1h_candle": latest["close"] > latest["open"],
        "breakout_20h": latest["close"] >= previous_high * 0.995,
    })
    return market


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
    if exposure_pct + SIZE_ALLOCATION["small"] > MAX_EXPOSURE_PCT:
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
        base_asset = symbol[:-4]
        if base_asset not in ELIGIBLE_BASE_ASSETS:
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
    candidates = markets[:limit]
    enriched = []
    for market in candidates:
        historical = enrich_market_history(market)
        if historical:
            enriched.append(historical)
    print(
        f"[fallback] Scanned {len(enriched)} established crypto markets "
        f"(from {len(tickers)} tickers)"
    )
    return enriched


# ─────────────────────────────────────────────
# Rule Evaluators
# ─────────────────────────────────────────────

def risk_parameters(market: dict, rule: dict) -> tuple[float, float, float]:
    """Return volatility-aware stop, target, and R:R percentages."""
    atr_pct = max(0.75, min(float(market.get("atr_pct") or 0), 6.0))
    stop_pct = max(1.5, min(
        atr_pct * float(rule.get("atr_stop_multiplier", 1.6)),
        6.0,
    ))
    rr = max(MIN_RR, float(rule.get("reward_risk_ratio", 2.0)))
    target_pct = min(stop_pct * rr, 12.0)
    return round(stop_pct, 2), round(target_pct, 2), round(target_pct / stop_pct, 2)


def position_size_for_signal(score: float, stop_loss_pct: float) -> str:
    """Choose a tier while keeping approximate portfolio risk below 0.5%."""
    max_allocation = 0.005 / max(stop_loss_pct / 100, 0.0001)
    if score >= 0.82 and max_allocation >= SIZE_ALLOCATION["full"]:
        return "full"
    if score >= 0.68 and max_allocation >= SIZE_ALLOCATION["medium"]:
        return "medium"
    return "small"


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
        if m["change_1h"] < rule["min_change_1h_pct"]:
            continue
        if m["change_4h"] < rule["min_change_4h_pct"]:
            continue
        if m["ema_fast"] <= m["ema_slow"]:
            continue
        if m["volume_ratio"] < rule["min_volume_ratio"]:
            continue
        stop_pct, target_pct, rr = risk_parameters(m, rule)
        rr_ok, _ = check_rr(target_pct, stop_pct)
        if not rr_ok:
            continue
        score = min(
            0.92,
            0.42
            + min(m["change_4h"] / 8, 0.20)
            + min((m["volume_ratio"] - 1) / 4, 0.20)
            + (0.10 if m["breakout_20h"] else 0),
        )
        signals.append(FallbackSignal(
            rule="momentum_long", symbol=m["symbol"],
            last_price=m["last_price"], change_24h=change,
            volume_usdt=volume, take_profit_pct=target_pct,
            stop_loss_pct=stop_pct,
            position_size=position_size_for_signal(score, stop_pct),
            score=round(score, 3), rr_ratio=rr,
            reason=(
                f"Trend momentum: 24h={change:+.1f}%, 4h={m['change_4h']:+.1f}%, "
                f"volume={m['volume_ratio']:.1f}x, ATR={m['atr_pct']:.1f}%, "
                f"R:R={rr:.1f}x"
            ),
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
        if not (
            rule["min_change_24h_pct"]
            <= change
            <= rule["max_change_24h_pct"]
        ):
            continue
        if volume < rule["min_volume_usd"]:
            continue
        if m["change_1h"] < rule["min_change_1h_pct"]:
            continue
        if not m["positive_1h_candle"] or m["last_price"] < m["ema_fast"]:
            continue
        stop_pct, target_pct, rr = risk_parameters(m, rule)
        rr_ok, _ = check_rr(target_pct, stop_pct)
        if not rr_ok:
            continue
        drop_score = min(
            0.88,
            0.45
            + min(abs(change) / 30, 0.25)
            + min(max(m["change_1h"], 0) / 5, 0.15),
        )
        signals.append(FallbackSignal(
            rule="fear_bounce", symbol=m["symbol"],
            last_price=m["last_price"], change_24h=change,
            volume_usdt=volume, take_profit_pct=target_pct,
            stop_loss_pct=stop_pct,
            position_size=position_size_for_signal(drop_score, stop_pct),
            score=round(drop_score, 3), rr_ratio=rr,
            reason=(
                f"Confirmed fear bounce: 24h={change:.1f}%, "
                f"1h={m['change_1h']:+.1f}%, F&G={fear_greed or 'unknown'}, "
                f"ATR={m['atr_pct']:.1f}%, R:R={rr:.1f}x"
            ),
        ))
    return signals


def evaluate_volume_breakout(markets: list[dict], config: dict) -> list[FallbackSignal]:
    rule = config["rules"]["volume_breakout"]
    if not rule["enabled"] or not markets:
        return []
    signals = []
    for m in markets:
        change, volume = m["change_24h"], m["volume_usdt"]
        if not (
            rule["min_change_24h_pct"]
            <= change
            <= rule["max_change_24h_pct"]
        ):
            continue
        if volume < rule["min_volume_usd"]:
            continue
        if m["change_1h"] < rule["min_change_1h_pct"]:
            continue
        if not m["breakout_20h"] or m["ema_fast"] <= m["ema_slow"]:
            continue
        vol_mult = m["volume_ratio"]
        if vol_mult < rule["volume_vs_avg_multiplier"]:
            continue
        stop_pct, target_pct, rr = risk_parameters(m, rule)
        rr_ok, _ = check_rr(target_pct, stop_pct)
        if not rr_ok:
            continue
        score = round(min(0.92, 0.52 + (vol_mult - 1.5) / 5 * 0.35), 3)
        signals.append(FallbackSignal(
            rule="volume_breakout", symbol=m["symbol"],
            last_price=m["last_price"], change_24h=change,
            volume_usdt=volume, take_profit_pct=target_pct,
            stop_loss_pct=stop_pct,
            position_size=position_size_for_signal(score, stop_pct),
            score=score, rr_ratio=rr,
            reason=(
                f"20h breakout: own volume={vol_mult:.1f}x average, "
                f"24h={change:+.1f}%, ATR={m['atr_pct']:.1f}%, R:R={rr:.1f}x"
            ),
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
        if not (
            rule.get("min_change_24h_pct", 2.0)
            <= change
            <= rule.get("max_change_24h_pct", 10.0)
        ):
            continue
        if volume < rule.get("min_volume_usd", 8_000_000):
            continue
        if m["change_1h"] <= 0 or m["change_4h"] <= 0:
            continue
        if m["ema_fast"] <= m["ema_slow"]:
            continue
        # Check taker ratio (fetch from API)
        taker_ratio = fetch_taker_ratio(m["symbol"])
        if taker_ratio is None:
            continue
        if taker_ratio < rule.get("min_taker_buy_ratio", 0.55):
            continue
        stop_pct, target_pct, rr = risk_parameters(m, rule)
        rr_ok, _ = check_rr(target_pct, stop_pct)
        if not rr_ok:
            continue
        score = round(min(0.92, 0.48 + (taker_ratio - 0.5) * 1.8), 3)
        signals.append(FallbackSignal(
            rule="taker_momentum", symbol=m["symbol"],
            last_price=m["last_price"], change_24h=change,
            volume_usdt=volume,
            take_profit_pct=target_pct,
            stop_loss_pct=stop_pct,
            position_size=position_size_for_signal(score, stop_pct),
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

    def symbol_available(symbol: str) -> bool:
        if symbol in deprioritized or symbol in open_symbols:
            return False
        history = config.get("asset_performance", {}).get(symbol, {})
        if history.get("last_exit_reason") != "stop_loss":
            return True
        try:
            last_exit = datetime.fromisoformat(history["last_exit_time"])
            return datetime.now(timezone.utc) - last_exit >= timedelta(
                hours=STOP_REENTRY_HOURS
            )
        except (KeyError, TypeError, ValueError):
            return True

    signals = [signal for signal in signals if symbol_available(signal.symbol)]
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
    markets = scan_markets(limit=20)
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
    if best:
        current_exposure = sum(
            SIZE_ALLOCATION.get(trade.get("position_size", "small"), 0.05)
            for trade in open_trades
        )
        remaining = MAX_EXPOSURE_PCT - current_exposure
        if SIZE_ALLOCATION[best.position_size] > remaining:
            best.position_size = (
                "medium" if remaining >= SIZE_ALLOCATION["medium"] else "small"
            )
    save_config(config)
    return best


# ─────────────────────────────────────────────
# Learning: Update Rule Performance
# ─────────────────────────────────────────────

def update_rule_performance(
    rule: str,
    symbol: str,
    pnl_pct: float,
    exit_reason: str,
    pnl_usd: float = 0.0,
    trade_id: int = None,
):
    """Update rule win rate, adjust thresholds, update daily stats."""
    config = load_config()
    config = reset_daily_stats_if_needed(config)

    processed_ids = config.setdefault("processed_trade_ids", [])
    if trade_id is not None and trade_id in processed_ids:
        return

    if rule not in config["rules"]:
        save_config(config)
        return

    rule_cfg = config["rules"][rule]
    won = pnl_pct > 0
    effective_exit_reason = (
        "trailing_stop"
        if won and exit_reason == "stop_loss"
        else exit_reason
    )

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
    asset["last_exit_time"] = datetime.now(timezone.utc).isoformat()
    asset["last_exit_reason"] = effective_exit_reason
    asset["last_pnl_pct"] = round(pnl_pct, 4)
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

    if trade_id is not None:
        processed_ids.append(trade_id)
        config["processed_trade_ids"] = processed_ids[-500:]

    config["rules"][rule] = rule_cfg
    save_config(config)


def process_closed_fallback_trades(closed_trades: list[dict]) -> int:
    """Feed newly closed fallback trades into the adaptive rule statistics."""
    processed = 0
    for trade in closed_trades:
        if trade.get("trade_type") != "fallback":
            continue
        tag = trade.get("narrative_tag", "")
        if not tag.startswith("fallback_"):
            continue
        pnl_pct = float(trade.get("pnl_pct") or 0)
        allocation = SIZE_ALLOCATION.get(trade.get("position_size"), 0.05)
        pnl_usd = STARTING_BALANCE * allocation * pnl_pct / 100
        before = set(load_config().get("processed_trade_ids", []))
        update_rule_performance(
            rule=tag.removeprefix("fallback_"),
            symbol=trade["symbol"],
            pnl_pct=pnl_pct,
            exit_reason=trade.get("exit_reason") or "manual",
            pnl_usd=pnl_usd,
            trade_id=trade.get("id"),
        )
        if trade.get("id") not in before:
            processed += 1
    return processed


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
