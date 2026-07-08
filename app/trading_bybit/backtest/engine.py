"""@responsibility 백테스트 엔진 — Bybit 과거 kline을 라이브와 동일한 컨텍스트·포지션 FSM으로 리플레이

Backtest engine. Fetches historical Bybit klines (entry + higher timeframe)
and replays them bar-by-bar, rebuilding the exact BybitContext the live bot
would have seen and driving the SAME PositionManager FSM. Risk daily caps are
intentionally OFF (permissive gate) — the goal is raw strategy statistics,
the Phase 2 gate that must pass before any live capital. Settlement is the
FSM's own stop/target/trailing against each bar's high/low (no look-ahead).
"""
from __future__ import annotations

from ..config import SYMBOL_SPECS, BybitConfig, SymbolSpec
from ..indicators import atr
from ..models import Candle
from ..strategies import make_strategy
from ..strategies.base import BybitContext
from ..execution.position import PositionManager

BYBIT_REST = "https://api.bybit.com/v5/market/kline"


def _interval_min(interval: str) -> int:
    table = {"D": 1440, "W": 10080, "M": 43200}
    return table.get(interval, int(interval))


PAGE_LIMIT = 1000        # Bybit v5 kline hard cap per request


def _fetch_page(symbol: str, interval: str, end_ms: int | None) -> list[Candle]:
    """One public kline page (≤1000 bars), any order. end_ms bounds the page
    (inclusive); None = newest page."""
    import httpx    # lazy: keeps the module importable in offline tests
    params = {"category": "linear", "symbol": symbol, "interval": interval,
              "limit": PAGE_LIMIT}
    if end_ms is not None:
        params["end"] = end_ms
    with httpx.Client(timeout=15, headers={"User-Agent": "coinmaster-pro"}) as c:
        r = c.get(BYBIT_REST, params=params)
        r.raise_for_status()
        data = r.json()
    if data.get("retCode") != 0:
        raise ValueError(f"bybit retCode {data.get('retCode')}: {data.get('retMsg')}")
    return [Candle(ts_ms=int(x[0]), open=float(x[1]), high=float(x[2]),
                   low=float(x[3]), close=float(x[4]), volume=float(x[5]))
            for x in data.get("result", {}).get("list", [])]


def fetch_history(symbol: str, interval: str, bars: int,
                  fetch_page=_fetch_page) -> list[Candle]:
    """Up to `bars` CLOSED klines, oldest→newest, paginated past the venue's
    1000-bar page cap by walking backwards (end = oldest_ts - 1). Stops early
    when the venue has no older history. Phase 2 게이트의 표본 확보 통로."""
    by_ts: dict[int, Candle] = {}
    end_ms: int | None = None
    while len(by_ts) < bars + 1:             # +1: forming newest bar dropped below
        page = fetch_page(symbol, interval, end_ms)
        if not page:
            break
        for c in page:
            by_ts[c.ts_ms] = c
        if len(page) < PAGE_LIMIT:            # venue exhausted — no older bars
            break
        end_ms = min(c.ts_ms for c in page) - 1
    out = sorted(by_ts.values(), key=lambda c: c.ts_ms)
    return out[:-1][-bars:] if out else out   # drop forming bar, keep newest N


class _MemJournal:
    """In-memory journal so a backtest never touches the live CSV."""
    def __init__(self):
        self.rows: list[dict] = []

    def append(self, rec: dict) -> dict:
        self.rows.append(rec)
        return rec


class _PermissiveRisk:
    """Backtest risk shim: no daily caps, no kill switch — raw stats only."""
    def allow_entry(self, *a, **k):
        return True, ""

    def record_ok(self): ...
    def record_error(self, e): ...


def replay(symbol: str, strategy_name: str, cfg: BybitConfig,
           entry_candles: list[Candle] | None = None,
           htf_candles: list[Candle] | None = None,
           bars: int = 1000) -> dict:
    """Replay one symbol. Candles can be injected (offline tests) or fetched
    (paginated, `bars` entry-TF bars). Returns {trades, closes, equity_curve,
    snapshots}."""
    spec: SymbolSpec = SYMBOL_SPECS[symbol.upper()]
    if entry_candles is None:
        entry_candles = fetch_history(spec.symbol, cfg.entry_interval, bars)
    if htf_candles is None:
        # HTF must cover the same span plus indicator warmup (ADX 등)
        span = _interval_min(cfg.entry_interval) * bars
        htf_bars = span // _interval_min(cfg.htf_interval) + 2 * (2 * cfg.adx_period + 1)
        htf_candles = fetch_history(spec.symbol, cfg.htf_interval, htf_bars)
    if not entry_candles or not htf_candles:
        return {"trades": [], "closes": [], "equity_curve": [], "snapshots": 0}

    strategy = make_strategy(strategy_name, cfg)
    pm = PositionManager(spec, cfg, _PermissiveRisk(), _MemJournal(), "backtest",
                         strategy_name)
    journal: _MemJournal = pm.journal   # type: ignore[assignment]
    entry_min = _interval_min(cfg.entry_interval)
    htf_min = _interval_min(cfg.htf_interval)
    warmup = max(cfg.box_lookback, cfg.atr_period, cfg.vol_ma_period,
                 cfg.ema_period, cfg.range_lookback, 2 * cfg.adx_period + 1) + 2

    equity_curve: list[float] = []
    snapshots = 0
    for i in range(warmup, len(entry_candles)):
        bar = entry_candles[i]
        closed_entry = entry_candles[:i + 1]
        bar_close_t = bar.ts_ms + entry_min * 60_000
        closed_htf = [c for c in htf_candles
                      if c.ts_ms + htf_min * 60_000 <= bar_close_t]
        if len(closed_htf) < warmup:
            continue
        snapshots += 1
        atr_val = atr(closed_entry, cfg.atr_period)

        pm.flatten_if_closed()
        pm.manage(bar, atr_val)
        ctx = BybitContext(symbol=spec.key, htf_candles=closed_htf,
                           entry_candles=closed_entry, equity_usd=pm.equity,
                           now=bar_close_t / 1000,
                           open_position_side=(pm.pos.side.value
                                               if pm.pos and pm.pos.state.value == "OPEN"
                                               else None))
        sig = strategy.evaluate(ctx)
        if sig is not None:
            p = pm.pos
            if p and p.state.value == "OPEN" and sig.side is p.side:
                pm.try_add(sig, bar.close, atr_val or 0.0, ctx.now)
            elif not (p and p.state.value == "OPEN"):
                pm.try_open(sig, bar.close, atr_val or 0.0, ctx.now)
        equity_curve.append(round(pm.equity, 4))

    # force-close any position still open at the end of the data
    if pm.pos and pm.pos.state.value == "OPEN":
        pm._close(entry_candles[-1].close, "backtest end", ctx.now)

    closes = [r for r in journal.rows if r["event"] == "CLOSE"]
    return {"trades": journal.rows, "closes": closes,
            "equity_curve": equity_curve, "snapshots": snapshots,
            "final_equity": round(pm.equity, 4)}
