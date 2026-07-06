"""@responsibility Bybit 트레이딩 공유 데이터타입 — Candle·TradeSignal·Position 등 도메인 모델

Shared datatypes for the Bybit leverage-margin trading package (Part 5).

Unlike Part 4's binary Up/Down markets, these model a continuous leveraged
perpetual position: entry, ATR stop, R-multiple target, partial exit, ATR
trailing and pyramiding. All PnL is expressed in USDT and in R-multiples
(realized_pnl / initial_risk) so the profit-ratio discipline from the
strategy source ("먹을 때 크게, 잃을 때 작게") is measurable directly.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


@dataclass(frozen=True)
class Candle:
    """One OHLCV bar (exchange kline). ts_ms = bar OPEN time."""
    ts_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def bullish(self) -> bool:
        return self.close >= self.open

    def as_dict(self) -> dict:
        return asdict(self)


class Side(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1

    @property
    def opposite(self) -> "Side":
        return Side.SHORT if self is Side.LONG else Side.LONG


class PositionState(str, Enum):
    FLAT = "FLAT"          # no position, watching
    OPEN = "OPEN"          # at least one unit filled, managing
    CLOSED = "CLOSED"      # fully exited this position


@dataclass
class TradeSignal:
    """A strategy's one-shot opinion. The strategy proposes direction and the
    risk anchors (stop distance from ATR, target as an R multiple); the
    position FSM owns sizing, fills, trailing and settlement."""
    side: Side
    signal_type: str            # e.g. "TREND_BREAKOUT"
    strength: int               # 0-100
    stop_price: float           # hard invalidation (1R away)
    entry_hint: float           # reference entry (next-bar open in paper)
    detail: str = ""

    def as_dict(self) -> dict:
        d = asdict(self)
        d["side"] = self.side.value
        return d


@dataclass
class Unit:
    """One filled tranche of a position (base entry or a pyramiding add)."""
    side: Side
    entry_price: float
    qty: float                  # base-asset quantity
    stop_price: float
    entry_ts: float
    is_add: bool = False
    fee_usd: float = 0.0

    @property
    def notional(self) -> float:
        return round(self.entry_price * self.qty, 6)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["side"] = self.side.value
        return d


@dataclass
class Position:
    """Aggregate of one or more Units on the same side, managed as a whole:
    shared trailing stop, staged take-profit, pyramiding adds. Risk (R) is
    fixed at the FIRST unit's entry so every downstream number is an R
    multiple — the profit-ratio yardstick."""
    symbol: str
    side: Side
    units: list[Unit] = field(default_factory=list)
    initial_risk_usd: float = 0.0     # 1R, fixed at first fill
    target_price: float | None = None  # first take-profit (2R by default)
    trail_price: float | None = None   # active trailing stop (None until armed)
    realized_pnl_usd: float = 0.0
    realized_fee_usd: float = 0.0
    closed_qty: float = 0.0
    partial_done: bool = False
    state: PositionState = PositionState.OPEN

    @property
    def open_qty(self) -> float:
        return round(sum(u.qty for u in self.units) - self.closed_qty, 8)

    @property
    def avg_entry(self) -> float:
        tot = sum(u.qty for u in self.units)
        if tot <= 0:
            return 0.0
        return sum(u.entry_price * u.qty for u in self.units) / tot

    @property
    def adds(self) -> int:
        return sum(1 for u in self.units if u.is_add)

    @property
    def stop_price(self) -> float | None:
        """The effective protective stop = trailing stop if armed, else the
        tightest unit stop on the risk side."""
        if self.trail_price is not None:
            return self.trail_price
        if not self.units:
            return None
        stops = [u.stop_price for u in self.units]
        return max(stops) if self.side is Side.SHORT else min(stops)

    def r_multiple(self) -> float | None:
        if self.initial_risk_usd <= 0:
            return None
        return round(self.realized_pnl_usd / self.initial_risk_usd, 3)

    def unrealized_usd(self, price: float) -> float:
        return round(self.side.sign * (price - self.avg_entry) * self.open_qty, 4)

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "side": self.side.value,
            "state": self.state.value, "units": len(self.units),
            "adds": self.adds, "open_qty": self.open_qty,
            "avg_entry": round(self.avg_entry, 4),
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "trail_price": self.trail_price,
            "initial_risk_usd": round(self.initial_risk_usd, 4),
            "realized_pnl_usd": round(self.realized_pnl_usd, 4),
            "r_multiple": self.r_multiple(),
            "partial_done": self.partial_done,
        }
