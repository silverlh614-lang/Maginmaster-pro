"""@responsibility 레짐 스위치 메타전략 — HTF ADX로 추세/횡보 판별, 상황에 맞는 하위 전략에 평가·진단 위임

Regime switch (상황 적응 매매). One HTF ADX reading decides the market
regime, then the bar's decision is delegated to the matching sub-strategy:

  ADX >= adx_trend_min → 추세장 → regime_trend_strategy (기본 trend_breakout)
  ADX <= adx_range_max → 횡보장 → regime_range_strategy (기본 range_box)
  그 사이 (dead zone)  → 판단 유보 — 신규 진입 없음 (레짐 전환 whipsaw 방지)

Sub-strategies are reused untouched — this class only routes the same
BybitContext to one of them per bar, so the strategy/execution split holds
and each sub-strategy stays independently backtestable. Thresholds are
Phase 2 backtest-gate calibration targets (hand-tune 금지).
"""
from __future__ import annotations

from ..indicators import adx
from ..models import TradeSignal
from .base import BybitContext, BybitStrategy
from .range_box import RangeBoxStrategy
from .trend_breakout import TrendBreakoutStrategy
from .trendline import TrendlineStrategy

# 하위 전략 후보 (registry를 import하면 순환이라 여기서 직접 매핑)
_SUBS: dict[str, type[BybitStrategy]] = {
    "trend_breakout": TrendBreakoutStrategy,
    "trendline": TrendlineStrategy,
    "range_box": RangeBoxStrategy,
}


class RegimeSwitchStrategy(BybitStrategy):
    name = "regime_switch"

    def __init__(self, config):
        super().__init__(config)
        for field in ("regime_trend_strategy", "regime_range_strategy"):
            name = getattr(config, field)
            if name not in _SUBS:
                raise ValueError(
                    f"{field}='{name}' 미지원 (가능: {list(_SUBS)})")
        self.trend_sub = _SUBS[config.regime_trend_strategy](config)
        self.range_sub = _SUBS[config.regime_range_strategy](config)

    def _regime(self, ctx: BybitContext) -> tuple[str | None, float | None]:
        """('trend'|'range'|'neutral'|None, adx). None = ADX 워밍업 부족."""
        v = adx(ctx.htf_candles, self.cfg.adx_period)
        if v is None:
            return None, None
        if v >= self.cfg.adx_trend_min:
            return "trend", v
        if v <= self.cfg.adx_range_max:
            return "range", v
        return "neutral", v

    def _sub(self, regime: str | None) -> BybitStrategy | None:
        if regime == "trend":
            return self.trend_sub
        if regime == "range":
            return self.range_sub
        return None

    def evaluate(self, ctx: BybitContext) -> TradeSignal | None:
        regime, v = self._regime(ctx)
        sub = self._sub(regime)
        if sub is None:
            return None
        sig = sub.evaluate(ctx)
        if sig is not None:
            sig.detail = f"[{regime} ADX {v:.1f} → {sub.name}] {sig.detail}"
        return sig

    def diagnose(self, ctx: BybitContext) -> dict | None:
        regime, v = self._regime(ctx)
        c = self.cfg
        label = {"trend": f"추세장 → {self.trend_sub.name}",
                 "range": f"횡보장 → {self.range_sub.name}",
                 "neutral": "관망 (dead zone)"}.get(regime, "ADX 워밍업")
        regime_gate = {
            "key": "regime", "label": "레짐 판별(HTF ADX)",
            "ok": regime in ("trend", "range"),
            "info": (f"ADX {v:.1f} · {label} "
                     f"(횡보≤{c.adx_range_max:.0f} / 추세≥{c.adx_trend_min:.0f})"
                     if v is not None else label),
        }
        sub = self._sub(regime)
        sub_d = sub.diagnose(ctx) if sub else None
        if sub_d is None:
            return {"allowed": "–", "ready": False, "regime": regime,
                    "passed": 1 if regime_gate["ok"] else 0, "total": 1,
                    "gates": [regime_gate],
                    "adx": round(v, 2) if v is not None else None}
        out = dict(sub_d)
        out["gates"] = [regime_gate] + sub_d["gates"]
        out["passed"] = (1 if regime_gate["ok"] else 0) + sub_d["passed"]
        out["total"] = 1 + sub_d["total"]
        out["ready"] = bool(regime_gate["ok"] and sub_d["ready"])
        out["regime"] = regime
        out["adx"] = round(v, 2) if v is not None else None
        return out
