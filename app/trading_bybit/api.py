"""@responsibility Bybit 관제탑 REST API — /api/bybit/* 봇 제어·저널·백테스트·설정 조회

REST API for the Bybit control tower (/api/bybit/*)."""
from __future__ import annotations

import csv
import datetime as dt
import io

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from .bot import MANAGER
from .config import CONFIG, SYMBOL_SPECS
from .store import FIELDS
from .strategies import STRATEGIES

router = APIRouter(prefix="/api/bybit", tags=["bybit"])


class StartRequest(BaseModel):
    mode: str = "paper"
    strategy: str = "trend_breakout"


class ManualRequest(BaseModel):
    action: str            # close
    symbol: str = "BTC"


@router.get("/status")
def status():
    return MANAGER.status()


@router.post("/start")
async def start(req: StartRequest):
    res = await MANAGER.start(mode=req.mode, strategy=req.strategy)
    if not res.get("ok"):
        raise HTTPException(409, res.get("error", "start failed"))
    return res


@router.post("/stop")
async def stop():
    res = await MANAGER.stop()
    if not res.get("ok"):
        raise HTTPException(409, res.get("error", "stop failed"))
    return res


@router.post("/manual")
def manual(req: ManualRequest):
    res = MANAGER.manual(req.action, symbol=req.symbol)
    if not res.get("ok"):
        raise HTTPException(409, res.get("error", "manual action failed"))
    return res


@router.post("/kill/reset")
def reset_kill():
    MANAGER.risk.reset_kill()
    return {"ok": True}


@router.get("/candles")
def candles(symbol: str = "BTC", tf: str = "entry", limit: int = 120):
    if symbol.upper() not in SYMBOL_SPECS:
        raise HTTPException(422, f"unknown symbol '{symbol}'")
    if tf not in ("entry", "htf"):
        raise HTTPException(422, "tf must be 'entry' or 'htf'")
    return MANAGER.candles(symbol, tf, min(max(limit, 10), 400))


@router.get("/trades")
def trades(limit: int = 50, symbol: str | None = None):
    sym = None if (not symbol or symbol.lower() in ("all", "")) else symbol.upper()
    if sym and sym not in SYMBOL_SPECS:
        raise HTTPException(422, f"unknown symbol '{symbol}'")
    return {"trades": MANAGER.journal.tail(limit, symbol=sym),
            "aggregate": MANAGER.journal.aggregate(symbol=sym),
            "by_symbol": MANAGER.journal.by_symbol(list(MANAGER.bots)),
            "today": MANAGER.risk.today(),
            "symbol": sym or "ALL"}


