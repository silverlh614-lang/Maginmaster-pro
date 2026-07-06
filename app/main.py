"""@responsibility FastAPI 엔트리포인트 — 대시보드·시뮬레이션·FSM·트레이딩 API 라우팅과 앱 조립

Coinmaster Pro — BTC cycle-bottom analysis service.

Part 1 (ensemble) runs as an offline/weekly batch: recompute the distribution
whenever the snapshot anchors change, then read percentiles to set DCA ladder
rungs. Part 2 (FSM) runs online/daily: feed on-chain inputs, consume the
emitted deploy fraction (knowledge base §7.3).
"""
from __future__ import annotations

import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from . import chain, ensemble, fsm, price_feed, snapshot
from .trading_bybit.api import router as bybit_router
from .trading_bybit.bot import MANAGER as BYBIT_MANAGER

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
FSM_STATE_PATH = str(DATA_DIR / "fsm_state.json")
CHART_PATH = str(DATA_DIR / "btc_bottom_dist.png")
CHAIN_CHART_PATH = str(DATA_DIR / "btc_cycle_chain.png")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Warm the kline feed on boot so the live chart shows candles immediately,
    # even before an operator starts the bot.
    BYBIT_MANAGER.start_feeds()
    # Auto-resume: if the Bybit bot was running before a restart/redeploy,
    # start it again with the same mode/strategy (state persists in the
    # DATA_DIR volume).
    bst = BYBIT_MANAGER.state_store.load()
    if bst.get("running"):
        await BYBIT_MANAGER.start(mode=bst.get("mode", "paper"),
                                  strategy=bst.get("strategy", "trend_breakout"))
    yield
    # Graceful exit WITHOUT persisting running=False, so auto-resume fires
    # on the next boot. An operator pressing "stop" is the only thing that
    # persists an intentional off state.
    await BYBIT_MANAGER.shutdown()
    await BYBIT_MANAGER.stop_feeds()


app = FastAPI(
    title="Coinmaster Pro — BTC Cycle Bottom Model",
    description="Monte Carlo ensemble bottom distribution + bottom-detection FSM. "
                "Structured opinion, not investment advice.",
    version="1.0.0",
    lifespan=lifespan,
)
app.include_router(bybit_router)

_lock = threading.Lock()
_cache: dict = {}


def _default_run() -> dict:
    """Compute (once) the default 300k-sim run and cache summary/histogram/chart."""
    with _lock:
        if "summary" not in _cache:
            draws = ensemble.simulate_bottom()
            s = ensemble.summarize(draws)
            _cache["summary"] = s
            _cache["histogram"] = ensemble.histogram(draws)
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            ensemble.plot_distribution(
                draws, s, CHART_PATH,
                realized_price=snapshot.REALIZED_PRICE,
                spot=snapshot.SPOT,
                snapshot_date=snapshot.SNAPSHOT_DATE,
            )
        return _cache


# ---------------------------------------------------------------- meta

@app.get("/healthz")
def healthz():
    return {"ok": True, "app_mode": "bybit"}


@app.get("/api/snapshot")
def get_snapshot():
    return {
        **snapshot.as_dict(),
        "warning": "[SNAPSHOT] values are time-sensitive; re-verify and override "
                   "via env vars (REALIZED_PRICE, MA_200W, SPOT, ATH, SNAPSHOT_DATE).",
    }


@app.get("/api/price")
def get_price():
    """Live BTC spot (TTL-cached, multi-source fallback). live=false means the
    static [SNAPSHOT] anchor is being served — treat as possibly outdated."""
    spot = price_feed.get_spot()
    return {
        **spot,
        "ath": snapshot.ATH,
        "drawdown_from_ath": round(spot["price"] / snapshot.ATH - 1.0, 4),
        "realized_price": snapshot.REALIZED_PRICE,
        "above_realized": spot["price"] > snapshot.REALIZED_PRICE,
    }


@app.get("/api/knowledge", response_class=PlainTextResponse)
def get_knowledge():
    path = ROOT / "knowledge" / "btc_analysis_knowledge.md"
    if not path.exists():
        raise HTTPException(404, "knowledge base not found")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------- Part 1

class LensSpec(BaseModel):
    min: float
    mode: float
    max: float
    weight: float = Field(gt=0)


class SimulateRequest(BaseModel):
    n: int = Field(default=300_000, ge=1_000, le=1_000_000)
    seed: int = 42
    lenses: dict[str, LensSpec] | None = None  # omit -> default 6 lenses


@app.get("/api/simulate")
def simulate_default():
    c = _default_run()
    return {"snapshot": snapshot.as_dict(), "summary": c["summary"],
            "histogram": c["histogram"], "lenses": ensemble.DEFAULT_LENSES}


@app.post("/api/simulate")
def simulate_custom(req: SimulateRequest):
    lenses = None
    if req.lenses:
        for name, L in req.lenses.items():
            if not (L.min <= L.mode <= L.max):
                raise HTTPException(422, f"lens '{name}': require min <= mode <= max")
        lenses = {k: (v.min, v.mode, v.max, v.weight) for k, v in req.lenses.items()}
    draws = ensemble.simulate_bottom(n=req.n, seed=req.seed, lenses=lenses)
    return {"summary": ensemble.summarize(draws),
            "histogram": ensemble.histogram(draws)}


@app.get("/api/distribution.png")
def distribution_png():
    _default_run()
    return FileResponse(CHART_PATH, media_type="image/png")


# ---------------------------------------------------------------- Part 3

