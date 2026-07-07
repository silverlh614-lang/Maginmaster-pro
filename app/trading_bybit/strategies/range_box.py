"""@responsibility 박스권 매매 전략 — ADX 횡보 확인 후 박스 상·하단 반전 게이트 평가, TradeSignal 방출·진단

Range-box reversal (박스권 매매). Works ONLY in a sideways market and fades
the edges of the consolidation box instead of chasing breakouts:

  1. regime  : HTF ADX <= adx_range_max — 횡보장 확인 (추세장에서는 침묵).
  2. box     : entry-TF box over range_lookback bars (current bar excluded).
  3. touch   : the bar's wick reaches the bottom (LONG) / top (SHORT) touch
               zone (k*ATR) and the close is back inside the box.
  4. reverse : reversal body — 양봉 at the bottom, 음봉 at the top.
  5. fits    : the FSM's rr_target profit must fit INSIDE the box (박스
               높이가 2R 목표를 감당 못 하면 진입하지 않는다).

Stop = box edge ∓ k*ATR (박스 이탈 = 무효). All thresholds are Phase 2
backtest-gate calibration targets (hand-tune 금지).
"""
from __future__ import annotations

from ..indicators import adx, atr, box_range
from ..models import Side, TradeSignal
from .base import BybitContext, BybitStrategy


class RangeBoxStrategy(BybitStrategy):
    name = "range_box"

    def _gates(self, ctx: BybitContext) -> dict | None:
        c = self.cfg
        htf, ef = ctx.htf_candles, ctx.entry_candles
        if len(htf) < 2 * c.adx_period + 1:
            return None
        if len(ef) < max(c.range_lookback, c.atr_period) + 2:
            return None

        adx_v = adx(htf, c.adx_period)
        regime_ok = adx_v is not None and adx_v <= c.adx_range_max

        box = box_range(ef, c.range_lookback)
        hi, lo = (box if box else (None, None))
        a = atr(ef, c.atr_period)
        cur = ef[-1]

        side = None
        touch = reverse = fits = False
        stop = None
        if box and a and a > 0:
            tol = c.range_touch_atr * a
            near_lo = cur.low <= lo + tol and cur.close > lo
            near_hi = cur.high >= hi - tol and cur.close < hi
            # 한 봉이 상·하단을 동시에 훑으면 박스가 봉 대비 너무 작다 — 침묵
            if near_lo and not near_hi:
                side, touch = Side.LONG, True
                reverse = cur.bullish
                stop = lo - a * c.atr_stop_mult
                fits = cur.close + c.rr_target * (cur.close - stop) <= hi
            elif near_hi and not near_lo:
                side, touch = Side.SHORT, True
                reverse = not cur.bullish
                stop = hi + a * c.atr_stop_mult
                fits = cur.close - c.rr_target * (stop - cur.close) >= lo

        # 후보 방향 (진단 표시용): 터치 전에는 가까운 변 기준
        if side is None and box:
            side = Side.LONG if (cur.close - lo) <= (hi - cur.close) else Side.SHORT

        ready = bool(regime_ok and touch and reverse and fits and a and a > 0)
        return {
            "allowed": side or Side.LONG, "adx": adx_v, "regime_ok": regime_ok,
            "box_hi": hi, "box_lo": lo, "touch": touch, "reverse": reverse,
            "fits": fits, "entry_ref": cur.close, "atr": a, "stop": stop,
            "ready": ready,
        }

    def evaluate(self, ctx: BybitContext) -> TradeSignal | None:
        g = self._gates(ctx)
        if not g or not g["ready"]:
            return None
        side, a = g["allowed"], g["atr"]
        cur = ctx.entry_candles[-1]
        strength = 60 + min(40, int(cur.body / a * 20)) if a else 60
        detail = (f"횡보(ADX {g['adx']:.1f}), 박스 "
                  f"[{g['box_lo']:.2f},{g['box_hi']:.2f}] "
                  f"{'하단 반등' if side is Side.LONG else '상단 반락'}, "
                  f"ATR {a:.2f}")
        return TradeSignal(side=side, signal_type="RANGE_REVERSAL",
                           strength=strength, stop_price=round(g["stop"], 6),
                           entry_hint=round(g["entry_ref"], 6), detail=detail)

    def diagnose(self, ctx: BybitContext) -> dict | None:
        g = self._gates(ctx)
        if not g:
            return None
        allowed = g["allowed"].value
        gates = [
            {"key": "regime", "label": "횡보장(ADX)", "ok": bool(g["regime_ok"]),
             "info": (f"ADX {g['adx']:.1f} ≤ {self.cfg.adx_range_max:.0f}"
                      if g["adx"] is not None else "–")},
            {"key": "box", "label": "박스권 확보", "ok": g["box_hi"] is not None,
             "info": (f"[{g['box_lo']:.2f}, {g['box_hi']:.2f}]"
                      if g["box_hi"] is not None else "–")},
            {"key": "touch", "label": "상·하단 터치 + 반전 몸통",
             "ok": bool(g["touch"] and g["reverse"]),
             "info": f"{allowed} 후보 · 현재가 {g['entry_ref']:.2f}"},
            {"key": "fits", "label": "2R 목표 박스 내", "ok": bool(g["fits"]),
             "info": f"R:R {self.cfg.rr_target:.1f}"},
        ]
        passed = sum(1 for x in gates if x["ok"])
        return {"allowed": allowed, "ready": g["ready"],
                "passed": passed, "total": len(gates), "gates": gates,
                "atr": round(g["atr"], 4) if g["atr"] else None,
                "box_hi": g["box_hi"], "box_lo": g["box_lo"],
                "adx": round(g["adx"], 2) if g["adx"] is not None else None,
                "stop_preview": round(g["stop"], 6) if g["stop"] else None}
