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


def fetch_klines(symbol: str, interval: str, limit: int = 1000) -> list[Candle]:
    """Public Bybit klines, oldest→newest, forming bar dropped."""
    import httpx    # lazy: keeps the module importable in offline tests
    params = {"category": "linear", "symbol": symbol, "interval": interval,
              "limit": min(limit, 1000)}
    with httpx.Client(timeout=15, headers={"User-Agent": "coinmaster-pro"}) as c:
        r = c.get(BYBIT_REST, params=params)
        r.raise_for_status()
        data = r.json()
    if data.get("retCode") != 0:
        raise ValueError(f"bybit retCode {data.get('retCode')}: {data.get('retMsg')}")
    rows = data.get("result", {}).get("list", [])
    out = [Candle(ts_ms=int(x[0]), open=float(x[1]), high=float(x[2]),
                  low=float(x[3]), close=float(x[4]), volume=float(x[5]))
           for x in rows]
    out.sort(key=lambda c: c.ts_ms)
    return out[:-1] if out else out          # drop the still-forming bar


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
           htf_candles: list[Candle] | None = None) -> dict:
    """Replay one symbol. Candles can be injected (offline tests) or fetched.
    Returns {trades, closes, equity_curve, snapshots}."""
    spec: SymbolSpec = SYMBOL_SPECS[symbol.upper()]
    if entry_candles is None:
        entry_candles = fetch_klines(spec.symbol, cfg.entry_interval)
    if htf_candles is None:
        htf_candles = fetch_klines(spec.symbol, cfg.htf_interval)
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