def _default_chain_run() -> dict:
    """Compute (once) the default 300k full-cycle chain and cache the results."""
    with _lock:
        if "chain_summary" not in _cache:
            draws = chain.simulate_chain()
            _cache["chain_summary"] = chain.summarize_chain(draws, ath=snapshot.ATH)
            _cache["chain_histograms"] = chain.chain_histograms(draws)
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            chain.plot_chain(draws, ath=snapshot.ATH, path=CHAIN_CHART_PATH)
        return _cache


class Triangle(BaseModel):
    min: float
    mode: float
    max: float


class ChainRequest(BaseModel):
    n: int = Field(default=300_000, ge=1_000, le=1_000_000)
    seed: int = 7
    recovery: Triangle | None = None      # bottom -> halving-day multiplier
    p_extinct: float = Field(default=0.30, ge=0.0, le=1.0)
    roi_extinct: Triangle | None = None
    roi_normal: Triangle | None = None
    lenses: dict[str, LensSpec] | None = None  # omit -> default 6 lenses


def _tri(t: Triangle | None, default: tuple, name: str) -> tuple:
    if t is None:
        return default
    if not (t.min <= t.mode <= t.max):
        raise HTTPException(422, f"{name}: require min <= mode <= max")
    return (t.min, t.mode, t.max)


@app.get("/api/chain")
def chain_default():
    c = _default_chain_run()
    return {"snapshot": snapshot.as_dict(), "summary": c["chain_summary"],
            "histograms": c["chain_histograms"]}


@app.post("/api/chain")
def chain_custom(req: ChainRequest):
    lenses = ensemble.DEFAULT_LENSES
    if req.lenses:
        for name, L in req.lenses.items():
            if not (L.min <= L.mode <= L.max):
                raise HTTPException(422, f"lens '{name}': require min <= mode <= max")
        lenses = {k: (v.min, v.mode, v.max, v.weight) for k, v in req.lenses.items()}
    p = chain.ChainParams(
        n=req.n, seed=req.seed,
        recovery=_tri(req.recovery, chain.ChainParams.recovery, "recovery"),
        p_extinct=req.p_extinct,
        roi_extinct=_tri(req.roi_extinct, chain.ChainParams.roi_extinct, "roi_extinct"),
        roi_normal=_tri(req.roi_normal, chain.ChainParams.roi_normal, "roi_normal"),
        lenses=lenses,
    )
    draws = chain.simulate_chain(p)
    return {"summary": chain.summarize_chain(draws, ath=snapshot.ATH),
            "histograms": chain.chain_histograms(draws)}


@app.get("/api/chain.png")
def chain_png():
    _default_chain_run()
    return FileResponse(CHAIN_CHART_PATH, media_type="image/png")


# ---------------------------------------------------------------- Part 2

class DailyInput(BaseModel):
    price: float
    realized_price: float | None = None  # default: snapshot anchor
    mvrv_z: float
    ma_200w: float | None = None
    lth_mvrv: float
    sth_mvrv: float
    made_new_low: bool = False


@app.get("/api/fsm/state")
def fsm_state():
    det = fsm.load_detector(FSM_STATE_PATH)
    return det.to_state()


@app.post("/api/fsm/update")
def fsm_update(d: DailyInput):
    det = fsm.load_detector(FSM_STATE_PATH)
    daily = fsm.Daily(
        price=d.price,
        realized_price=d.realized_price if d.realized_price is not None else snapshot.REALIZED_PRICE,
        mvrv_z=d.mvrv_z,
        ma_200w=d.ma_200w if d.ma_200w is not None else snapshot.MA_200W,
        lth_mvrv=d.lth_mvrv,
        sth_mvrv=d.sth_mvrv,
        made_new_low=d.made_new_low,
    )
    result = det.update(daily)
    fsm.save_detector(det, FSM_STATE_PATH)
    return {**result, "state": det.to_state()}


@app.post("/api/fsm/reset")
def fsm_reset(ladder_tranches: int = 5):
    det = fsm.BottomDetector(ladder_tranches=ladder_tranches)
    fsm.save_detector(det, FSM_STATE_PATH)
    return det.to_state()


@app.get("/api/fsm/demo")
def fsm_demo():
    return {"rows": fsm.demo_path()}


# ---------------------------------------------------------------- UI

@app.get("/", response_class=HTMLResponse)
def index():
    """Landing page = Bybit leverage-margin control tower."""
    return (ROOT / "static" / "bybit.html").read_text(encoding="utf-8")


@app.get("/model", response_class=HTMLResponse)
def model_page():
    """Cycle-bottom model dashboard (Parts 1-3)."""
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/bybit", response_class=HTMLResponse)
def bybit_page():
    """Part 5 — Bybit leverage-margin trend-breakout control tower (paper)."""
    return (ROOT / "static" / "bybit.html").read_text(encoding="utf-8")


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(ROOT / "static" / "manifest.webmanifest",
                        media_type="application/manifest+json")


@app.get("/icon.svg")
def icon():
    return FileResponse(ROOT / "static" / "icon.svg", media_type="image/svg+xml")


# PWA / home-screen icons (Android manifest icons + iOS apple-touch-icon).
_PWA_ICONS = ("icon-192.png", "icon-512.png", "icon-maskable-512.png",
              "apple-touch-icon.png")


@app.get("/{name}.png")
def png_icon(name: str):
    """Serve the home-screen icon PNGs from static/ (allow-listed only)."""
    fname = f"{name}.png"
    if fname not in _PWA_ICONS:
        raise HTTPException(404, "not found")
    return FileResponse(ROOT / "static" / fname, media_type="image/png")
