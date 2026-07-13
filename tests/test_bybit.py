"""Offline tests for the Bybit leverage-margin package (Part 5) — synthetic
candles, no network. Run:  python -m tests.test_bybit"""
from __future__ import annotations

import os
import tempfile

# isolate journal/state files before importing the package
_tmp = tempfile.mkdtemp(prefix="bybit-test-")
os.environ["DATA_DIR"] = _tmp

from app.trading_bybit import indicators as ind          # noqa: E402
from app.trading_bybit.config import SYMBOL_SPECS, BybitConfig  # noqa: E402
from app.trading_bybit.execution.position import PositionManager  # noqa: E402
from app.trading_bybit.models import Candle, Side, TradeSignal  # noqa: E402
from app.trading_bybit.risk import BybitRiskManager, size_position  # noqa: E402
from app.trading_bybit.store import BotState, Journal    # noqa: E402
from app.trading_bybit.strategies import make_strategy    # noqa: E402
from app.trading_bybit.strategies.base import BybitContext  # noqa: E402

BTC = SYMBOL_SPECS["BTC"]


def _c(ts, o, h, l, cl, v=100.0):
    return Candle(ts_ms=ts, open=o, high=h, low=l, close=cl, volume=v)


# ------------------------------------------------------------- indicators

def test_indicators():
    vals = [1, 2, 3, 4, 5, 6]
    e = ind.ema(vals, 3)
    assert len(e) == len(vals) and e[-1] > e[0]
    assert ind.sma([1, 2, 3, 4], 2) == 3.5
    assert ind.sma([1], 5) is None

    candles = [_c(i * 60000, 100, 102, 98, 101) for i in range(20)]
    a = ind.atr(candles, 14)
    assert a is not None and a > 0

    prev = _c(0, 100, 101, 97, 98)          # bearish body 2
    cur = _c(1, 97.5, 104, 97, 103)         # bullish body 5.5, engulfs
    assert ind.bullish_engulfing(prev, cur)
    assert not ind.bearish_engulfing(prev, cur)
    prev2 = _c(0, 98, 104, 97, 103)         # bullish
    cur2 = _c(1, 103.5, 104, 96, 97)        # bearish engulf
    assert ind.bearish_engulfing(prev2, cur2)

    box = ind.box_range([_c(i, 100, 110, 100, 105) for i in range(21)]
                        + [_c(21, 105, 130, 105, 128)], 20)
    assert box is not None and box[0] == 110 and box[1] == 100
    print("ok  indicators")


# ------------------------------------------------------------- sizing

def test_sizing():
    # $200 equity, 1% risk = $2; entry 100 stop 98 (dist 2) -> qty 1.0
    qty, risk, why = size_position(200, 1.0, 100, 98, BTC, 5.0)
    assert qty == 1.0 and abs(risk - 2.0) < 1e-9, (qty, risk, why)

    # leverage cap: huge risk clamps notional to equity*max
    qty, risk, why = size_position(100, 50.0, 100, 99, BTC, 5.0)
    assert abs(qty * 100 - 500) < 1e-6, (qty, "notional should clamp to 500")

    # below-min qty -> refuse
    qty, risk, why = size_position(100, 0.01, 50000, 49000, BTC, 5.0)
    assert qty == 0.0 and "min" in why
    print("ok  sizing (risk %, leverage cap, min qty)")


# ------------------------------------------------------------- risk gate

def test_risk_gate():
    cfg = BybitConfig()
    cfg.max_concurrent_positions = 1
    cfg.max_total_open_risk_pct = 2.0        # $4 on $200
    risk = BybitRiskManager(cfg, Journal(), BotState())
    ok, _ = risk.allow_entry(0, 0.0, 2.0)
    assert ok
    # second concurrent position blocked
    ok, why = risk.allow_entry(1, 2.0, 2.0)
    assert not ok and "concurrent" in why
    # add that would exceed total open-risk cap blocked
    ok, why = risk.allow_entry(1, 3.0, 2.0, is_add=True)
    assert not ok and "open_risk" in why
    # kill switch
    for _ in range(cfg.max_consecutive_errors):
        risk.record_error("boom")
    assert risk.kill_switch
    assert not risk.allow_entry(0, 0.0, 1.0)[0]
    risk.reset_kill()
    assert risk.allow_entry(0, 0.0, 1.0)[0]
    print("ok  risk gate (concurrent, open-risk cap, kill switch)")


