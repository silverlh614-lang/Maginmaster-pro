"""@responsibility 지표 순수함수 — EMA·ATR·거래량MA·장악형 캔들·박스권, 전략이 소비하는 계산 전용

Pure indicator functions over Candle lists. No I/O, no state — every value
is derived from the candles passed in, so they are trivially unit-testable
and identical in live and backtest. They encode the strategy source's
systems: the 5-period MA trend filter, ATR volatility sizing, volume
confirmation, the engulfing (장악형) pattern and the box range.
"""
from __future__ import annotations

from .models import Candle


def ema(values: list[float], period: int) -> list[float]:
    """Exponential moving average, seeded with the first value. Returns a
    list aligned 1:1 with `values` (index i = EMA up to and including i)."""
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def sma(values: list[float], period: int) -> float | None:
    """Simple moving average of the last `period` values (None if too few)."""
    if len(values) < period or period <= 0:
        return None
    return sum(values[-period:]) / period


def atr(candles: list[Candle], period: int) -> float | None:
    """Wilder's Average True Range over the last `period` bars (None if too
    few). True range includes gaps (prev close), so it survives the violent
    candles the source warns about."""
    if len(candles) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        trs.append(max(c.high - c.low, abs(c.high - p.close),
                       abs(c.low - p.close)))
    # Wilder smoothing over the tail
    atr_val = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr_val = (atr_val * (period - 1) + tr) / period
    return atr_val


def bullish_engulfing(prev: Candle, cur: Candle) -> bool:
    """상승 장악형: prev bearish, cur bullish, cur body strictly larger and
    engulfing prev's body (bodies compared, wicks ignored — source rule)."""
    if prev.bullish or not cur.bullish:
        return False
    if cur.body <= prev.body:
        return False
    return cur.close >= prev.open and cur.open <= prev.close


def bearish_engulfing(prev: Candle, cur: Candle) -> bool:
    """하락 장악형: prev bullish, cur bearish, cur body strictly larger and
    engulfing prev's body."""
    if not prev.bullish or cur.bullish:
        return False
    if cur.body <= prev.body:
        return False
    return cur.open >= prev.close and cur.close <= prev.open


def box_range(candles: list[Candle], lookback: int) -> tuple[float, float] | None:
    """(high, low) of the last `lookback` bars EXCLUDING the most recent bar —
    the consolidation box the newest candle may break out of. None if too
    few bars."""
    if len(candles) < lookback + 1:
        return None
    window = candles[-(lookback + 1):-1]
    return max(c.high for c in window), min(c.low for c in window)
