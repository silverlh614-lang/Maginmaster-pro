"""@responsibility Part 3 풀사이클 체인 — 바닥 앙상블→회복 배수→반감기 가격→ROI 혼합→2029 고점 시뮬레이션

Part 3 — Full-cycle chain simulation.

Chains: bottom (6-lens ensemble, same lenses as Part 1)
        -> recovery multiplier (bottom -> halving day, hist 2.7x-4.1x over 17-18mo)
        -> 2028-04 halving price
        -> top ROI regime mixture (normal decay vs rally-extinction)
        -> 2029 cycle-top price + timing.

Ported from btc_cycle_chain.py. All parameters are explicit judgment calls;
change them and the answer changes (knowledge base §1-5). The recovery mode is
shifted BELOW the historical mean (2.8 vs 3.5) to respect maturity/decay, and
ROI decay 96->30->7.9->2 (/3.7 per cycle) extrapolates to 0.54, which is why a
rally-extinction regime is mixed in at all.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import numpy as np

from . import ensemble

HALVING_DATE = date(2028, 4, 20)  # ~block 950,000


@dataclass
class ChainParams:
    n: int = 300_000
    seed: int = 7
    # bottom -> halving-day multiplier: hist 3.8x / 2.7x / 4.1x, all ~17-18mo
    recovery: tuple[float, float, float] = (1.8, 2.8, 4.2)
    # regime mixture for halving -> top ROI
    p_extinct: float = 0.30
    roi_extinct: tuple[float, float, float] = (0.90, 1.05, 1.30)
    roi_normal: tuple[float, float, float] = (1.30, 1.80, 3.20)
    # top timing: halving + N(534, 45) days clipped, hist 525/549/534
    top_days_mean: float = 534.0
    top_days_sd: float = 45.0
    top_days_clip: tuple[float, float] = (380.0, 700.0)
    lenses: dict = field(default_factory=lambda: ensemble.DEFAULT_LENSES)


@dataclass
class ChainDraws:
    bottom: np.ndarray
    halving_price: np.ndarray
    top: np.ndarray
    roi: np.ndarray
    days_to_top: np.ndarray


def simulate_chain(p: ChainParams | None = None) -> ChainDraws:
    p = p or ChainParams()
    rng = np.random.default_rng(p.seed)

    # Link 0: bottom ensemble (mixture-of-experts over the 6 lenses)
    names = list(p.lenses)
    w = np.array([p.lenses[k][3] for k in names], dtype=float)
    w /= w.sum()
    idx = rng.choice(len(names), p.n, p=w)
    bottom = np.empty(p.n)
    for i, k in enumerate(names):
        lo, mo, hi, _ = p.lenses[k]
        m = idx == i
        bottom[m] = rng.triangular(lo, mo, hi, m.sum())

    # Link 1: bottom -> halving-day price
    rec_mult = rng.triangular(*p.recovery, p.n)
    halving_price = bottom * rec_mult

    # Link 2: halving -> top ROI (regime mixture)
    extinct = rng.random(p.n) < p.p_extinct
    roi = np.where(extinct,
                   rng.triangular(*p.roi_extinct, p.n),
                   rng.triangular(*p.roi_normal, p.n))
    top = halving_price * roi

    # Link 3: top timing
    days = np.clip(rng.normal(p.top_days_mean, p.top_days_sd, p.n),
                   *p.top_days_clip)

    return ChainDraws(bottom, halving_price, top, roi, days)


def _q(a: np.ndarray, ps=(5, 25, 50, 75, 95)) -> dict:
    return {str(p): float(np.percentile(a, p)) for p in ps}


def summarize_chain(d: ChainDraws, ath: float) -> dict:
    date_ps = (25, 50, 75)
    dq = np.percentile(d.days_to_top, date_ps)
    return {
        "n": int(len(d.top)),
        "halving_date": HALVING_DATE.isoformat(),
        "bottom": _q(d.bottom),
        "halving_price": _q(d.halving_price),
        "top": _q(d.top),
        "prob": {
            "halving_above_old_ath": float((d.halving_price > ath).mean()),
            "top_above_old_ath": float((d.top > ath).mean()),
            "top_above_200k": float((d.top > 200_000).mean()),
            "top_above_300k": float((d.top > 300_000).mean()),
            "top_above_500k": float((d.top > 500_000).mean()),
            "rally_fails": float((d.roi < 1).mean()),
        },
        "top_date": {
            str(p): (HALVING_DATE + timedelta(days=int(v))).isoformat()
            for p, v in zip(date_ps, dq)
        },
        "note": "chained judgment calls, not a forecast — recovery mode 2.8x is "
                "below the 3.5x historical mean and 30% rally-extinction is mixed "
                "in because ROI decay (/3.7 per cycle) extrapolates below 1x",
    }


def chain_histograms(d: ChainDraws, bins: int = 100) -> dict:
    out = {}
    for key, arr in (("halving_price", d.halving_price), ("top", d.top)):
        hist, edges = np.histogram(arr, bins=bins)
        out[key] = {"counts": hist.tolist(), "edges": [float(e) for e in edges]}
    return out


def plot_chain(d: ChainDraws, ath: float, path: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(13, 5))
    for ax, data, title, color in [
        (axes[0], d.halving_price, "2028-04 halving-day price", "#F7931A"),
        (axes[1], d.top, "2029 cycle-top price", "#0A84FF"),
    ]:
        ax.hist(data, bins=100, color=color, alpha=0.85)
        med = float(np.median(data))
        ax.axvline(med, color="k", ls="--", lw=1.6, label=f"median ${med:,.0f}")
        ax.axvline(ath, color="#E23", ls=":", lw=1.6,
                   label=f"old ATH ${ath/1000:.0f}k")
        ax.set_title(title, weight="bold")
        ax.legend(fontsize=9)
        ax.spines[["top", "right"]].set_visible(False)
        ax.xaxis.set_major_formatter(lambda x, _: f"${x/1000:.0f}k")
    fig.suptitle("BTC full-cycle chain — bottom → halving → top "
                 f"({len(d.top):,} Monte Carlo)", fontsize=13, weight="bold")
    fig.tight_layout()
    fig.savefig(path, dpi=140, bbox_inches="tight")
    plt.close(fig)
