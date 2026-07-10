"""Offline tests for the trendline / range-box / regime-switch strategies —
synthetic candles, no network. Run:  python -m tests.test_bybit_strategies"""
from __future__ import annotations

# tests.test_bybit isolates DATA_DIR before importing the app package —
# import it first so this module inherits the same isolation + helpers.
from tests.test_bybit import _c, _coherent_series, _uptrend_breakout  # noqa: F401

from app.trading_bybit import indicators as ind          # noqa: E402
from app.trading_bybit.config import BybitConfig          # noqa: E402
from app.trading_bybit.models import Side                 # noqa: E402
from app.trading_bybit.strategies import STRATEGIES, make_strategy  # noqa: E402
from app.trading_bybit.strategies.base import BybitContext  # noqa: E402


def _ctx(htf, entry):
    return BybitContext(symbol="BTC", htf_candles=htf, entry_candles=entry,
                        equity_usd=200, now=0)


# ------------------------------------------------------------- indicators

def _flat_htf(n=30):
    """Sideways bars: identical highs/lows -> +DM = -DM = 0 -> ADX 0."""
    return [_c(i * 3600000, 100, 101, 99, 100.5 if i % 2 else 99.5)
            for i in range(n)]


def _trending_htf(n=30, step=2.0):
    return [_c(i * 3600000, 100 + step * i, 101 + step * i,
               99 + step * i, 100.5 + step * i) for i in range(n)]


def test_adx_regime():
    assert ind.adx(_flat_htf(10), 14) is None            # warmup guard
    trend = ind.adx(_trending_htf(), 14)
    flat = ind.adx(_flat_htf(), 14)
    assert trend is not None and flat is not None
    assert trend > 25 and flat < 5, (trend, flat)
    print(f"ok  adx regime (trend {trend:.1f} vs flat {flat:.1f})")


def test_swing_and_trendline():
    # flat lows at 110 except confirmed dips at i=5 (100) and i=15 (105)
    lows = [110.0] * 24
    lows[5], lows[15] = 100.0, 105.0
    cs = [_c(i, lo + 1, lo + 2, lo, lo + 1.5) for i, lo in enumerate(lows)]
    highs, pivots = ind.swing_points(cs, 3)
    assert [p[0] for p in pivots] == [5, 15], pivots
    assert not highs                                     # equal highs: no pivot
    line = ind.trendline_from(pivots)
    assert line is not None
    slope, intercept = line
    assert abs(slope - 0.5) < 1e-9 and abs(intercept - 97.5) < 1e-9
    assert ind.trendline_from(pivots[:1]) is None        # needs two pivots
    print("ok  swing pivots + two-touch trendline fit")


# ------------------------------------------------------------- trendline

def _rising_zigzag(n=29):
    """Uptrend path (low = 100 + 0.5i) with pivot lows dipping to the support
    line (95 + 0.5i) at i=8 and i=18 — a rising two-touch trendline."""
    out = []
    for i in range(n):
        lo = 100 + 0.5 * i
        if i in (8, 18):
            lo = 95 + 0.5 * i
        out.append(_c(i * 900000, lo + 1, lo + 2, lo, lo + 1.5))
    return out


def test_trendline_bounce_long():
    cfg = BybitConfig()
    strat = make_strategy("trendline", cfg)
    htf = _trending_htf(10, step=1.0)                    # close >= EMA5 -> LONG

    # no touch: price is riding well above the projected line -> silent
    entry = _rising_zigzag(29)
    assert strat.evaluate(_ctx(htf, entry)) is None

    # touch bar: wick reaches the line (95 + 0.5*29 = 109.5), closes back above
    entry.append(_c(29 * 900000, 110.0, 111.2, 109.0, 110.8))
    sig = strat.evaluate(_ctx(htf, entry))
    assert sig is not None and sig.side is Side.LONG, sig
    assert sig.signal_type == "TRENDLINE_BOUNCE"
    assert sig.stop_price < sig.entry_hint
    d = strat.diagnose(_ctx(htf, entry))
    assert d and d["ready"] and d["passed"] == d["total"] == 4, d
    assert abs(d["trendline"] - 109.5) < 1e-6, d["trendline"]
    print("ok  trendline bounce LONG (touch+hold, stop below line)")


# ------------------------------------------------------------- range box

def _triangle_entry(n):
    """Triangle wave between 100 and 120 (period 20) — a clean 박스권."""
    out = []
    for i in range(n):
        t = i % 20
        px = 100 + 2 * t if t <= 10 else 100 + 2 * (20 - t)
        out.append(_c(i * 900000, px, px + 1, px - 1, px + 0.5))
    return out


