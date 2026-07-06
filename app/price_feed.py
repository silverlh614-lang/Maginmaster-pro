"""@responsibility 현물가 단일 통로 — CoinGecko→Coinbase→Binance 폴백 + TTL 캐시, 전부 실패 시 스냅샷 앵커

Live BTC spot price feed with TTL cache and multi-source fallback.

Tries public price APIs in order (CoinGecko -> Coinbase -> Binance); on total
failure falls back to the [SNAPSHOT] SPOT anchor so the app keeps working
offline. Only the spot price is auto-refreshed — realized price / 200W MA are
on-chain aggregates without a free public endpoint, so they stay env-driven
(see app/snapshot.py) and should be re-verified via the snapshot-analyst agent.

Env:
    PRICE_TTL_SECONDS   cache lifetime (default 60)
    PRICE_FEED_DISABLED set to "1" to always serve the snapshot value
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from datetime import datetime, timezone

from . import snapshot

TTL_SECONDS = float(os.getenv("PRICE_TTL_SECONDS", "60"))
DISABLED = os.getenv("PRICE_FEED_DISABLED", "") == "1"
_TIMEOUT = 6.0

_lock = threading.Lock()
_cache: dict = {"price": None, "source": None, "fetched_at": 0.0}


def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "coinmaster-pro/1.0"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        return json.load(r)


def _from_coingecko() -> float:
    d = _get_json("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd")
    return float(d["bitcoin"]["usd"])


def _from_coinbase() -> float:
    d = _get_json("https://api.coinbase.com/v2/prices/BTC-USD/spot")
    return float(d["data"]["amount"])


def _from_binance() -> float:
    d = _get_json("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
    return float(d["price"])


_SOURCES = [
    ("coingecko", _from_coingecko),
    ("coinbase", _from_coinbase),
    ("binance", _from_binance),
]


def get_spot() -> dict:
    """Return the freshest available spot price.

    {"price", "source", "fetched_at", "live", "ttl_seconds"} — `live` is False
    when serving the snapshot fallback (feed disabled or all sources failed).
    """
    now = time.time()
    with _lock:
        if _cache["price"] is not None and now - _cache["fetched_at"] < TTL_SECONDS:
            return _result(_cache["price"], _cache["source"], _cache["fetched_at"], live=True)

        errors = []
        if not DISABLED:
            for name, fn in _SOURCES:
                try:
                    price = fn()
                    if price > 0:
                        _cache.update(price=price, source=name, fetched_at=now)
                        return _result(price, name, now, live=True)
                except Exception as e:  # network/parse/proxy — try next source
                    errors.append(f"{name}: {type(e).__name__}")

        # stale cache beats static snapshot
        if _cache["price"] is not None:
            return _result(_cache["price"], _cache["source"] + " (stale)",
                           _cache["fetched_at"], live=True)
        return {
            **_result(snapshot.SPOT, "snapshot-fallback", now, live=False),
            "errors": errors or ["feed disabled"],
        }


def _result(price: float, source: str, fetched_at: float, live: bool) -> dict:
    return {
        "price": float(price),
        "source": source,
        "fetched_at": datetime.fromtimestamp(fetched_at, tz=timezone.utc).isoformat(),
        "live": live,
        "ttl_seconds": TTL_SECONDS,
    }