def test_risk_caps_follow_compounded_equity():
    """총자산대비 rule (복리 단타 비법서 시스템 #2): the open-risk cap must be
    measured against CURRENT equity, not the starting stake."""
    cfg = BybitConfig()
    cfg.max_total_open_risk_pct = 2.0        # 2% of equity
    risk = BybitRiskManager(cfg, Journal(), BotState())
    # $3 risk on $200 equity (cap $4) → allowed
    assert risk.allow_entry(0, 0.0, 3.0, equity_usd=200.0)[0]
    # same $3 risk after equity halved to $100 (cap $2) → blocked
    ok, why = risk.allow_entry(0, 0.0, 3.0, equity_usd=100.0)
    assert not ok and "open_risk" in why, why
    # after equity compounds to $400 (cap $8), $6 risk → allowed
    assert risk.allow_entry(0, 0.0, 6.0, equity_usd=400.0)[0]
    # no equity passed → falls back to config stake (back-compat)
    assert risk.allow_entry(0, 0.0, 3.0)[0]
    print("ok  risk caps follow compounded equity (총자산대비)")


# ------------------------------------------------------------- position FSM

def _pm(cfg=None):
    cfg = cfg or BybitConfig()
    risk = BybitRiskManager(cfg, Journal(), BotState())
    return PositionManager(BTC, cfg, risk, Journal(), "paper", "trend_breakout")


def test_fsm_stop_loss():
    pm = _pm()
    sig = TradeSignal(Side.LONG, "T", 80, stop_price=98, entry_hint=100)
    assert pm.try_open(sig, 100, atr_val=2.0, ts=0)
    # candle dumps through the stop -> LOSS close at stop
    pm.manage(_c(60000, 100, 100.5, 97, 97.5), 2.0)
    assert pm.pos.state.value == "CLOSED"
    assert pm.pos.realized_pnl_usd < 0
    print("ok  fsm stop-loss")


def test_fsm_partial_then_trail():
    from app.trading_bybit import store
    store.TRADES_CSV.unlink(missing_ok=True)   # isolate the aggregate assertion
    cfg = BybitConfig()
    cfg.rr_target = 2.0
    cfg.partial_tp_frac = 0.5
    pm = _pm(cfg)
    sig = TradeSignal(Side.LONG, "T", 80, stop_price=98, entry_hint=100)
    assert pm.try_open(sig, 100, atr_val=2.0, ts=0)  # 1R = $2 dist, target 104
    # bar tags 2R target -> partial 50% + breakeven stop
    pm.manage(_c(60000, 100, 104.5, 100, 103), 2.0)
    assert pm.pos.partial_done and pm.pos.open_qty < 1.0
    assert pm.pos.trail_price is not None
    banked = pm.pos.realized_pnl_usd
    assert banked > 0
    # next bar pulls back to breakeven trail -> full close, still net positive
    # (total < banked partial only by the small exit fee on the breakeven leg)
    pm.manage(_c(120000, 103, 103.2, 99, 99.5), 2.0)
    assert pm.pos.state.value == "CLOSED"
    assert pm.pos.realized_pnl_usd > 0
    # journal: exactly one settled CLOSE row (no double counting the partial)
    agg = pm.journal.aggregate()
    assert agg["trades"] == 1 and agg["settled"] == 1, agg
    # PARTIAL row records its own realized cash + leg R (result stays blank)
    prow = next(r for r in pm.journal.tail(10) if r["event"] == "PARTIAL")
    assert prow["result"] == "" and float(prow["pnl_usd"]) > 0, prow
    assert float(prow["r_multiple"]) > 0, prow
    print("ok  fsm partial + trailing + single settled row + partial pnl logged")


def test_fsm_pyramiding():
    cfg = BybitConfig()
    cfg.pyramid_enabled = True
    cfg.pyramid_max_adds = 2
    cfg.pyramid_min_r = 1.0
    cfg.max_total_open_risk_pct = 10.0
    pm = _pm(cfg)
    sig = TradeSignal(Side.LONG, "T", 80, stop_price=98, entry_hint=100)
    assert pm.try_open(sig, 100, 2.0, 0)
    # price 1R ahead (102) -> add allowed
    add = TradeSignal(Side.LONG, "T", 80, stop_price=100, entry_hint=102)
    assert pm.try_add(add, 102, 2.0, 1)
    assert pm.pos.adds == 1 and len(pm.pos.units) == 2
    print("ok  fsm pyramiding add")


# ------------------------------------------------------------- strategy

