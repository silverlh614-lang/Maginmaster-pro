"""@responsibility 추세선 매매 전략 — 피벗 2점 추세선 되돌림 터치·반등 게이트 평가 후 TradeSignal 방출·진단

Trendline bounce (추세선 매매). Connect the last two CONFIRMED swing pivots
on the entry timeframe into a trendline and enter WITH the higher-timeframe
trend when price pulls back to the line and holds it:

  1. HTF trend filter : HTF close vs its 5-EMA sets the ALLOWED direction.
  2. trendline        : rising pivot lows (LONG) / falling pivot highs (SHORT)
                        — the projected line must slope with the trend.
  3. touch            : the bar's wick reaches the line's touch zone (k*ATR).
  4. hold             : the bar closes back on the trend side of the line with
                        a same-direction body (반등 확인).

Stop = line value ∓ k*ATR — a clean line break is the invalidation. All
thresholds are Phase 2 backtest-gate calibration targets (hand-tune 금지).
"""
from __future__ import annotations

from ..indicators import atr, ema, swing_points, trendline_from
from ..models import Side, TradeSignal
from .base import BybitContext, BybitStrategy


class TrendlineStrategy(BybitStrategy):
    name = "trendline"

    def _gates(self, ctx: BybitContext) -> dict | None:
        c = self.cfg
        htf, ef = ctx.htf_candles, ctx.entry_candles
        if len(htf) < c.ema_period + 2:
            return None
        if len(ef) < max(c.ema_period, c.atr_period, 2 * c.pivot_strength + 3) + 2:
            return None

        htf_ema = ema([x.close for x in htf], c.ema_period)[-1]
        allowed = Side.LONG if htf[-1].close >= htf_ema else Side.SHORT

        highs, lows = swing_points(ef, c.pivot_strength)
        pivots = lows if allowed is Side.LONG else highs
        line = trendline_from(pivots)
        i_now = len(ef) - 1
        slope = line_val = None
        line_ok = False
        if line:
            slope, intercept = line
            line_val = slope * i_now + intercept
            # 상승 지지선(LONG)은 우상향, 하락 저항선(SHORT)은 우하향이어야 한다
            line_ok = slope > 0 if allowed is Side.LONG else slope < 0

        a = atr(ef, c.atr_period)
        cur = ef[-1]
        touch = hold = False
        stop = None
        if line_ok and a and a > 0:
            tol = c.trendline_touch_atr * a
            if allowed is Side.LONG:
                touch = cur.low <= line_val + tol
                hold = cur.close > line_val and cur.bullish
                stop = line_val - a * c.atr_stop_mult
            else:
                touch = cur.high >= line_val - tol
                hold = cur.close < line_val and not cur.bullish
                stop = line_val + a * c.atr_stop_mult

        ready = bool(line_ok and touch and hold and a and a > 0)
        return {
            "allowed": allowed, "htf_close": htf[-1].close, "htf_ema": htf_ema,
            "pivots": len(pivots), "slope": slope, "line_val": line_val,
            "line_ok": line_ok, "touch": touch, "hold": hold,
            "entry_ref": cur.close, "atr": a, "stop": stop, "ready": ready,
        }

    def evaluate(self, ctx: BybitContext) -> TradeSignal | None:
        g = self._gates(ctx)
        if not g or not g["ready"]:
            return None
        allowed, a = g["allowed"], g["atr"]
        cur = ctx.entry_candles[-1]
        strength = 60 + min(40, int(cur.body / a * 20)) if a else 60
        detail = (f"HTF {'>' if allowed is Side.LONG else '<'}EMA5, "
                  f"추세선 {g['line_val']:.2f} (기울기 {g['slope']:+.4f}) "
                  f"터치·반등, ATR {a:.2f}")
        return TradeSignal(side=allowed, signal_type="TRENDLINE_BOUNCE",
                           strength=strength, stop_price=round(g["stop"], 6),
                           entry_hint=round(g["entry_ref"], 6), detail=detail)

    def diagnose(self, ctx: BybitContext) -> dict | None:
        g = self._gates(ctx)
        if not g:
            return None
        allowed = g["allowed"].value
        line_info = (f"{g['line_val']:.2f} · 기울기 {g['slope']:+.4f}"
                     if g["line_val"] is not None else f"피벗 {g['pivots']}개")
        gates = [
            {"key": "trend", "label": "HTF 추세(5EMA)", "ok": True,
             "info": f"{allowed} · {g['htf_close']:.2f} vs {g['htf_ema']:.2f}"},
            {"key": "line", "label": "추세선(피벗 2점)", "ok": bool(g["line_ok"]),
             "info": line_info},
            {"key": "touch", "label": "추세선 터치", "ok": bool(g["touch"]),
             "info": f"현재가 {g['entry_ref']:.2f}"},
            {"key": "hold", "label": "반등 확인(방향 몸통)", "ok": bool(g["hold"]),
             "info": "종가가 추세선 위/아래 복귀"},
        ]
        passed = sum(1 for x in gates if x["ok"])
        return {"allowed": allowed, "ready": g["ready"],
                "passed": passed, "total": len(gates), "gates": gates,
                "atr": round(g["atr"], 4) if g["atr"] else None,
                "box_hi": None, "box_lo": None,
                "trendline": (round(g["line_val"], 6)
                              if g["line_val"] is not None else None),
                "stop_preview": round(g["stop"], 6) if g["stop"] else None}