def test_range_box_long_short():
    cfg = BybitConfig()
    strat = make_strategy("range_box", cfg)
    flat = _flat_htf(30)

    # mid-box bar -> silent (no edge touch)
    entry = _triangle_entry(39)                          # ends at px=104, falling
    assert strat.evaluate(_ctx(flat, entry)) is None

    # bottom touch + bullish reversal -> LONG, stop below the box
    entry_long = entry + [_c(39 * 900000, 100.2, 101.5, 99.2, 101.0)]
    sig = strat.evaluate(_ctx(flat, entry_long))
    assert sig is not None and sig.side is Side.LONG, sig
    assert sig.signal_type == "RANGE_REVERSAL"
    assert sig.stop_price < 99.0                         # box lo(99) - k*ATR

    # top touch + bearish reversal -> SHORT, stop above the box
    entry_short = _triangle_entry(30)                    # ends at px=118, rising
    entry_short.append(_c(30 * 900000, 119.8, 120.8, 118.5, 119.0))
    sig = strat.evaluate(_ctx(flat, entry_short))
    assert sig is not None and sig.side is Side.SHORT, sig
    assert sig.stop_price > 121.0                        # box hi(121) + k*ATR

    # trending regime -> the same bottom-touch setup is BLOCKED
    trending = _trending_htf(30)
    assert strat.evaluate(_ctx(trending, entry_long)) is None
    d = strat.diagnose(_ctx(trending, entry_long))
    assert d and not d["ready"]
    assert not next(g for g in d["gates"] if g["key"] == "regime")["ok"]
    print("ok  range box (LONG bottom / SHORT top / mid-box + trend regime silent)")


# ------------------------------------------------------------- regime switch

def test_regime_switch_routing():
    # 횡보장: flat HTF -> range_box sub fires at the box bottom
    cfg = BybitConfig()
    strat = make_strategy("regime_switch", cfg)
    entry_long = _triangle_entry(39) + [_c(39 * 900000, 100.2, 101.5, 99.2, 101.0)]
    sig = strat.evaluate(_ctx(_flat_htf(30), entry_long))
    assert sig is not None and sig.signal_type == "RANGE_REVERSAL", sig
    assert sig.detail.startswith("[range ADX"), sig.detail

    # 추세장: reuse the trend-breakout scenario; its HTF has only 25 bars so
    # shrink adx_period, and anchor thresholds to the measured ADX (test-only
    # routing check — live thresholds stay backtest-calibrated). confirm=1:
    # 이 테스트는 라우팅만 본다 — 확정 가드는 test_regime_confirm_bars 소관.
    htf, entry = _uptrend_breakout()
    cfg2 = BybitConfig()
    cfg2.adx_period = 10
    cfg2.regime_confirm_bars = 1
    v = ind.adx(htf, cfg2.adx_period)
    cfg2.adx_trend_min = v * 0.9
    cfg2.adx_range_max = v * 0.5
    strat2 = make_strategy("regime_switch", cfg2)
    sig2 = strat2.evaluate(_ctx(htf, entry))
    assert sig2 is not None and sig2.signal_type == "TREND_BREAKOUT", sig2
    assert sig2.detail.startswith("[trend ADX"), sig2.detail

    # dead zone (range_max < ADX < trend_min) -> no entries, ready False
    cfg3 = BybitConfig()
    cfg3.adx_period = 10
    cfg3.adx_trend_min = v + 5
    cfg3.adx_range_max = max(v - 5, 0.0)
    strat3 = make_strategy("regime_switch", cfg3)
    assert strat3.evaluate(_ctx(htf, entry)) is None
    d = strat3.diagnose(_ctx(htf, entry))
    assert d and not d["ready"] and d["regime"] == "neutral", d
    assert d["gates"][0]["key"] == "regime" and not d["gates"][0]["ok"]

    # misconfigured sub-strategy name -> loud failure at construction
    cfg4 = BybitConfig()
    cfg4.regime_range_strategy = "nope"
    try:
        make_strategy("regime_switch", cfg4)
        assert False, "should have raised"
    except ValueError:
        pass
    print("ok  regime switch (range/trend routing, dead zone, bad sub name)")


def test_regime_none_option():
    """regime_range_strategy='none' → 횡보장에서는 관망 (신규 진입 없음)."""
    cfg = BybitConfig()
    cfg.regime_range_strategy = "none"
    strat = make_strategy("regime_switch", cfg)
    entry_long = _triangle_entry(39) + [_c(39 * 900000, 100.2, 101.5, 99.2, 101.0)]
    assert strat.evaluate(_ctx(_flat_htf(30), entry_long)) is None
    d = strat.diagnose(_ctx(_flat_htf(30), entry_long))
    assert d and not d["ready"] and "관망" in d["gates"][0]["info"], d
    print("ok  regime none option (횡보장 관망 — no entries)")


