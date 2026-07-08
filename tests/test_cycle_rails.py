"""Offline tests for the 200-week MA auto-computation (no network — the weekly
close fetch is monkeypatched). Run:  python -m tests.test_cycle_rails"""
from __future__ import annotations

from app import cycle_rails, snapshot


def _reset():
    cycle_rails._cache.update(value=None, as_of=0.0, source=None)


def test_live_sma_and_cache():
    _reset()
    calls = {"n": 0}

    def fake():
        calls["n"] += 1
        return [float(i) for i in range(1, 201)], "binance"   # closes 1..200

    orig = cycle_rails._weekly_closes
    cycle_rails._weekly_closes = fake
    try:
        r = cycle_rails.get_ma_200w()
        assert r["live"] and r["source"] == "binance"
        assert abs(r["value"] - 100.5) < 1e-9, r          # mean(1..200)=100.5
        # second call inside TTL must hit cache, not refetch
        cycle_rails.get_ma_200w()
        assert calls["n"] == 1, calls
    finally:
        cycle_rails._weekly_closes = orig
    print("ok  live 200W SMA + TTL cache (no refetch within TTL)")


def test_too_few_bars_falls_back():
    _reset()
    orig = cycle_rails._weekly_closes
    cycle_rails._weekly_closes = lambda: ([100.0] * 50, "binance")
    try:
        r = cycle_rails.get_ma_200w()
        assert not r["live"] and r["source"] == "snapshot-anchor"
        assert r["value"] == round(snapshot.MA_200W, 2), r
    finally:
        cycle_rails._weekly_closes = orig
    print("ok  <200 weekly bars → snapshot anchor fallback")


def test_fetch_error_falls_back():
    _reset()
    def boom():
        raise RuntimeError("network down")
    orig = cycle_rails._weekly_closes
    cycle_rails._weekly_closes = boom
    try:
        r = cycle_rails.get_ma_200w()
        assert not r["live"] and r["value"] == round(snapshot.MA_200W, 2), r
    finally:
        cycle_rails._weekly_closes = orig
    print("ok  fetch error → snapshot anchor fallback (panel never breaks)")


def test_stale_live_beats_anchor():
    _reset()
    cycle_rails._cache.update(value=59000.0, as_of=1.0, source="binance")  # very old
    orig = cycle_rails._weekly_closes
    cycle_rails._weekly_closes = lambda: (_ for _ in ()).throw(RuntimeError("down"))
    try:
        r = cycle_rails.get_ma_200w()
        assert r["live"] and "stale" in r["source"] and r["value"] == 59000.0, r
    finally:
        cycle_rails._weekly_closes = orig
        _reset()
    print("ok  stale live value beats static anchor")


if __name__ == "__main__":
    test_live_sma_and_cache()
    test_too_few_bars_falls_back()
    test_fetch_error_falls_back()
    test_stale_live_beats_anchor()
    print("\nall cycle-rails tests passed ✅")
