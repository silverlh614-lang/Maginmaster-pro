"""Offline tests for the IS/OOS replay window and the H7/H8 gate-verdict
runner — synthetic candles, no network.
Run:  python -m tests.test_gatereport"""
from __future__ import annotations

import math
import os
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="gate-test-"))

from app.trading_bybit.backtest.engine import replay              # noqa: E402
from app.trading_bybit.backtest.gatereport import (               # noqa: E402
    MIN_TRADES, build_report)
from app.trading_bybit.config import BybitConfig                  # noqa: E402
from app.trading_bybit.models import Candle                       # noqa: E402


def _c(ts, o, h, l, cl, v=100.0):
    return Candle(ts_ms=ts, open=o, high=h, low=l, close=cl, volume=v)


def _series(n=800):
    """15m path: consolidation → up-leg → chop → down-leg, with volume spikes
    so the breakout gates can fire in both windows."""
    out = []
    px = 100.0
    for i in range(n):
        if i < 200:
            drift = 0.0
        elif i < 420:
            drift = 0.35
        elif i < 560:
            drift = 0.0
        else:
            drift = -0.3
        base = px + drift + 1.6 * math.sin(i / 4.0)
        o = base
        c = base + (0.8 if i % 2 == 0 else -0.8) + drift
        h, l = max(o, c) + 0.6, min(o, c) - 0.6
        v = 100 + (260 if i % 6 == 0 else 0)
        out.append(_c(i * 900_000, o, h, l, c, v))
        px = c
    return out


def _agg(entry, k):
    """Aggregate k 15m bars into one higher-TF bar (aligned like real klines)."""
    htf = []
    for j in range(0, len(entry) - (k - 1), k):
        g = entry[j:j + k]
        htf.append(_c(g[0].ts_ms, g[0].open, max(x.high for x in g),
                      min(x.low for x in g), g[-1].close,
                      sum(x.volume for x in g)))
    return htf


def test_frac_windows():
    """IS(0,0.7) + OOS(0.7,1) must partition the full replay: window edges
    meet at the split index and snapshot counts add up to the full run."""
    cfg = BybitConfig()
    entry = _series(800)
    htf = _agg(entry, 4)          # 60m
    full = replay("BTC", "trend_breakout", cfg, entry_candles=entry, htf_candles=htf)
    r_is = replay("BTC", "trend_breakout", cfg, entry_candles=entry,
                  htf_candles=htf, frac=(0.0, 0.7))
    r_oos = replay("BTC", "trend_breakout", cfg, entry_candles=entry,
                   htf_candles=htf, frac=(0.7, 1.0))
    assert r_is["window"][1] == r_oos["window"][0] == int(800 * 0.7)
    assert r_oos["window"][1] == 800 and full["window"][0] == r_is["window"][0]
    assert r_is["snapshots"] + r_oos["snapshots"] == full["snapshots"], (
        r_is["snapshots"], r_oos["snapshots"], full["snapshots"])
    # OOS 트레이드는 전부 분할점 이후 봉에서만 발생해야 한다
    split_ts = entry[int(800 * 0.7)].ts_ms / 1000
    for row in r_oos["closes"]:
        assert float(row["ts"]) >= split_ts if str(row["ts"]).replace(".", "").isdigit() else True
    print("ok  frac windows partition the replay (IS+OOS == full)")


def test_build_report_structure():
    """Matrix runner returns per-cell metrics and a verdict draft for H7/H8;
    a missing HTF series (D not supplied) is skipped, not fatal."""
    cfg = BybitConfig()
    entry = _series(800)
    htf_by_iv = {"60": _agg(entry, 4), "240": _agg(entry, 16)}   # D 없음 → 스킵
    rep = build_report("BTC", cfg, entry, htf_by_iv, split=0.7)

    assert rep["symbol"] == "BTC" and rep["split"] == 0.7
    assert rep["entry_bars"] == 800
    h7, h8 = rep["h7"], rep["h8"]
    assert len(h7["cells"]) == 4                  # (60,240) × (IS,OOS)
    assert {c["seg"] for c in h7["cells"]} == {"IS", "OOS"}
    assert len(h8["cells"]) == 4                  # exempt off/on × IS/OOS
    assert {c["exempt"] for c in h8["cells"]} == {False, True}
    for cell in h7["cells"] + h8["cells"]:
        for k in ("trades", "net_usd", "pf", "mdd_usd", "short_trades"):
            assert k in cell, cell
    assert isinstance(h7["verdict"], str) and h7["verdict"]
    assert isinstance(h8["verdict"], str) and h8["verdict"]
    assert isinstance(h7["low_sample"], bool) and isinstance(h8["low_sample"], bool)
    # 표본 부족이면 verdict 가 그렇게 말해야 한다 (합성 데이터는 대체로 소표본)
    if h7["low_sample"]:
        assert "표본" in h7["verdict"]
    assert "초안" in rep["note"]
    print(f"ok  gate report structure (h7 cells=4, h8 cells=4, "
          f"min_trades={MIN_TRADES})")


if __name__ == "__main__":
    test_frac_windows()
    test_build_report_structure()
    print("\nall gatereport tests passed ✅")