def _uptrend_breakout():
    """HTF: 20-bar box [100,110] then a breakout bar to 130.
    Entry: uptrend closing with a bullish engulfing + volume spike."""
    htf = []
    for i in range(24):
        base = 100 + (i % 3)              # oscillate inside [100,110]
        htf.append(_c(i * 3600000, base, 110, 100, 105))
    htf.append(_c(24 * 3600000, 108, 132, 107, 130, 500))  # breakout up
    entry = []
    px = 110.0
    for i in range(28):
        entry.append(_c(i * 900000, px, px + 1, px - 1, px + 0.5, 100))
        px += 0.2
    prev = _c(28 * 900000, 120, 121, 117, 118, 100)         # bearish
    cur = _c(29 * 900000, 117.5, 123.5, 117, 123, 300)      # bullish engulf + vol
    entry += [prev, cur]
    return htf, entry


def test_strategy_signal():
    cfg = BybitConfig()
    strat = make_strategy("trend_breakout", cfg)
    htf, entry = _uptrend_breakout()
    ctx = BybitContext(symbol="BTC", htf_candles=htf, entry_candles=entry,
                       equity_usd=200, now=0)
    sig = strat.evaluate(ctx)
    assert sig is not None and sig.side is Side.LONG, sig
    assert sig.stop_price < sig.entry_hint
    print("ok  strategy trend-breakout LONG signal")


def _downtrend_breakdown_lowvol():
    """HTF: 20-bar box [100,110] then a breakdown bar to 80.
    Entry: downtrend ending with a bearish engulfing but NO volume spike
    (vol == MA, below MA*1.2) — the H8 scenario from the source p31."""
    htf = []
    for i in range(24):
        base = 100 + (i % 3)
        htf.append(_c(i * 3600000, base, 110, 100, 105))
    htf.append(_c(24 * 3600000, 102, 103, 78, 80, 500))      # breakdown
    entry = []
    px = 130.0
    for i in range(28):
        entry.append(_c(i * 900000, px, px + 1, px - 1, px - 0.5, 100))
        px -= 0.5
    prev = _c(28 * 900000, 118.0, 119.0, 117.2, 118.6, 100)  # bullish
    cur = _c(29 * 900000, 118.8, 119.0, 114.0, 115.0, 100)   # bearish engulf, vol=MA
    entry += [prev, cur]
    return htf, entry


def test_short_vol_exempt_flag():
    """H8 infra: SHORT with a no-volume bearish engulfing fires ONLY when the
    short_vol_exempt flag is on (default off keeps the volume gate)."""
    htf, entry = _downtrend_breakdown_lowvol()

    cfg = BybitConfig()
    assert cfg.short_vol_exempt is False          # 라이브 기본값은 off
    strat = make_strategy("trend_breakout", cfg)
    ctx = BybitContext(symbol="BTC", htf_candles=htf, entry_candles=entry,
                       equity_usd=200, now=0)
    d = strat.diagnose(ctx)
    assert d and d["allowed"] == "SHORT", d
    assert strat.evaluate(ctx) is None            # 거래량 미달 → 차단 (기존 동작)

    cfg2 = BybitConfig()
    cfg2.short_vol_exempt = True                  # 백테스트 A/B의 on 케이스
    strat2 = make_strategy("trend_breakout", cfg2)
    sig = strat2.evaluate(ctx)
    assert sig is not None and sig.side is Side.SHORT, sig
    assert sig.stop_price > sig.entry_hint        # SHORT: 스탑은 진입가 위
    print("ok  H8 short_vol_exempt flag (off=blocked, on=SHORT fires)")


# ------------------------------------------------------------- backtest

def _coherent_series(n=200):
    """One 15m price path (consolidation then uptrend) aggregated 4:1 into an
    aligned 1h series — so htf/entry timestamps line up like real klines."""
    import math
    entry = []
    for i in range(n):
        trend = 0.0 if i < 80 else (i - 80) * 0.6
        base = 100 + trend + 3 * math.sin(i / 3.0)
        o = base
        c = base + (0.9 if i % 2 == 0 else -0.9)
        h, l = max(o, c) + 0.7, min(o, c) - 0.7
        vol = 100 + (250 if i % 7 == 0 else 0)
        entry.append(_c(i * 900000, o, h, l, c, vol))
    htf = []
    for j in range(0, n - 3, 4):
        g = entry[j:j + 4]
        htf.append(_c(g[0].ts_ms, g[0].open, max(x.high for x in g),
                      min(x.low for x in g), g[-1].close, sum(x.volume for x in g)))
    return htf, entry


