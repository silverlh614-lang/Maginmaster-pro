"""@responsibility 200주선 자동 산출 — 주봉 종가 200개의 SMA를 공개 kline으로 계산, TTL 캐시·실패 시 스냅샷 앵커 폴백

The 200-week moving average is NOT an on-chain aggregate — it is just the SMA
of the last 200 weekly closes, so it CAN be derived from public price data
(unlike the realized price, which stays env-driven in app/snapshot.py). This
module computes it live with a long TTL cache and, on any failure, falls back
to the frozen [SNAPSHOT] MA_200W anchor so the cycle panel never breaks.

Spot price keeps its own single channel (app/price_feed.py); this is a
separate weekly rail and does not touch that path.

Env:
    MA200W_TTL_SECONDS   cache lifetime (default 21600 = 6h; weekly metric)
    MA200W_DISABLED      set to "1" to always serve the snapshot anchor
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone

from . import snapshot

TTL_SECONDS = float(os.getenv("MA200W_TTL_SECONDS", str(6 * 3600)))
DISABLED = os.getenv("MA200W_DISABLED", "") == "1"
_TIMEOUT = 8.0

_lock = threading.Lock()
_cache: dict = {"value": None, "as_of": 0.0, "source": None}


def _weekly_closes() -> tuple[list[float], str]:
    """Last ~200 weekly BTC closes (oldest→newest) from a public venue.
    Binance klines: [openTime, o, h, l, c, v, closeTime, ...]."""
    url = ("https://api.binance.com/api/v3/klines"
           "?symbol=BTCUSDT&interval=1w&limit=200")
    req = urllib.request.Request(url, headers={"User-Agent": "coinmaster-pro/1.0"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        rows = json.load(r)
    closes = [float(x[4]) for x in rows]
    return closes, "binance"


def get_ma_200w() -> dict:
    """{"value", "source", "as_of", "live"}. live=False → frozen anchor served
    (disabled, fetch failed, or too few weekly bars)."""
    now = time.time()
    with _lock:
        if _cache["value"] is not None and now - _cache["as_of"] < TTL_SECONDS:
            return _out(_cache["value"], _cache["source"], _cache["as_of"], True)

        if not DISABLED:
            try:
                closes, src = _weekly_closes()
                if len(closes) >= 200:
                    val = sum(closes[-200:]) / 200.0
                    _cache.update(value=val, as_of=now, source=src)
                    return _out(val, src, now, True)
            except Exception:
                pass  # fall through to anchor / stale cache

        if _cache["value"] is not None:      # stale live beats static anchor
            return _out(_cache["value"], (_cache["source"] or "") + " (stale)",
                        _cache["as_of"], True)
        return _out(snapshot.MA_200W, "snapshot-anchor", now, False)


def _out(value: float, source: str, as_of: float, live: bool) -> dict:
    return {
        "value": round(float(value), 2),
        "source": source,
        "as_of": datetime.fromtimestamp(as_of, tz=timezone.utc).date().isoformat(),
        "live": live,
    }
