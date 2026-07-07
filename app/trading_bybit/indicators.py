"""@responsibility 지표 순수함수 — EMA·ATR·ADX·거래량MA·장악형·박스권·피벗·추세선, 전략이 소비하는 계산 전용

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


def adx(candles: list[Candle], period: int) -> float | None:
    """Wilder's ADX (trend strength, 0-100). High = trending, low = ranging —
    the regime gate between 추세장 and 횡보장. None if fewer than 2*period+1
    bars (DI warmup + DX smoothing)."""
    if len(candles) < 2 * period + 1 or period <= 0:
        return None
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    trs: list[float] = []
    for i in range(1, len(candles)):
        c, p = candles[i], candles[i - 1]
        up, down = c.high - p.high, p.low - c.low
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        trs.append(max(c.high - c.low, abs(c.high - p.close),
                       abs(c.low - p.close)))

    def _wilder_sum(vals: list[float]) -> list[float]:
        s = sum(vals[:period])
        out = [s]
        for v in vals[period:]:
            s = s - s / period + v
            out.append(s)
        return out

    tr_s, pdm_s, mdm_s = _wilder_sum(trs), _wilder_sum(plus_dm), _wilder_sum(minus_dm)
    dxs: list[float] = []
    for t, pd, md in zip(tr_s, pdm_s, mdm_s):
        if t <= 0:
            dxs.append(0.0)
            continue
        pdi, mdi = 100.0 * pd / t, 100.0 * md / t
        den = pdi + mdi
        dxs.append(100.0 * abs(pdi - mdi) / den if den > 0 else 0.0)
    if len(dxs) < period:
        return None
    adx_val = sum(dxs[:period]) / period
    for d in dxs[period:]:
        adx_val = (adx_val * (period - 1) + d) / period
    return adx_val


def swing_points(candles: list[Candle], strength: int
                 ) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """(pivot_highs, pivot_lows) as (index, price). A pivot needs `strength`
    strictly lower highs / higher lows on BOTH sides, so the newest `strength`
    bars can never be pivots yet (no look-ahead — a pivot is only known once
    confirmed by the right-hand bars)."""
    highs: list[tuple[int, float]] = []
    lows: list[tuple[int, float]] = []
    for i in range(strength, len(candles) - strength):
        c = candles[i]
        around = [candles[j] for j in range(i - strength, i + strength + 1) if j != i]
        if all(c.high > x.high for x in around):
            highs.append((i, c.high))
        if all(c.low < x.low for x in around):
            lows.append((i, c.low))
    return highs, lows


def trendline_from(pivots: list[tuple[int, float]]
                   ) -> tuple[float, float] | None:
    """(slope, intercept) of the line through the LAST TWO pivots — the classic
    two-touch trendline. value at bar i = slope*i + intercept. None with fewer
    than two pivots."""
    if len(pivots) < 2:
        return None
    (i1, p1), (i2, p2) = pivots[-2], pivots[-1]
    if i2 == i1:
        return None
    slope = (p2 - p1) / (i2 - i1)
    return slope, p1 - slope * i1
