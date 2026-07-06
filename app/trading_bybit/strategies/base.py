"""@responsibility Bybit 전략 플러그인 프로토콜 — 전략은 TradeSignal만 방출, 사이징·집행은 포지션 FSM 소유

Bybit strategy plugin protocol. A strategy inspects the current candle
context once per closed entry-bar and either stays quiet (None) or emits a
TradeSignal proposing a direction and a stop. Position sizing, order
placement, trailing, pyramiding and settlement are NOT the strategy's job —
the position FSM owns those (mirrors Part 4's strategy/execution split).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..models import Candle, TradeSignal


@dataclass
class BybitContext:
    """Everything a strategy sees for one decision. Candles are CLOSED bars
    (the in-progress bar is excluded), newest last."""
    symbol: str
    htf_candles: list[Candle]        # higher-timeframe closed bars
    entry_candles: list[Candle]      # entry-timeframe closed bars
    equity_usd: float
    now: float                       # wall clock (unix sec)
    open_position_side: str | None = None   # "LONG"/"SHORT" if already in one
    extras: dict = field(default_factory=dict)   # indicator cache for the UI


class BybitStrategy(ABC):
    name: str = "base"

    def __init__(self, config):
        self.cfg = config

    @abstractmethod
    def evaluate(self, ctx: BybitContext) -> TradeSignal | None:
        ...

    def diagnose(self, ctx: BybitContext) -> dict | None:
        """Optional live gate snapshot for the dashboard (None = unsupported)."""
        return None