@router.get("/trades.csv")
def trades_csv(symbol: str | None = None, limit: int = 100_000):
    sym = None if (not symbol or symbol.lower() in ("all", "")) else symbol.upper()
    rows = MANAGER.journal.tail(limit, symbol=sym)[::-1]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS)
    w.writeheader()
    w.writerows(rows)
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    fname = f"bybit_trades_{(symbol or 'all').lower()}_{day}.csv"
    return Response(content="\ufeff" + buf.getvalue(),
                    media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


class BacktestRequest(BaseModel):
    # overrides accepts strings too so interval fields can be swept
    # (H7: htf_interval "60"|"240"|"D") alongside numeric params.
    symbol: str = "BTC"
    strategy: str = "trend_breakout"
    overrides: dict[str, float | str] = {}
    days: float = 10.0    # replay window — paginated fetch past the 1000-bar cap


@router.post("/backtest")
def backtest(req: BacktestRequest):
    import copy

    from .backtest.engine import replay
    from .backtest.metrics import compute

    if req.symbol.upper() not in SYMBOL_SPECS:
        raise HTTPException(422, f"unknown symbol '{req.symbol}'")
    if req.strategy not in STRATEGIES:
        raise HTTPException(422, f"unknown strategy '{req.strategy}'")
    cfg = copy.copy(CONFIG)
    for k, v in req.overrides.items():
        if not hasattr(cfg, k):
            raise HTTPException(422, f"unknown config field '{k}'")
        cur = getattr(cfg, k)
        try:
            if isinstance(cur, bool):   # bool("0") is True — parse explicitly
                val = (v.strip().lower() in ("1", "true", "yes", "on")
                       if isinstance(v, str) else bool(v))
            else:
                val = type(cur)(v)
        except (TypeError, ValueError):
            raise HTTPException(422, f"bad value for '{k}': {v!r}")
        setattr(cfg, k, val)
    from .backtest.engine import _interval_min
    days = min(max(req.days, 1.0), 365.0)
    bars = max(200, int(days * 1440 / _interval_min(cfg.entry_interval)))
    try:
        r = replay(req.symbol, req.strategy, cfg, bars=bars)
    except Exception as e:
        raise HTTPException(502, f"backtest fetch/replay failed: {e}")
    return {"symbol": req.symbol.upper(), "strategy": req.strategy,
            "days": days, "bars": bars,
            "metrics": compute(r["closes"], cfg.equity_usd, r.get("final_equity", cfg.equity_usd)),
            "final_equity": r.get("final_equity"),
            "snapshots": r["snapshots"], "trades": len(r["closes"]),
            "equity_curve": r["equity_curve"][-500:],
            # 저널 CSV 와 동일 컬럼의 이벤트 행 — UI 의 결과 CSV 내보내기용
            "trade_rows": r["trades"][:2000]}


class GateReportRequest(BaseModel):
    symbol: str = "BTC"
    days: float = 30.0
    split: float = 0.7          # IS 비율 (나머지가 OOS)


@router.post("/gatereport")
def gatereport(req: GateReportRequest):
    """H7·H8 판정 매트릭스 실행 — kline 을 (심볼, 인터벌)당 1회만 받아
    모든 조합에 재사용한다. 결과는 판정 '초안'(레지스트리 기록은 사람 몫)."""
    import copy

    from .backtest.engine import _interval_min, fetch_history
    from .backtest.gatereport import H7_IVS, build_report

    if req.symbol.upper() not in SYMBOL_SPECS:
        raise HTTPException(422, f"unknown symbol '{req.symbol}'")
    if not (0.5 <= req.split <= 0.9):
        raise HTTPException(422, "split must be within [0.5, 0.9]")
    spec = SYMBOL_SPECS[req.symbol.upper()]
    cfg = copy.copy(CONFIG)
    days = min(max(req.days, 3.0), 365.0)
    entry_min = _interval_min(cfg.entry_interval)
    bars = max(400, int(days * 1440 / entry_min))
    span_min = bars * entry_min
    try:
        entry = fetch_history(spec.symbol, cfg.entry_interval, bars)
        htf_by_iv: dict = {}
        for iv in {*H7_IVS, cfg.htf_interval}:
            hb = span_min // _interval_min(iv) + 2 * (2 * cfg.adx_period + 1)
            htf_by_iv[iv] = fetch_history(spec.symbol, iv, hb)
    except Exception as e:
        raise HTTPException(502, f"gatereport kline fetch failed: {e}")
    return build_report(spec.key, cfg, entry, htf_by_iv, req.split)


@router.get("/config")
def config():
    return {"config": CONFIG.as_dict(),
            "symbols": list(MANAGER.bots),
            "strategies": list(STRATEGIES),
            "phase": "1 (paper only)"}


@router.get("/live/status")
def live_status():
    """Phase 3 준비 상태 점검 — 키 존재 여부(불리언만)·거래소 도달성·지갑
    조회(읽기 전용). 주문 경로는 게이트 잠금 상태임을 항상 명시한다."""
    from .bybit_client import BybitClient
    c = BybitClient(CONFIG)
    out = {
        "testnet": CONFIG.testnet,
        "live_enabled": CONFIG.live_enabled,
        "keys_configured": c.keys_configured,
        "base_url": c.base,
        "order_path": "LOCKED — 백테스트 게이트 통과 후 Phase 3 에서 해제",
    }
    try:
        out["exchange_reachable"] = c.server_time() is not None
    except Exception as e:
        out["exchange_reachable"] = False
        out["reach_error"] = type(e).__name__
    if c.keys_configured:
        try:
            out["wallet"] = c.wallet_balance()
        except Exception as e:
            out["wallet_error"] = type(e).__name__
    return out
