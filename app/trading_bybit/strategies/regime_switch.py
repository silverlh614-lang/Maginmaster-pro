"""@responsibility 레짐 스위치 메타전략 — HTF ADX로 추세/횡보 판별, 상황에 맞는 하위 전략에 평가·진단 위임

Regime switch (상황 적응 매매). One HTF ADX reading decides the market
regime, then the bar's decision is delegated to the matching sub-strategy:

  ADX >= adx_trend_min → 추세장 → regime_trend_strategy (기본 trend_breakout)
  ADX <= adx_range_max → 횡보장 → regime_range_strategy (기본 range_box)
  그 사이 (dead zone)  → 판단 유보 — 신규 진입 없음 (레짐 전환 whipsaw 방지)

A regime is only ADOPTED once it holds for regime_confirm_bars consecutive
HTF closes (임계값 1봉 스침으로 인한 경계 whipsaw 가드) — checked statelessly
by re-reading ADX on truncated histories, so live/backtest/restart agree.
Per-bar regime counts and confirmed-regime flips are kept as telemetry()
for the Phase 2 gate's stratified sample-size report.

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

# 하위 전략 후보 (registry를 import하면 순환이라 여기서 직접 매핑).
# "none" = 해당 레짐에서는 관망 — 신규 진입 없음 (예: 횡보장 무매매).
_SUBS: dict[str, type[BybitStrategy] | None] = {
    "trend_breakout": TrendBreakoutStrategy,
    "trendline": TrendlineStrategy,
    "range_box": RangeBoxStrategy,
    "none": None,
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
        _make = lambda n: _SUBS[n](config) if _SUBS[n] is not None else None
        self.trend_sub = _make(config.regime_trend_strategy)
        self.range_sub = _make(config.regime_range_strategy)
        # 텔레메트리 (판단에 불사용): bar별 레짐 분포 + 확정 레짐 플립 수.
        # Phase 2 게이트가 "어느 팔이 표본 부족인가"를 정량화하는 데 쓴다.
        self._counts: dict[str, int] = {}
        self._flips = 0
        self._last_confirmed: str | None = None

    def _regime_raw(self, htf) -> tuple[str | None, float | None]:
        """임계값만 본 순간 판정 ('trend'|'range'|'neutral'|None, adx)."""
        v = adx(htf, self.cfg.adx_period)
        if v is None:
            return None, None
        if v >= self.cfg.adx_trend_min:
            return "trend", v
        if v <= self.cfg.adx_range_max:
            return "range", v
        return "neutral", v

    def _regime(self, ctx: BybitContext
                ) -> tuple[str | None, float | None, str | None]:
        """('trend'|'range'|'neutral'|None, adx, pending).

        None = ADX 워밍업 부족. 레짐은 최근 regime_confirm_bars 연속 HTF
        봉에서 같은 판정이 유지될 때만 채택 — 미확정이면 'neutral'(관망)로
        내리고 pending에 대기 중인 레짐을 담는다 (경계 whipsaw 가드).
        무상태: 잘린 히스토리로 재판정하므로 재시작·백테스트와 동일."""
        raw, v = self._regime_raw(ctx.htf_candles)
        n = max(1, int(self.cfg.regime_confirm_bars))
        if raw in (None, "neutral") or n == 1:
            return raw, v, None
        for k in range(1, n):
            prev, _ = self._regime_raw(ctx.htf_candles[:-k])
            if prev != raw:
                return "neutral", v, raw
        return raw, v, None

    def _record(self, regime: str | None, pending: str | None) -> None:
        key = "confirming" if pending else (regime or "warmup")
        self._counts[key] = self._counts.get(key, 0) + 1
        if regime in ("trend", "range") and regime != self._last_confirmed:
            if self._last_confirmed is not None:
                self._flips += 1
            self._last_confirmed = regime

    def telemetry(self) -> dict:
        """평가 bar별 레짐 분포와 확정 레짐 플립 수 (백테스트 리포트용)."""
        return {"bars": dict(self._counts), "regime_flips": self._flips}

    def _sub(self, regime: str | None) -> BybitStrategy | None:
        if regime == "trend":
            return self.trend_sub
        if regime == "range":
            return self.range_sub
        return None

    def evaluate(self, ctx: BybitContext) -> TradeSignal | None:
        regime, v, pending = self._regime(ctx)
        self._record(regime, pending)
        sub = self._sub(regime)
        if sub is None:
            return None
        sig = sub.evaluate(ctx)
        if sig is not None:
            sig.detail = f"[{regime} ADX {v:.1f} → {sub.name}] {sig.detail}"
        return sig

    def diagnose(self, ctx: BybitContext) -> dict | None:
        regime, v, pending = self._regime(ctx)
        c = self.cfg
        t_name = self.trend_sub.name if self.trend_sub else "관망(none)"
        r_name = self.range_sub.name if self.range_sub else "관망(none)"
        neutral = (f"관망 — {pending} 전환 확인 중 "
                   f"(연속 {max(1, int(c.regime_confirm_bars))}봉 확정 대기)"
                   if pending else "관망 (dead zone)")
        label = {"trend": f"추세장 → {t_name}",
                 "range": f"횡보장 → {r_name}",
                 "neutral": neutral}.get(regime, "ADX 워밍업")
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
