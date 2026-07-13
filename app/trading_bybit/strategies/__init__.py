"""@responsibility Bybit 전략 레지스트리 — 이름→전략 클래스 매핑, 신규 전략 등록 지점"""
from .base import BybitContext, BybitStrategy
from .range_box import RangeBoxStrategy
from .regime_switch import RegimeSwitchStrategy
from .trend_breakout import TrendBreakoutStrategy
from .trendline import TrendlineStrategy

STRATEGIES = {
    "trend_breakout": TrendBreakoutStrategy,
    "trendline": TrendlineStrategy,
    "range_box": RangeBoxStrategy,
    "regime_switch": RegimeSwitchStrategy,
}


def make_strategy(name: str, config) -> BybitStrategy:
    if name not in STRATEGIES:
        raise ValueError(f"unknown strategy '{name}' (available: {list(STRATEGIES)})")
    return STRATEGIES[name](config)