def test_backtest_replay():
    from app.trading_bybit.backtest.engine import replay
    from app.trading_bybit.backtest.metrics import compute
    cfg = BybitConfig()
    htf, entry = _coherent_series(220)
    r = replay("BTC", "trend_breakout", cfg, entry_candles=entry, htf_candles=htf)
    m = compute(r["closes"], cfg.equity_usd, r.get("final_equity", cfg.equity_usd))
    assert isinstance(m["trades"], int)
    assert r["snapshots"] > 0, "replay produced no evaluatable bars"
    print(f"ok  backtest replay (snapshots={r['snapshots']}, trades={m['trades']})")


def test_candles_export():
    """bot.candles() must build OHLCV + EMA when the collector has candles —
    regression for a missing `ema` import that 500'd the /candles endpoint
    only once real bars arrived (empty short-circuited past the bug)."""
    from app.trading_bybit.bot import BybitManager
    from app.trading_bybit.config import BybitConfig
    mgr = BybitManager(BybitConfig())
    bot = mgr.bots["BTC"]
    col = bot.collector
    px = 63000.0
    for i in range(30):
        ts = 1_700_000_000_000 + i * 900_000
        col._bars[col.entry_interval][ts] = Candle(ts, px, px + 50, px - 50, px + 10, 100 + i)
        px += 5
    out = bot.candles("entry", 120)
    assert len(out["candles"]) == 30 and len(out["ema5"]) == 30, out
    assert out["candles"][0][0] < out["candles"][-1][0]     # ascending by time
    print("ok  candles export (ema import regression)")


def _klines_bybit(n=6, base=63000.0):
    # newest-first: [start, o, h, l, c, v, turnover] as strings
    rows = []
    for i in range(n):
        ts = 1_700_000_000_000 + i * 900_000
        px = base + i * 5
        rows.append([str(ts), str(px), str(px + 50), str(px - 50),
                     str(px + 10), "100", "0"])
    return list(reversed(rows))


def _klines_binance(n=6, base=63000.0):
    # oldest-first: [openTime, o, h, l, c, v, closeTime, ...]
    rows = []
    for i in range(n):
        ts = 1_700_000_000_000 + i * 900_000
        px = base + i * 5
        rows.append([ts, str(px), str(px + 50), str(px - 50), str(px + 10),
                     "100", ts + 899_999, "0", 0, "0", "0", "0"])
    return rows


def test_kline_source_failover():
    """Bybit 403 (geo-block) must fail over to Binance, then OKX, and populate
    the same Candle buffer. No real network — httpx.MockTransport."""
    import asyncio
    import httpx
    from app.trading_bybit.collectors.kline import KlineCollector

    def make_handler(fail_hosts):
        def handler(request: httpx.Request) -> httpx.Response:
            host = request.url.host
            if host in fail_hosts:
                return httpx.Response(403, text="forbidden")
            if host == "fapi.binance.com":
                return httpx.Response(200, json=_klines_binance())
            if host == "www.okx.com":
                # okx: code "0", data newest-first [ts,o,h,l,c,vol,volCcy,...]
                rows = [[r[0], r[1], r[2], r[3], r[4], r[5], "0", "0", "1"]
                        for r in reversed(_klines_binance())]
                return httpx.Response(200, json={"code": "0", "data": rows})
            if host == "api.bybit.com":
                return httpx.Response(200, json={"retCode": 0, "retMsg": "OK",
                    "result": {"list": _klines_bybit()}})
            return httpx.Response(404)
        return handler

    async def run_case(fail_hosts, expect_source):
        col = KlineCollector("BTCUSDT", "15", "60", limit=200)
        transport = httpx.MockTransport(make_handler(fail_hosts))
        async with httpx.AsyncClient(transport=transport) as client:
            await col.poll_once(client)
        assert col.source == expect_source, (fail_hosts, col.source)
        assert len(col.entry_closed()) >= 4, col.status()
        assert col.last_price() is not None
        return col

    # primary works
    asyncio.run(run_case(set(), "bybit"))
    # bybit geo-blocked -> binance
    asyncio.run(run_case({"api.bybit.com"}, "binance"))
    # bybit + binance blocked -> okx
    asyncio.run(run_case({"api.bybit.com", "fapi.binance.com"}, "okx"))

    # all blocked -> raises, source none
    async def all_fail():
        col = KlineCollector("BTCUSDT", "15", "60")
        transport = httpx.MockTransport(make_handler(
            {"api.bybit.com", "fapi.binance.com", "www.okx.com"}))
        async with httpx.AsyncClient(transport=transport) as client:
            try:
                await col.poll_once(client)
                assert False, "should have raised"
            except RuntimeError:
                pass
        assert col.source == "none"
    asyncio.run(all_fail())
    print("ok  kline source failover (bybit->binance->okx)")


