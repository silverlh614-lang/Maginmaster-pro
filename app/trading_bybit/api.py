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
    try:
        r = replay(req.symbol, req.strategy, cfg)
    except Exception as e:
        raise HTTPException(502, f"backtest fetch/replay failed: {e}")
    return {"symbol": req.symbol.upper(), "strategy": req.strategy,
            "metrics": compute(r["closes"], cfg.equity_usd, r.get("final_equity", cfg.equity_usd)),
            "final_equity": r.get("final_equity"),
            "snapshots": r["snapshots"], "trades": len(r["closes"]),
            "equity_curve": r["equity_curve"][-500:]}


@router.get("/config")
def config():
    return {"config": CONFIG.as_dict(),
            "symbols": list(MANAGER.bots),
            "strategies": list(STRATEGIES),
            "phase": "1 (paper only)"}
