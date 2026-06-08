from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model.data import Bar, BarType
from nautilus_trader.model.enums import OrderSide, TimeInForce
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.instruments import Instrument
from nautilus_trader.model.objects import Quantity
from nautilus_trader.trading.strategy import Strategy


@dataclass
class SymbolState:
    closes: list[float]
    volumes: list[float]


class NarrativeMemoryStrategyConfig(StrategyConfig):
    instrument_id: Optional[InstrumentId] = None
    bar_type: Optional[BarType] = None
    instrument_ids: tuple[InstrumentId, ...] = ()
    bar_types: tuple[BarType, ...] = ()
    trade_size: str = "0.01"
    lookback_bars: int = 24
    momentum_return_pct: float = 3.0
    fear_drop_pct: float = 5.0
    fear_market_drop_pct: float = 3.0
    breakout_volume_multiple: float = 2.0
    stop_loss_pct: float = 3.0
    base_take_profit_pct: float = 6.0
    narrative_mode: str = "auto"


class NarrativeMemoryStrategy(Strategy):
    def __init__(self, config: NarrativeMemoryStrategyConfig) -> None:
        super().__init__(config)
        self.cfg = config
        self._states: dict[InstrumentId, SymbolState] = {}
        self._instruments: dict[InstrumentId, Instrument] = {}
        self._active_instrument_id: Optional[InstrumentId] = None
        self._entry_price: Optional[float] = None
        self._take_profit_pct: float = config.base_take_profit_pct

    def on_start(self) -> None:
        instrument_ids = self.cfg.instrument_ids
        bar_types = self.cfg.bar_types
        if not instrument_ids and self.cfg.instrument_id is not None:
            instrument_ids = (self.cfg.instrument_id,)
        if not bar_types and self.cfg.bar_type is not None:
            bar_types = (self.cfg.bar_type,)
        if not instrument_ids or not bar_types:
            raise RuntimeError("instrument_ids and bar_types must be set")

        for instrument_id, bar_type in zip(instrument_ids, bar_types):
            instrument = self.cache.instrument(instrument_id)
            if instrument is None:
                continue
            self._instruments[instrument_id] = instrument
            self._states[instrument_id] = SymbolState(closes=[], volumes=[])
            self.subscribe_bars(bar_type)

    def on_bar(self, bar: Bar) -> None:
        instrument_id = bar.bar_type.instrument_id
        if instrument_id not in self._states:
            return

        state = self._states[instrument_id]
        close = float(bar.close)
        volume = float(bar.volume)
        state.closes.append(close)
        state.volumes.append(volume)

        if self._active_instrument_id == instrument_id:
            if self._should_exit(close):
                self._close_open(instrument_id)
                self._active_instrument_id = None
                self._entry_price = None
            return

        if self._active_instrument_id is not None:
            return

        score, take_profit_pct = self._score_setup(instrument_id)
        if score <= 0.0:
            return

        instrument = self._instruments.get(instrument_id)
        if instrument is None:
            return

        quantity = self._order_quantity(instrument)
        self._submit(instrument_id, OrderSide.BUY, quantity)
        self._active_instrument_id = instrument_id
        self._entry_price = close
        self._take_profit_pct = take_profit_pct

    def _score_setup(self, instrument_id: InstrumentId) -> tuple[float, float]:
        state = self._states[instrument_id]
        lookback = max(int(self.cfg.lookback_bars), 2)
        if len(state.closes) <= lookback or len(state.volumes) <= lookback:
            return 0.0, self.cfg.base_take_profit_pct

        close = state.closes[-1]
        reference = state.closes[-lookback]
        prior_close = state.closes[-2]
        avg_volume = sum(state.volumes[-lookback:-1]) / max(lookback - 1, 1)
        volume_ratio = state.volumes[-1] / avg_volume if avg_volume > 0 else 0.0
        return_pct = ((close / reference) - 1.0) * 100.0
        one_bar_pct = ((close / prior_close) - 1.0) * 100.0 if prior_close else 0.0
        market_drop_pct = self._market_drop_pct(lookback)

        narrative, narrative_weight = self._narrative_memory(instrument_id, return_pct, volume_ratio)
        if self.cfg.narrative_mode == "fallback_only":
            narrative_weight = 0.0

        momentum = return_pct >= self.cfg.momentum_return_pct and volume_ratio >= 1.1
        fear_bounce = (
            return_pct <= -self.cfg.fear_drop_pct
            and market_drop_pct <= -self.cfg.fear_market_drop_pct
            and one_bar_pct > 0.0
        )
        breakout = (
            volume_ratio >= self.cfg.breakout_volume_multiple
            and close > max(state.closes[-lookback:-1])
            and one_bar_pct > 0.0
        )

        score = 0.0
        if narrative:
            score += narrative_weight
        if momentum:
            score += 1.0
        if fear_bounce:
            score += 0.8
        if breakout:
            score += 1.2

        take_profit_pct = self.cfg.base_take_profit_pct * max(1.0, narrative_weight)
        return score, take_profit_pct

    def _market_drop_pct(self, lookback: int) -> float:
        btc_state = None
        for instrument_id, state in self._states.items():
            if str(instrument_id).startswith("BTCUSDT"):
                btc_state = state
                break
        if btc_state is None or len(btc_state.closes) <= lookback:
            return 0.0
        return ((btc_state.closes[-1] / btc_state.closes[-lookback]) - 1.0) * 100.0

    def _narrative_memory(
        self,
        instrument_id: InstrumentId,
        return_pct: float,
        volume_ratio: float,
    ) -> tuple[str, float]:
        symbol = str(instrument_id).split(".")[0].upper()
        mode = self.cfg.narrative_mode
        forced = {
            "force_ai": "ai",
            "force_rwa": "rwa",
            "force_meme": "meme",
            "force_depin": "depin",
        }.get(mode)
        narrative_by_symbol = {
            "ETHUSDT": "rwa",
            "SOLUSDT": "meme",
            "BNBUSDT": "depin",
            "XRPUSDT": "rwa",
            "DOGEUSDT": "meme",
            "LINKUSDT": "ai",
        }
        memory_weight = {
            "ai": 1.4,
            "rwa": 1.2,
            "meme": 1.6,
            "depin": 1.3,
        }
        narrative = forced or narrative_by_symbol.get(symbol, "")
        if not narrative:
            return "", 0.0
        if return_pct > 0.0 and volume_ratio >= 1.0:
            return narrative, memory_weight.get(narrative, 1.0)
        return "", 0.0

    def _should_exit(self, close: float) -> bool:
        if self._entry_price is None:
            return False
        move_pct = ((close / self._entry_price) - 1.0) * 100.0
        return move_pct <= -self.cfg.stop_loss_pct or move_pct >= self._take_profit_pct

    def _submit(
        self,
        instrument_id: InstrumentId,
        side: OrderSide,
        quantity: Quantity,
    ) -> None:
        order = self.order_factory.market(
            instrument_id=instrument_id,
            order_side=side,
            quantity=quantity,
            time_in_force=TimeInForce.GTC,
        )
        self.submit_order(order)

    def _order_quantity(self, instrument: Instrument) -> Quantity:
        raw_size = Decimal(self.cfg.trade_size)
        if instrument.size_precision == 0 and raw_size < Decimal("1"):
            raw_size = Decimal("1")
        return Quantity(raw_size, instrument.size_precision)

    def _close_open(self, instrument_id: InstrumentId) -> None:
        for position in self.cache.positions_open(instrument_id=instrument_id):
            self._submit(instrument_id, OrderSide.SELL, position.quantity)

    def on_stop(self) -> None:
        for instrument_id in self._instruments:
            self.cancel_all_orders(instrument_id)
            self.close_all_positions(instrument_id)
