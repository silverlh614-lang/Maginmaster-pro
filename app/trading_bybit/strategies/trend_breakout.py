"""@responsibility 추세돌파 전략 — 상위추세·5EMA·박스돌파·장악형·거래량 게이트 평가 후 TradeSignal 방출·진단

Trend-breakout — the strategy source's main system, encoded as one gate
chain (all must agree, else no trade — the source's "칼이 많으면 못 찌른다"
discipline):

  1. HTF trend filter : HTF close vs its 5-EMA sets the ALLOWED direction.
  2. box breakout     : the newest HTF close must break beyond the prior box.
  3. entry 5-EMA      : entry-timeframe close on the same side of its 5-EMA.
  4. engulfing+volume : a 장악형 candle with volume above its MA (거래량=세력).

`_gates()` computes every gate once; `evaluate()` fires a signal when all
pass, and `diagnose()` exposes the same gate states + indicator values so the
dashboard can show live "how close to a signal" without duplicating logic.
"""
from __future__ import annotations

from ..indicators import (atr, bearish_engulfing, box_range, bullish_engulfing,
                          ema, sma)
from ..models import Side, TradeSignal
from .base import BybitContext, BybitStrategy


class TrendBreakoutStrategy(BybitStrategy):
    name = "trend_breakout"

    def _gates(self, ctx: BybitContext) -> dict | None:
        c = self.cfg
        htf, ef = ctx.htf_candles, ctx.entry_candles
        if len(htf) < max(c.ema_period, c.box_lookback) + 2:
            return None
        if len(ef) < max(c.ema_period, c.vol_ma_period, c.atr_period) + 2:
            return None

        htf_ema = ema([x.close for x in htf], c.ema_period)[-1]
        allowed = Side.LONG if htf[-1].close >= htf_ema else Side.SHORT

        box = box_range(htf, c.box_lookback)
        hi, lo = (box if box else (None, None))
        buf = c.breakout_buffer_pct / 100.0
        broke = False
        if box:
            broke = (htf[-1].close > hi * (1 + buf) if allowed is Side.LONG
                     else htf[-1].close < lo * (1 - buf))

        ef_ema = ema([x.close for x in ef], c.ema_period)[-1]
        ema_ok = (ef[-1].close >= ef_ema if allowed is Side.LONG
                  else ef[-1].close <= ef_ema)

        prev, cur = ef[-2], ef[-1]
        engulf = (bullish_engulfing(prev, cur) if allowed is Side.LONG
                  else bearish_engulfing(prev, cur))
        engulf_ok = engulf or not c.require_engulfing

        vma = sma([x.volume for x in ef], c.vol_ma_period)
        vol_ratio = (cur.volume / vma) if vma else None
        vol_ok = (not c.require_volume) or (vma is None) or (cur.volume >= vma * c.vol_mult)

        a = atr(ef, c.atr_period)
        entry_ref = cur.close
        stop = None
        if a and a > 0:
            stop = (entry_ref - a * c.atr_stop_mult if allowed is Side.LONG
                    else entry_ref + a * c.atr_stop_mult)

        ready = bool(broke and ema_ok and engulf_ok and vol_ok and a and a > 0)
        return {
            "allowed": allowed, "htf_close": htf[-1].close, "htf_ema": htf_ema,
            "box_hi": hi, "box_lo": lo, "broke": broke,
            "entry_close": entry_ref, "entry_ema": ef_ema, "ema_ok": ema_ok,
            "engulf": engulf, "engulf_ok": engulf_ok,
            "vol_ratio": vol_ratio, "vol_ok": vol_ok, "vol_mult": c.vol_mult,
            "atr": a, "stop": stop, "entry_ref": entry_ref, "ready": ready,
        }

    def evaluate(self, ctx: BybitContext) -> TradeSignal | None:
        g = self._gates(ctx)
        if not g or not g["ready"]:
            return None
        allowed = g["allowed"]
        a, stop, entry_ref = g["atr"], g["stop"], g["entry_ref"]
        cur = ctx.entry_candles[-1]
        strength = 60 + min(40, int(cur.body / a * 20)) if a else 60
        detail = (f"HTF {'>' if allowed is Side.LONG else '<'}EMA5, "
                  f"box {'↑' if allowed is Side.LONG else '↓'}"
                  f"[{g['box_lo']:.2f},{g['box_hi']:.2f}], engulf+vol, ATR {a:.2f}")
        return TradeSignal(side=allowed, signal_type="TREND_BREAKOUT",
                           strength=strength, stop_price=round(stop, 6),
                           entry_hint=round(entry_ref, 6), detail=detail)

    def diagnose(self, ctx: BybitContext) -> dict | None:
        """Live gate snapshot for the dashboard (same computation as
        evaluate, but never short-circuits — every gate's state is exposed)."""
        g = self._gates(ctx)
        if not g:
            return None
        allowed = g["allowed"].value
        gates = [
            {"key": "trend", "label": "HTF 추세(5EMA)", "ok": True,
             "info": f"{allowed} · {g['htf_close']:.2f} vs {g['htf_ema']:.2f}"},
            {"key": "breakout", "label": "박스 돌파", "ok": bool(g["broke"]),
             "info": (f"[{g['box_lo']:.2f}, {g['box_hi']:.2f}]"
                      if g["box_hi"] is not None else "–")},
            {"key": "ema", "label": "진입 5EMA 정렬", "ok": bool(g["ema_ok"]),
             "info": f"{g['entry_close']:.2f} vs {g['entry_ema']:.2f}"},
            {"key": "engulf", "label": "장악형 + 거래량", "ok": bool(g["engulf_ok"] and g["vol_ok"]),
             "info": (f"engulf={'Y' if g['engulf'] else 'N'} · "
                      f"vol×{g['vol_ratio']:.2f}" if g["vol_ratio"] is not None
                      else "engulf/vol")},
        ]
        passed = sum(1 for x in gates if x["ok"])
        return {"allowed": allowed, "ready": g["ready"],
                "passed": passed, "total": len(gates), "gates": gates,
                "atr": round(g["atr"], 4) if g["atr"] else None,
                "box_hi": g["box_hi"], "box_lo": g["box_lo"],
                "entry_ema": round(g["entry_ema"], 6),
                "stop_preview": round(g["stop"], 6) if g["stop"] else None}
