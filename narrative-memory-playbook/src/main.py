import math
from typing import Any

from getagent import backtest, data, runtime


def _clean(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _clean_metrics(metrics: dict[str, Any]) -> dict[str, Any]:
    return {key: _clean(value) for key, value in metrics.items()}


def _effective_spec(symbols: list[str]) -> dict[str, Any]:
    spec = dict(runtime.backtest_spec or {})
    instruments = spec.get("instruments") or []
    wanted = {f"{symbol}.BINANCE" for symbol in symbols}
    spec["instruments"] = [
        instrument for instrument in instruments if instrument.get("id") in wanted
    ]
    strategy = dict(spec.get("strategy") or {})
    config = dict(strategy.get("config") or {})
    manifest_cfg = runtime.manifest.get("strategy_config", {}) or {}
    for key in (
        "trade_size",
        "lookback_bars",
        "momentum_return_pct",
        "fear_drop_pct",
        "fear_market_drop_pct",
        "breakout_volume_multiple",
        "stop_loss_pct",
        "base_take_profit_pct",
        "narrative_mode",
    ):
        if key in manifest_cfg:
            config[key] = manifest_cfg[key]
    strategy["config"] = config
    spec["strategy"] = strategy
    return spec


def run() -> None:
    cfg = runtime.manifest.get("strategy_config", {}) or {}
    configured_symbols = cfg.get("trading_symbols") or ["BTCUSDT"]
    max_symbols = int(cfg.get("max_backtest_symbols", len(configured_symbols)) or 1)
    symbols = [str(symbol).upper() for symbol in configured_symbols[:max_symbols]]

    frames: dict[str, Any] = {}
    skipped: list[str] = []
    for symbol in symbols:
        bars = data.crypto.futures.kline(
            symbol=symbol,
            interval="4h",
            exchange="binance",
            limit=1000,
        )
        replay_frame = backtest.prepare_frame(bars, datetime_index="date")
        if replay_frame.empty:
            skipped.append(symbol)
            continue
        frames[f"{symbol}.BINANCE"] = replay_frame

    if not frames:
        runtime.emit_signal(
            action="watch",
            symbol=symbols[0] if symbols else "",
            confidence=0.0,
            metrics={"symbols_loaded": 0, "symbols_requested": len(symbols)},
            meta={"reason": "no historical bars returned", "skipped_symbols": skipped},
        )
        return

    loaded_symbols = [key.split(".")[0] for key in frames]
    result = backtest.run(ohlcv_data=frames, spec=_effective_spec(loaded_symbols))
    chart_path = backtest.generate_chart(result)

    summary = result.summary or {}
    try:
        net_pnl = float(summary.get("net_pnl", 0) or 0)
    except (TypeError, ValueError):
        net_pnl = 0.0

    primary_symbol = loaded_symbols[0]
    action = "long" if net_pnl > 0 else "watch"
    metrics = _clean_metrics(
        {
            "total_return_pct": result.total_return_pct,
            "net_pnl": net_pnl,
            "starting_balance": summary.get("starting_balance"),
            "sharpe_ratio": result.sharpe_ratio,
            "max_drawdown_pct": result.max_drawdown_pct,
            "win_rate": result.win_rate,
            "total_trades": result.total_trades,
            "profit_factor": result.profit_factor,
            "symbols_requested": len(symbols),
            "symbols_loaded": len(loaded_symbols),
            "rows_min": min(len(frame) for frame in frames.values()),
        }
    )
    runtime.emit_signal(
        action=action,
        symbol=primary_symbol,
        confidence=_clean(result.win_rate) or 0.0,
        metrics=metrics,
        meta={
            "chart_path": chart_path,
            "loaded_symbols": loaded_symbols,
            "skipped_symbols": skipped,
            "narrative_mode": cfg.get("narrative_mode", "auto"),
            "active_narratives": cfg.get("active_narratives", []),
        },
    )


if __name__ == "__main__":
    run()