def test_kline_cross_validation():
    """The live source's price is cross-checked against a second venue; a gap
    beyond the threshold flags divergence. No network — MockTransport."""
    import asyncio
    import httpx
    from app.trading_bybit.collectors.kline import KlineCollector
    from app.trading_bybit.models import Candle

    def make_client(binance_last):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "fapi.binance.com":
                return httpx.Response(200, json=[[1_700_000_000_000, "1", "1",
                    "1", str(binance_last), "1", 1, "0", 0, "0", "0", "0"]])
            return httpx.Response(403)      # bybit/okx unreachable here
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def run(binance_last):
        col = KlineCollector("BTCUSDT", "15", "60")
        col.source = "bybit"                        # pretend bybit is live
        col._forming["15"] = Candle(1, 1, 1, 1, 63000.0, 1)   # my price = 63000
        async with make_client(binance_last) as client:
            await col._cross_check(client)
        return col

    # 63700 vs 63000 -> ~1.1% gap > 0.8% threshold -> divergence
    c = asyncio.run(run(63700.0))
    assert c.cross_source == "binance" and c.divergence, c.status()
    assert c.cross_dev_pct > 0.8, c.cross_dev_pct
    # 63020 vs 63000 -> ~0.03% -> agrees, no divergence
    c = asyncio.run(run(63020.0))
    assert c.cross_source == "binance" and not c.divergence, c.status()
    print("ok  kline cross-source validation (divergence flag)")


def test_state_persistence():
    """A live open position must round-trip through PositionStore so a trade
    in progress survives a restart/redeploy. (Equity round-trips through the
    unified AccountLedger/AccountStore — tests/test_account_ledger.py.)"""
    from app.trading_bybit.config import BybitConfig, SYMBOL_SPECS
    from app.trading_bybit.execution.position import PositionManager
    from app.trading_bybit.models import Position, PositionState, Side, Unit
    from app.trading_bybit.risk import BybitRiskManager
    from app.trading_bybit.store import BotState, Journal, PositionStore

    cfg = BybitConfig()
    spec = SYMBOL_SPECS["BTC"]
    j = Journal()
    risk = BybitRiskManager(cfg, j, BotState())
    pm = PositionManager(spec, cfg, risk, j, "paper", "trend_breakout")
    # simulate a partially-managed open long
    pm.pos = Position(symbol="BTCUSDT", side=Side.LONG,
                      units=[Unit(Side.LONG, 63000.0, 0.01, 62500.0, 1.0,
                                  fee_usd=0.3)],
                      initial_risk_usd=5.0, target_price=64000.0,
                      trail_price=62800.0, realized_pnl_usd=2.1,
                      partial_done=True, state=PositionState.OPEN)
    pm.bars_in_trade = 9

    store = PositionStore()
    store.save("BTC", pm.to_state())
    assert "equity" not in store.load("BTC")       # equity는 계좌 원장 소관

    # a fresh manager (as after a redeploy) restores the exact state
    pm2 = PositionManager(spec, cfg, risk, j, "paper", "trend_breakout")
    pm2.load_state(store.load("BTC"))
    assert pm2.equity == cfg.equity_usd            # 심볼 기록은 equity 미보유
    assert pm2.bars_in_trade == 9
    p = pm2.pos
    assert p is not None and p.state == PositionState.OPEN and p.side == Side.LONG
    assert p.partial_done and abs(p.trail_price - 62800.0) < 1e-9
    assert abs(p.avg_entry - 63000.0) < 1e-6 and abs(p.open_qty - 0.01) < 1e-9
    assert abs(p.realized_pnl_usd - 2.1) < 1e-9

    # nothing saved for another symbol -> fresh start, no crash
    pm3 = PositionManager(spec, cfg, risk, j, "paper", "trend_breakout")
    pm3.load_state(store.load("ETH"))
    assert pm3.pos is None and pm3.equity == cfg.equity_usd
    print("ok  state persistence (equity + open position round-trip)")


if __name__ == "__main__":
    test_indicators()
    test_sizing()
    test_risk_gate()
    test_risk_caps_follow_compounded_equity()
    test_fsm_stop_loss()
    test_fsm_partial_then_trail()
    test_fsm_pyramiding()
    test_strategy_signal()
    test_short_vol_exempt_flag()
    test_backtest_replay()
    test_candles_export()
    test_kline_source_failover()
    test_kline_cross_validation()
    test_state_persistence()
    print("\nall bybit tests passed ✅")
