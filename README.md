# Narrative Memory Agent

A paper-trading crypto agent that combines narrative detection with historical
memory and a regime-aware technical fallback strategy.

The agent has three position slots:

- One reserved narrative position.
- Two fallback positions.
- No real orders are submitted. All execution is paper trading.

## Why Narrative Memory

Crypto markets often organize around themes such as AI, RWA tokenization,
DePIN, meme cycles, and exchange-traded product adoption. The agent detects
these narratives from news and sentiment, retrieves similar historical
narrative outcomes, and uses that memory to influence:

- entry timing;
- representative asset selection;
- position size;
- expected holding period;
- stop and target planning.

When no suitable narrative entry is ready, the fallback lane continues to scan
the market instead of leaving the portfolio idle.

## Architecture

```text
News + sentiment + market data
              |
              v
     Narrative detection
              |
       Historical memory
              |
              v
      Narrative decision ---------+
                                  |
Spot tickers + 1h candles         |
              |                   |
              v                   v
       Fallback V4 scanner --> Paper execution
                                  |
                                  v
                      Live marks, exits, learning
                                  |
                                  v
                         SQLite + dashboard
```

## Fallback Strategy V4

The fallback strategy scans the top 20 eligible USDT spot markets by volume.
It uses an allowlist of established crypto assets to exclude stablecoins,
leveraged products, tokenized equities, and newly listed noise.

Each candidate receives 50 one-hour candles. BTC's 4-hour trend and EMA structure classify the broad market as bullish, bearish, or neutral; bearish regimes suppress new longs, bullish regimes suppress shorts, and neutral regimes allow both sides. These candles are also used to calculate:

- 1-hour and 4-hour returns;
- 8-period and 21-period EMAs;
- volume relative to the asset's own rolling 20-hour average;
- a rolling 20-hour breakout level;
- 14-period ATR as a percentage of price;
- positive-candle confirmation.

### Momentum Long

Requires:

- 24-hour return between 2% and 12%;
- positive 1-hour and 4-hour momentum;
- EMA 8 above EMA 21;
- volume at least 1.2 times the asset's own rolling average;
- acceptable funding and portfolio risk.

The upper return bound is intended to avoid chasing assets after extreme daily
spikes.

### Momentum Short

Requires:

- 24-hour return between -12% and -2%;
- negative 1-hour and 4-hour momentum;
- EMA 8 below EMA 21;
- a confirmed breakdown below the rolling 20-hour low;
- volume at least 1.2 times the asset's own rolling average;
- funding that is not already excessively negative.

Short stops sit above entry and targets below entry. The same ATR, reward-to-risk,
breakeven, trailing-stop, exposure, and cooldown controls used for longs apply
symmetrically to shorts.

### Confirmed Fear Bounce

Requires:

- Fear and Greed at or below 25;
- 24-hour decline between 6% and 15%;
- a positive 1-hour candle;
- positive 1-hour return;
- price reclaiming the 8-period EMA.

An asset stopped out by this rule cannot be re-entered for 12 hours.

### Volume Breakout

Requires:

- a new or near-new 20-hour high;
- positive 1-hour price action;
- EMA 8 above EMA 21;
- volume at least 1.5 times the asset's own rolling average.

Volume is deliberately compared with the same asset's history, not with the
volume of unrelated markets.

### Taker Momentum

Adds derivatives taker-flow confirmation to a moderate spot trend. It requires
positive 1-hour and 4-hour momentum, EMA alignment, and a minimum taker buy
ratio.

## Risk Management

- Stops use 1.5 to 1.6 ATR, bounded between 1.5% and 6%.
- Targets are expressed as reward-to-risk multiples, generally 1.8R to 2R.
- Position tiers allocate approximately 5%, 10%, or 15% of the paper account.
- Position selection keeps estimated risk near or below 0.5% of portfolio.
- Total modeled allocation is capped at 30%.
- At +1R, a fallback stop moves to breakeven.
- At +1.5R, the stop begins trailing by the original risk distance.
- A fallback trade with weak progress after 24 hours is closed by a time stop.
- A daily loss circuit breaker and entry cooldown remain active.

## Learning Loop

Closed fallback trades are processed exactly once. The agent records:

- trade count, wins, losses, and win rate by rule;
- per-asset performance;
- last exit reason and time;
- daily paper PnL;
- assets that repeatedly underperform.

Automatic threshold adjustment occurs only at configured observation
intervals. A larger sample is still required before treating adaptive changes
as statistically meaningful.

Narrative trades and fallback trades have separate writeback paths so fallback
outcomes do not contaminate narrative memory.

## Dashboard

The dashboard displays:

- open and closed paper trades;
- live mark price and unrealized PnL;
- current stop and target;
- narrative memory;
- fallback rule statistics;
- portfolio curve;
- agent activity logs.

Data refreshes every 30 seconds. Open positions are marked every minute.

## Railway Deployment

Build and start commands are defined by `Dockerfile` and `start.sh`.

Mount a Railway volume and configure:

```text
DATA_DIR=/data
LOG_DIR=/data/logs
```

The SQLite database, state, strategy configuration, learning statistics, and
logs must share that persistent volume. Without it, redeployment can reset the
agent's memory.

The application listens on Railway's `PORT` variable.

## Local Verification

```bash
python -m py_compile main.py agent/*.py dashboard/app.py
python main.py
```

The agent requires its configured market-data tools and network access. Do not
use real exchange credentials for paper-trading evaluation.

## Evaluation Guidance

For competition evaluation, compare:

1. fallback-only performance;
2. narrative detection without memory;
3. narrative detection with memory-informed timing and sizing.

The strongest evidence for the concept is an ablation showing that memory
improves return, drawdown, timing, or abstention rather than merely adding an
explanation layer.

## Limitations

- Paper results do not model full slippage, spread, or market impact.
- The fallback sample must grow before its adaptive statistics are reliable.
- Narrative memories seeded from historical examples should eventually be
  replaced or supplemented with systematically collected outcomes.
- A single representative token may not fully capture a broad narrative.
- Historical narrative returns should be treated as distributions, not exact
  future targets.

## Research Basis

- [Momentum and liquidity in cryptocurrencies](https://arxiv.org/abs/1904.00890)
- [Sentiment and cryptocurrency return forecasting](https://arxiv.org/abs/2210.00883)
- [ChatGPT's effect on AI-related crypto assets](https://arxiv.org/abs/2305.12739)
- [Bitget spot candlestick API](https://www.bitget.com/api-doc/spot/market/Get-Candle-Data)
- [Bitget spot ticker API](https://www.bitget.com/api-doc/spot/market/Get-Tickers)
