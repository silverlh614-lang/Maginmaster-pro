"""@responsibility 온체인 스냅샷 앵커 SSOT — 시점 고정 값, env 오버라이드로만 갱신 (코드 수정 금지)

On-chain snapshot anchors.

[SNAPSHOT 2026-07] values from the knowledge base (knowledge/btc_analysis_knowledge.md §2).
These are time-sensitive and MUST be re-verified periodically — override them on
Railway via environment variables instead of editing code:

    REALIZED_PRICE, MA_200W, SPOT, ATH, SNAPSHOT_DATE
"""
from __future__ import annotations
import os


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw.replace(",", "").replace("$", ""))
    except ValueError:
        return default


REALIZED_PRICE = _env_float("REALIZED_PRICE", 53_600.0)  # decisive line (network cost basis)
MA_200W = _env_float("MA_200W", 61_800.0)                # historical bottom rail
SPOT = _env_float("SPOT", 61_500.0)
ATH = _env_float("ATH", 126_198.0)                       # 2025-10-06 cycle top
SNAPSHOT_DATE = os.getenv("SNAPSHOT_DATE", "2026-07-03")


def as_dict() -> dict:
    return {
        "realized_price": REALIZED_PRICE,
        "ma_200w": MA_200W,
        "spot": SPOT,
        "ath": ATH,
        "snapshot_date": SNAPSHOT_DATE,
        "drawdown_from_ath": round(SPOT / ATH - 1.0, 4),
    }