def test_regime_confirm_bars():
    """레짐 확정 가드 — 임계값을 1봉만 스친 레짐은 채택하지 않는다.
    픽스처: _uptrend_breakout HTF의 ADX(10)는 최신 봉 10.0, 직전 봉 0.0 —
    즉 '이번 봉에 갓 나타난 추세장'. 앵커 임계값은 테스트 전용."""
    htf, entry = _uptrend_breakout()
    base = dict(adx_period=10, adx_trend_min=5.0, adx_range_max=-1.0)

    # confirm=2: 추세가 최신 1봉뿐 → 관망 (pending), 시그널 없음
    cfg = BybitConfig()
    for k, val in {**base, "regime_confirm_bars": 2}.items():
        setattr(cfg, k, val)
    strat = make_strategy("regime_switch", cfg)
    assert strat.evaluate(_ctx(htf, entry)) is None
    d = strat.diagnose(_ctx(htf, entry))
    assert d and d["regime"] == "neutral" and not d["gates"][0]["ok"], d
    assert "확인 중" in d["gates"][0]["info"], d["gates"][0]
    tel = strat.telemetry()
    assert tel["bars"].get("confirming") == 1 and tel["regime_flips"] == 0, tel

    # confirm=1 (즉시 채택): 동일 컨텍스트에서 시그널이 나와야 한다 (대조군)
    cfg1 = BybitConfig()
    for k, val in {**base, "regime_confirm_bars": 1}.items():
        setattr(cfg1, k, val)
    strat1 = make_strategy("regime_switch", cfg1)
    sig = strat1.evaluate(_ctx(htf, entry))
    assert sig is not None and sig.signal_type == "TREND_BREAKOUT", sig

    # 추세 HTF 봉이 하나 더 쌓여 2연속 확정 → confirm=2도 채택·시그널 방출
    htf2 = htf + [_c(25 * 3600000, 130, 152, 129, 150, 500)]
    sig2 = strat.evaluate(_ctx(htf2, entry))
    assert sig2 is not None and sig2.detail.startswith("[trend ADX"), sig2
    tel2 = strat.telemetry()
    assert tel2["bars"].get("trend") == 1, tel2
    print("ok  regime confirm bars (1봉 스침 관망 → 2연속 확정 채택 + 텔레메트리)")


def test_stratify_by_regime():
    from app.trading_bybit.backtest.metrics import stratify_by_regime
    trades = [
        {"symbol": "BTC", "event": "OPEN",
         "signal_detail": "[range ADX 12.0 → range_box] 횡보"},
        {"symbol": "BTC", "event": "CLOSE", "pnl_usd": 4.0, "r_multiple": 2.0},
        {"symbol": "BTC", "event": "OPEN",
         "signal_detail": "[trend ADX 30.0 → trend_breakout] 추세"},
        {"symbol": "BTC", "event": "PARTIAL", "pnl_usd": 1.0, "r_multiple": 0.5},
        {"symbol": "BTC", "event": "CLOSE", "pnl_usd": -2.0, "r_multiple": -1.0},
        {"symbol": "ETH", "event": "OPEN", "signal_detail": "no prefix"},
        {"symbol": "ETH", "event": "CLOSE", "pnl_usd": 1.0, "r_multiple": 0.5},
    ]
    s = stratify_by_regime(trades)
    assert set(s) == {"range", "trend", "unrouted"}, s
    assert s["range"]["trades"] == 1 and s["range"]["wins"] == 1, s["range"]
    assert s["trend"]["trades"] == 1 and s["trend"]["losses"] == 1, s["trend"]
    assert s["trend"]["pnl_usd"] == -2.0, s["trend"]      # PARTIAL 행 미중복
    assert s["unrouted"]["trades"] == 1, s["unrouted"]
    print("ok  stratify by regime (팔별 분리 + unrouted + PARTIAL 비중복)")


# ------------------------------------------------------------- integration

def test_registry_and_backtest_replay():
    from app.trading_bybit.backtest.engine import replay
    assert {"trendline", "range_box", "regime_switch"} <= set(STRATEGIES)
    cfg = BybitConfig()
    htf, entry = _coherent_series(220)
    for name in ("trendline", "range_box", "regime_switch"):
        r = replay("BTC", name, cfg, entry_candles=entry, htf_candles=htf)
        assert r["snapshots"] > 0, (name, r)
        if name == "regime_switch":
            tel = r.get("regime_telemetry")
            assert tel and sum(tel["bars"].values()) == r["snapshots"], tel
    print("ok  registry + backtest replay smoke (3 new strategies + telemetry)")


if __name__ == "__main__":
    test_adx_regime()
    test_swing_and_trendline()
    test_trendline_bounce_long()
    test_range_box_long_short()
    test_regime_switch_routing()
    test_regime_none_option()
    test_regime_confirm_bars()
    test_stratify_by_regime()
    test_registry_and_backtest_replay()
    print("\nall strategy tests passed ✅")
