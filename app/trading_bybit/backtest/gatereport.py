"""@responsibility 게이트 판정 러너 — H7(HTF 스윕)·H8(숏 거래량 면제) IS/OOS 매트릭스 실행과 판정 초안 생성

Gate-verdict runner. Executes the backtest matrix that hypothesis_registry
H7 / H8 call for — H7: htf_interval 60/240/D, H8: short_vol_exempt off/on —
each split into an in-sample (IS) and out-of-sample (OOS) window, and lines
the results up against the registry's verdict criteria. Output is a DRAFT:
the human records CONFIRMED/REJECTED in the registry (교차 심볼 일관성은
심볼별로 재실행해 비교). Pure logic — candles are injected by the caller,
so the whole matrix is offline-testable.
"""
from __future__ import annotations

import copy

from ..config import BybitConfig
from ..models import Candle
from .engine import replay
from .metrics import compute

H7_IVS = ["60", "240", "D"]     # H7 스윕 대상 (60 = 현행 기준선)
MIN_TRADES = 8                  # 셀당 최소 표본 — 미만이면 '표본 부족'


def _run(symbol: str, strategy: str, cfg: BybitConfig,
         entry: list[Candle], htf: list[Candle],
         frac: tuple[float, float]) -> dict:
    r = replay(symbol, strategy, cfg, entry_candles=entry, htf_candles=htf,
               frac=frac)
    m = compute(r["closes"], cfg.equity_usd,
                r.get("final_equity", cfg.equity_usd))
    shorts = [c for c in r["closes"] if c.get("side") == "SHORT"]
    return {
        "trades": len(r["closes"]),
        "net_usd": round(sum(float(c["pnl_usd"] or 0) for c in r["closes"]), 2),
        "expectancy_r": m.get("expectancy_r"),
        "pf": m.get("profit_factor"),
        "mdd_usd": m.get("max_drawdown_usd"),
        "short_trades": len(shorts),
        "short_net_usd": round(sum(float(c["pnl_usd"] or 0) for c in shorts), 2),
    }


def _get(cells: list[dict], **kv) -> dict | None:
    for c in cells:
        if all(c.get(k) == v for k, v in kv.items()):
            return c
    return None


def _better(c: dict, base: dict) -> bool:
    """후보가 기준선보다 낫다: E[R]↑ AND PF 유지 이상 AND MDD 악화 없음."""
    er = lambda x: x["expectancy_r"] if x["expectancy_r"] is not None else -9.0
    pf = lambda x: x["pf"] if x["pf"] is not None else 0.0
    dd = lambda x: abs(x["mdd_usd"] or 0.0)
    return er(c) > er(base) and pf(c) >= pf(base) and dd(c) <= dd(base) + 1e-9


def _judge_h7(cells: list[dict]) -> dict:
    checks, winners = [], []
    low_sample = any(c["trades"] < MIN_TRADES for c in cells)
    for iv in ("240", "D"):
        seg_ok = []
        for seg in ("IS", "OOS"):
            b, c = _get(cells, htf="60", seg=seg), _get(cells, htf=iv, seg=seg)
            seg_ok.append(bool(b and c and _better(c, b)))
        good = all(seg_ok)
        checks.append({"cand": iv, "is_better": seg_ok[0], "oos_better": seg_ok[1],
                       "both": good})
        if good:
            winners.append(iv)
    if low_sample:
        verdict = "표본 부족 — 기간을 늘려 재실행 (셀당 최소 %d건)" % MIN_TRADES
    elif winners:
        verdict = "CONFIRM 후보: HTF " + ", ".join(winners) + " (IS·OOS 모두 60분 대비 우위)"
    else:
        verdict = "REJECT 후보 — 60분봉(현행) 유지"
    return {"checks": checks, "low_sample": low_sample, "verdict": verdict}


def _judge_h8(cells: list[dict]) -> dict:
    seg_ok, details = [], []
    low_sample = any(c["short_trades"] < MIN_TRADES
                     for c in cells if c["exempt"])
    for seg in ("IS", "OOS"):
        off, on = _get(cells, exempt=False, seg=seg), _get(cells, exempt=True, seg=seg)
        ok = bool(off and on
                  and on["short_net_usd"] > off["short_net_usd"]
                  and abs(on["mdd_usd"] or 0) <= abs(off["mdd_usd"] or 0) + 1e-9)
        seg_ok.append(ok)
        details.append({"seg": seg, "short_net_improved": ok})
    if low_sample:
        verdict = "표본 부족 — SHORT 표본이 셀당 %d건 미만" % MIN_TRADES
    elif all(seg_ok):
        verdict = "CONFIRM 후보: 면제 ON (IS·OOS 모두 SHORT 순손익 개선 + MDD 무악화)"
    else:
        verdict = "REJECT 후보 — 거래량 게이트 유지 (게이트 완화는 미달 시 즉시 기각)"
    return {"checks": details, "low_sample": low_sample, "verdict": verdict}


def build_report(symbol: str, cfg_base: BybitConfig,
                 entry: list[Candle], htf_by_iv: dict[str, list[Candle]],
                 split: float = 0.7) -> dict:
    """H7·H8 매트릭스 실행 → 셀 지표 + 레지스트리 기준 대조 + 판정 초안."""
    segs = [("IS", (0.0, split)), ("OOS", (split, 1.0))]

    h7_cells = []
    for iv in H7_IVS:
        htf = htf_by_iv.get(iv)
        if not htf:
            continue
        cfg = copy.copy(cfg_base)
        cfg.htf_interval = iv
        for seg, fr in segs:
            h7_cells.append({"htf": iv, "seg": seg,
                             **_run(symbol, "trend_breakout", cfg, entry, htf, fr)})

    base_iv = cfg_base.htf_interval
    h8_cells = []
    for flag in (False, True):
        cfg = copy.copy(cfg_base)
        cfg.short_vol_exempt = flag
        for seg, fr in segs:
            h8_cells.append({"exempt": flag, "seg": seg,
                             **_run(symbol, "trend_breakout", cfg, entry,
                                    htf_by_iv[base_iv], fr)})

    return {
        "symbol": symbol, "split": split, "entry_bars": len(entry),
        "h7": {"cells": h7_cells, **_judge_h7(h7_cells)},
        "h8": {"cells": h8_cells, **_judge_h8(h8_cells)},
        "note": ("판정 초안입니다 — CONFIRMED/REJECTED 확정은 hypothesis_registry에 "
                 "기록하세요. 교차 심볼 일관성(H7 기준)은 다른 심볼 탭에서도 실행해 "
                 "비교해야 합니다."),
    }
