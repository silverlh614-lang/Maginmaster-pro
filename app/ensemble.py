"""@responsibility Part 1 앙상블 — 6개 렌즈 삼각분포 mixture-of-experts 몬테카를로 바닥 가격 분포

Part 1 — Monte Carlo ensemble over 6 valuation/technical lenses.

Each lens = a triangular distribution (min, mode, max) over the bottom price it
implies, plus a reliability weight. Drawn as a mixture-of-experts: pick a lens
proportional to its weight, then sample its triangle.

Ported from btc_bottom_model.py; lens values are [SNAPSHOT 2026-07] and encode
the knowledge base §6.1 triangulation. Changing them changes the conclusion —
the model is a structured opinion, not a prediction (§1-5).
"""
from __future__ import annotations
import numpy as np

# name : (min, mode, max, weight)
DEFAULT_LENSES: dict[str, tuple[float, float, float, float]] = {
    "realized_price":     (44_000, 51_000, 54_000, 0.22),  # network cost-basis support
    "ma_200w":            (55_000, 59_000, 62_000, 0.20),  # historical bottom rail
    "drawdown_decay":     (50_000, 53_500, 57_000, 0.15),  # -55~60% (decaying each cycle)
    "mvrv_capitulation":  (40_000, 48_000, 53_000, 0.18),  # green-zone flush below cost basis
    "death_cross_leg":    (29_000, 31_000, 33_000, 0.15),  # 3D 50/200 cross -> -46~52%
    "historical_repeat":  (25_000, 28_000, 31_000, 0.10),  # naive -77~85% repeat (weakest)
}

KEY_LEVELS = [53_600, 50_000, 45_000, 40_000, 35_000]


def simulate_bottom(n: int = 300_000, seed: int = 42,
                    lenses: dict | None = None) -> np.ndarray:
    lenses = lenses or DEFAULT_LENSES
    rng = np.random.default_rng(seed)
    names = list(lenses)
    w = np.array([lenses[k][3] for k in names], dtype=float)
    w /= w.sum()
    idx = rng.choice(len(names), size=n, p=w)
    out = np.empty(n)
    for i, k in enumerate(names):
        lo, mode, hi, _ = lenses[k]
        m = idx == i
        out[m] = rng.triangular(lo, mode, hi, m.sum())
    return out


def summarize(draws: np.ndarray, key_levels: list[float] | None = None) -> dict:
    key_levels = key_levels or KEY_LEVELS
    pct = [5, 10, 25, 50, 75, 90, 95]
    q = np.percentile(draws, pct)
    hist, edges = np.histogram(draws, bins=90)
    peak = int(hist.argmax())
    mode = 0.5 * (edges[peak] + edges[peak + 1])
    return {
        "n": int(len(draws)),
        "mean": float(draws.mean()),
        "median": float(np.median(draws)),
        "mode": float(mode),
        "percentiles": {str(p): float(v) for p, v in zip(pct, q)},
        "prob_below": {str(int(L)): float((draws < L).mean()) for L in key_levels},
        "note": "mode is an artifact of the narrow deep-cluster lenses; "
                "robust central values are median/EV (knowledge base §7.1)",
    }


def histogram(draws: np.ndarray, bins: int = 90) -> dict:
    hist, edges = np.histogram(draws, bins=bins)
    return {
        "counts": hist.tolist(),
        "edges": [float(e) for e in edges],
    }


def plot_distribution(draws: np.ndarray, s: dict, path: str,
                      realized_price: float, spot: float,
                      snapshot_date: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(11, 6))
    ax.hist(draws, bins=90, color="#F7931A", alpha=0.85, edgecolor="none")

    def vline(x, color, label, ls="--"):
        ax.axvline(x, color=color, ls=ls, lw=1.8, label=label)

    vline(realized_price, "#E23", f"Realized price ${realized_price/1000:.1f}k (decisive line)")
    vline(spot, "#555", f"Spot ~${spot/1000:.1f}k ({snapshot_date})", ls=":")
    vline(s["median"], "#08C", f"Median ${s['median']:,.0f}")
    vline(s["mean"], "#0A0", f"EV ${s['mean']:,.0f}", ls="-.")

    ax.set_title(f"BTC cycle-bottom distribution — 6-lens weighted ensemble ({s['n']:,} sims)",
                 fontsize=13, weight="bold")
    ax.set_xlabel("Bottom price (USD)")
    ax.set_ylabel("Frequency")
    ax.legend(fontsize=9, framealpha=0.9)
    ax.spines[["top", "right"]].set_visible(False)
    ax.xaxis.set_major_formatter(lambda x, _: f"${x/1000:.0f}k")
    fig.tight_layout()
    fig.savefig(path, dpi=140, bbox_inches="tight")
    plt.close(fig)
