"""@responsibility Part 2 FSM — 일별 온체인 입력을 바닥 감지 상태기계에 넣어 자본 배치 비율 산출

Part 2 — Rule-based bottom-detection state machine (daily signal generator).

Consumes daily on-chain inputs, emits a phase + a capital-deploy fraction.
Ladder-buys the descent, confirms on reclaim signals, hands off to trend logic.
Designed to work even when the exact low is missed (knowledge base §1-4).

State persists to a JSON file so the daily cadence survives process restarts
(Railway redeploys). Pure logic — no numpy required.
"""
from __future__ import annotations
import json
import os
from dataclasses import dataclass, field
from enum import Enum


class Phase(Enum):
    WATCH = "watch"                    # nothing to do
    CAPITULATION_WATCH = "cap_watch"   # under cost basis -> arm the ladder
    ACCUMULATE = "accumulate"          # deploy DCA tranches on new lows
    CONFIRM = "confirm"                # reclaim firing -> deploy remainder
    TREND = "trend"                    # bottom confirmed -> trend logic takes over


@dataclass
class Daily:
    price: float
    realized_price: float   # network cost basis
    mvrv_z: float           # <0 undervalued; historical bottoms ~ -0.5..0
    ma_200w: float
    lth_mvrv: float
    sth_mvrv: float
    made_new_low: bool      # new cycle low printed today?


@dataclass
class BottomDetector:
    ladder_tranches: int = 5
    phase: Phase = Phase.WATCH
    _fired: int = 0
    _low: float = field(default=float("inf"))

    def update(self, d: Daily) -> dict:
        under_cost   = d.price < d.realized_price
        deep_value   = d.mvrv_z < 0.0
        reclaim      = d.price > d.realized_price and d.mvrv_z > 0.0
        lth_cross    = d.lth_mvrv > d.sth_mvrv           # classic bottom/bull-start cross
        reclaim_200w = d.price > d.ma_200w
        deploy = 0.0

        if self.phase is Phase.WATCH:
            if under_cost or deep_value:
                self.phase, self._low = Phase.CAPITULATION_WATCH, d.price

        if self.phase is Phase.CAPITULATION_WATCH:
            self._low = min(self._low, d.price)
            if d.mvrv_z < -0.2 and under_cost:
                self.phase = Phase.ACCUMULATE

        if self.phase is Phase.ACCUMULATE:
            if self._fired < self.ladder_tranches and (d.made_new_low or d.mvrv_z < -0.3):
                self._fired += 1
                deploy = 1.0 / self.ladder_tranches
            if reclaim and lth_cross:
                self.phase = Phase.CONFIRM

        if self.phase is Phase.CONFIRM:
            if self._fired < self.ladder_tranches:
                deploy = (self.ladder_tranches - self._fired) / self.ladder_tranches
                self._fired = self.ladder_tranches
            if reclaim_200w:
                self.phase = Phase.TREND

        return {
            "phase": self.phase.value,
            "deploy_fraction": round(deploy, 4),
            "cum_deployed": round(self._fired / self.ladder_tranches, 4),
            "flags": {
                "under_cost": under_cost, "mvrv_green": deep_value,
                "lth>sth": lth_cross, "reclaim_200w": reclaim_200w,
            },
        }

    # ---- persistence -------------------------------------------------
    def to_state(self) -> dict:
        low = self._low
        return {
            "ladder_tranches": self.ladder_tranches,
            "phase": self.phase.value,
            "fired": self._fired,
            "low": None if low == float("inf") else low,
        }

    @classmethod
    def from_state(cls, s: dict) -> "BottomDetector":
        det = cls(ladder_tranches=int(s.get("ladder_tranches", 5)))
        det.phase = Phase(s.get("phase", "watch"))
        det._fired = int(s.get("fired", 0))
        low = s.get("low")
        det._low = float("inf") if low is None else float(low)
        return det


def load_detector(path: str) -> BottomDetector:
    if os.path.exists(path):
        with open(path) as f:
            return BottomDetector.from_state(json.load(f))
    return BottomDetector()


def save_detector(det: BottomDetector, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(det.to_state(), f)


def demo_path() -> list[dict]:
    """Run a fresh detector over a synthetic descent -> capitulation -> recovery."""
    prices = ([61_500 - i * 700 for i in range(20)]      # 61.5k -> ~48k
              + [48_000 + i * 900 for i in range(20)])   # recover -> ~65k
    realized = 53_600
    det = BottomDetector(ladder_tranches=5)
    prev_low = float("inf")
    rows = []
    for i, p in enumerate(prices):
        new_low = p < prev_low
        prev_low = min(prev_low, p)
        # toy on-chain proxies driven by the price/cost-basis gap
        gap = (p - realized) / realized
        mvrv_z = max(-0.8, min(3.0, gap * 4.0))
        lth = 1.0 + gap * 1.2
        sth = 1.0 + gap * 0.3
        r = det.update(Daily(p, realized, mvrv_z, 61_800, lth, sth, new_low))
        rows.append({"day": i, "price": p, **r})
    return rows
