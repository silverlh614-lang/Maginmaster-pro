"""Offline tests for paginated kline history (Phase 2 표본 확보) — the page
fetcher is injected, no network. Run:  python -m tests.test_backtest_history"""
from __future__ import annotations

from tests.test_bybit import _c                            # noqa: F401  (DATA_DIR isolation)

from app.trading_bybit.backtest.engine import PAGE_LIMIT, fetch_history  # noqa: E402

MIN = 60_000  # 1 minute in ms


def _venue(total_bars: int):
    """Fake venue with `total_bars` 15m bars ending at ts = total_bars*15MIN.
    Returns (fetch_page, call_counter) mimicking Bybit paging semantics:
    newest-first pages of up to PAGE_LIMIT bars, `end` inclusive."""
    all_ts = [i * 15 * MIN for i in range(1, total_bars + 1)]
    calls = {"n": 0}

    def fetch_page(symbol, interval, end_ms):
        calls["n"] += 1
        ts = [t for t in all_ts if end_ms is None or t <= end_ms]
        page = ts[-PAGE_LIMIT:]
        return [_c(t, 100, 101, 99, 100.5) for t in page]

    return fetch_page, calls


def test_single_page_enough():
    fp, calls = _venue(3000)
    out = fetch_history("BTCUSDT", "15", 500, fetch_page=fp)
    assert len(out) == 500 and calls["n"] == 1, (len(out), calls)
    assert out[0].ts_ms < out[-1].ts_ms                    # oldest → newest
    print("ok  single page when bars <= 1000 (1 request)")


def test_pagination_past_1000():
    fp, calls = _venue(6000)
    out = fetch_history("BTCUSDT", "15", 2880, fetch_page=fp)   # 30일치 15m
    assert len(out) == 2880, len(out)
    assert calls["n"] == 3, calls                          # 1000+1000+881+
    # 연속성: 매 봉 간격이 정확히 15분 (누락·중복 없음)
    diffs = {out[i + 1].ts_ms - out[i].ts_ms for i in range(len(out) - 1)}
    assert diffs == {15 * MIN}, diffs
    # 최신(미완성) 봉은 제외 — 마지막 봉이 원장의 마지막 봉이 아니어야 함
    assert out[-1].ts_ms == 6000 * 15 * MIN - 15 * MIN
    print("ok  pagination stitches 2880 bars over 3 requests, gapless")


def test_venue_exhausted_early():
    fp, calls = _venue(1500)                               # 요청보다 짧은 히스토리
    out = fetch_history("BTCUSDT", "15", 5000, fetch_page=fp)
    assert len(out) == 1499, len(out)                      # 전부 (미완성 1봉 제외)
    assert calls["n"] == 2, calls                          # 1000 + 500(<limit) → stop
    print("ok  short venue history → returns all available, stops early")


def test_empty_venue():
    out = fetch_history("BTCUSDT", "15", 100,
                        fetch_page=lambda s, i, e: [])
    assert out == []
    print("ok  empty venue → empty list, no crash")


def test_backtest_rows_carry_bar_time():
    """CSV 내보내기용 trade_rows 에는 이벤트가 발생한 BAR 시각(ts, ISO)이
    찍혀야 한다 — 라이브 저널은 기록 시각 폴백 유지."""
    from app.trading_bybit.backtest.engine import _MemJournal, _PermissiveRisk
    from app.trading_bybit.config import SYMBOL_SPECS, BybitConfig
    from app.trading_bybit.execution.position import PositionManager
    from app.trading_bybit.models import Side, TradeSignal

    pm = PositionManager(SYMBOL_SPECS["BTC"], BybitConfig(), _PermissiveRisk(),
                         _MemJournal(), "backtest", "t")
    sig = TradeSignal(Side.LONG, "T", 80, stop_price=98, entry_hint=100)
    assert pm.try_open(sig, 100, 2.0, ts=1_700_000_000)        # 2023-11-14 UTC
    pm.manage(_c(1_700_000_900_000, 100, 100.5, 97, 97.5), 2.0)  # stop -> CLOSE
    rows = pm.journal.rows
    assert rows[0]["event"] == "OPEN" and rows[0]["ts"].startswith("2023-11-14"), rows[0]
    assert rows[-1]["event"] == "CLOSE" and rows[-1]["ts"].startswith("2023-11-14"), rows[-1]
    print("ok  backtest journal rows carry bar timestamps (ISO)")


def test_structural_risk_caps_pyramiding():
    """백테스트 리스크 심은 합산 오픈리스크 캡을 라이브와 동일하게 집행해야
    한다 — 캡을 넘는 애드업 스택(-5R급 손실의 원인)은 차단."""
    from app.trading_bybit.backtest.engine import _StructuralRisk
    from app.trading_bybit.config import BybitConfig

    r = _StructuralRisk(BybitConfig())            # cap 2% = $4 on $200
    assert r.allow_entry(0, 0.0, 2.0, equity_usd=200.0)[0]
    assert r.allow_entry(1, 2.0, 2.0, is_add=True, equity_usd=200.0)[0]
    ok, why = r.allow_entry(1, 4.0, 2.0, is_add=True, equity_usd=200.0)
    assert not ok and "total_open_risk" in why, why
    print("ok  structural risk shim (open-risk cap enforced in backtests)")


if __name__ == "__main__":
    test_single_page_enough()
    test_pagination_past_1000()
    test_venue_exhausted_early()
    test_empty_venue()
    test_backtest_rows_carry_bar_time()
    test_structural_risk_caps_pyramiding()
    print("\nall history pagination tests passed ✅")
