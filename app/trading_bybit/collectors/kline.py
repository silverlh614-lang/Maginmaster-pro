"""@responsibility 캔들 수집기 — Bybit→Binance→OKX 공개 kline REST 폴백으로 진입·상위 시간봉 OHLCV 유지 (인증 불필요)

Candle collector. Polls a PUBLIC v5/kline-style REST endpoint (no API key) for
both the entry and higher timeframes and keeps a deduped, time-sorted buffer of
CLOSED candles per interval. Bybit is the primary source; if it is unreachable
(some regions geo-block api.bybit.com), it fails over to Binance USDⓈ-M futures
and then OKX swaps so the live chart and strategy keep receiving identical
Candle objects. The newest returned bar is still forming, so it is exposed as
last_price but excluded from the closed-candle list the strategy consumes.
"""
from __future__ import annotations

import asyncio

import httpx

from ..models import Candle

BYBIT_REST = "https://api.bybit.com/v5/market/kline"
BYBIT_TESTNET_REST = "https://api-testnet.bybit.com/v5/market/kline"
BINANCE_REST = "https://fapi.binance.com/fapi/v1/klines"
OKX_REST = "https://www.okx.com/api/v5/market/candles"
MAX_BARS = 400          # rolling history kept per interval

# Cross-source price validation: every N polls, compare the live source's
# latest price against a second venue; a gap beyond the threshold flags the
# feed as untrustworthy (bad tick / stuck source) so the operator can react.
CROSS_CHECK_EVERY = 15          # polls (~30s at a 2s poll)
MAX_SOURCE_DEV_PCT = 0.8        # % gap between venues that raises a warning

# Bybit interval code (minutes as string / D,W,M) -> other venues' bar strings.
_BINANCE_IV = {"1": "1m", "3": "3m", "5": "5m", "15": "15m", "30": "30m",
               "60": "1h", "120": "2h", "240": "4h", "360": "6h",
               "720": "12h", "D": "1d", "W": "1w"}
_OKX_IV = {"1": "1m", "3": "3m", "5": "5m", "15": "15m", "30": "30m",
           "60": "1H", "120": "2H", "240": "4H", "360": "6H",
           "720": "12H", "D": "1D", "W": "1W"}


def _base_of(symbol: str) -> str:
    """'BTCUSDT' -> 'BTC' (for venues that want a dashed instrument id)."""
    return symbol[:-4] if symbol.endswith("USDT") else symbol


class KlineCollector:
    def __init__(self, symbol: str, entry_interval: str, htf_interval: str,
                 limit: int = 200, testnet: bool = False):
        self.symbol = symbol
        self.entry_interval = entry_interval
        self.htf_interval = htf_interval
        self.limit = limit
        # ts_ms -> Candle, per interval (dedupe on bar open time)
        self._bars: dict[str, dict[int, Candle]] = {entry_interval: {},
                                                     htf_interval: {}}
        self._forming: dict[str, Candle | None] = {entry_interval: None,
                                                    htf_interval: None}
        self.source = "none"
        self.last_error = ""
        self._polls = 0
        # cross-source validation state (advisory — never blocks the feed)
        self.cross_source = ""
        self.cross_dev_pct: float | None = None
        self.divergence = False
        # Source failover order. Testnet only exists on Bybit, so pin to it.
        if testnet:
            self._bybit_url = BYBIT_TESTNET_REST
            self._sources: list[tuple[str, object]] = [
                ("bybit_testnet", self._src_bybit)]
        else:
            self._bybit_url = BYBIT_REST
            self._sources = [("bybit", self._src_bybit),
                             ("binance", self._src_binance),
                             ("okx", self._src_okx)]

    # ------------------------------------------------------------- buffer

    def closed_candles(self, interval: str) -> list[Candle]:
        bars = self._bars.get(interval, {})
        return [bars[k] for k in sorted(bars)]

    def entry_closed(self) -> list[Candle]:
        return self.closed_candles(self.entry_interval)

    def htf_closed(self) -> list[Candle]:
        return self.closed_candles(self.htf_interval)

    def last_price(self) -> float | None:
        f = self._forming.get(self.entry_interval)
        if f is not None:
            return f.close
        closed = self.entry_closed()
        return closed[-1].close if closed else None

    def newest_entry_ts(self) -> int | None:
        closed = self.entry_closed()
        return closed[-1].ts_ms if closed else None

    def _ingest(self, interval: str, candles: list[Candle]) -> None:
        """Merge a batch (any order). The newest bar is the in-progress bar →
        forming, not closed."""
        if not candles:
            return
        parsed = sorted(candles, key=lambda c: c.ts_ms)
        self._forming[interval] = parsed[-1]
        store = self._bars[interval]
        for c in parsed[:-1]:                       # all but the forming bar
            store[c.ts_ms] = c
        # trim history
        if len(store) > MAX_BARS:
            for k in sorted(store)[:-MAX_BARS]:
                del store[k]

    # ------------------------------------------------------- source adapters
    # Each returns a list[Candle] (any order) or raises on failure.

    async def _src_bybit(self, client: httpx.AsyncClient, interval: str,
                         limit: int | None = None) -> list[Candle]:
        params = {"category": "linear", "symbol": self.symbol,
                  "interval": interval, "limit": limit or self.limit}
        r = await client.get(self._bybit_url, params=params)
        r.raise_for_status()
        d = r.json()
        if d.get("retCode") != 0:
            raise RuntimeError(f"retCode {d.get('retCode')}: {d.get('retMsg')}")
        # rows newest-first: [start, o, h, l, c, v, turnover] as strings
        return [Candle(ts_ms=int(x[0]), open=float(x[1]), high=float(x[2]),
                       low=float(x[3]), close=float(x[4]), volume=float(x[5]))
                for x in d.get("result", {}).get("list", [])]

    async def _src_binance(self, client: httpx.AsyncClient, interval: str,
                          limit: int | None = None) -> list[Candle]:
        iv = _BINANCE_IV.get(interval)
        if iv is None:
            raise RuntimeError(f"unsupported interval {interval}")
        r = await client.get(BINANCE_REST,
                             params={"symbol": self.symbol, "interval": iv,
                                     "limit": limit or self.limit})
        r.raise_for_status()
        rows = r.json()
        if not isinstance(rows, list):
            raise RuntimeError(str(rows)[:80])
        # rows oldest-first: [openTime, o, h, l, c, v, closeTime, ...]
        return [Candle(ts_ms=int(x[0]), open=float(x[1]), high=float(x[2]),
                       low=float(x[3]), close=float(x[4]), volume=float(x[5]))
                for x in rows]

    async def _src_okx(self, client: httpx.AsyncClient, interval: str,
                      limit: int | None = None) -> list[Candle]:
        iv = _OKX_IV.get(interval)
        if iv is None:
            raise RuntimeError(f"unsupported interval {interval}")
        inst = f"{_base_of(self.symbol)}-USDT-SWAP"
        r = await client.get(OKX_REST,
                             params={"instId": inst, "bar": iv,
                                     "limit": limit or self.limit})
        r.raise_for_status()
        d = r.json()
        if str(d.get("code", "0")) != "0":
            raise RuntimeError(f"code {d.get('code')}: {d.get('msg')}")
        # rows newest-first: [ts, o, h, l, c, vol, volCcy, ...]
        return [Candle(ts_ms=int(x[0]), open=float(x[1]), high=float(x[2]),
                       low=float(x[3]), close=float(x[4]), volume=float(x[5]))
                for x in d.get("data", [])]

    # ------------------------------------------------------------- fetch

    async def poll_once(self, client: httpx.AsyncClient) -> None:
        """Fetch BOTH intervals from the first reachable source (same source for
        both, so the strategy never mixes venues within a poll)."""
        errors: list[str] = []
        for name, fn in self._sources:
            try:
                entry = await fn(client, self.entry_interval)
                if not entry:
                    raise RuntimeError("empty entry list")
                htf = await fn(client, self.htf_interval)
                self._ingest(self.entry_interval, entry)
                self._ingest(self.htf_interval, htf)
                self.source = name
                self.last_error = ""
                self._polls += 1
                if self._polls % CROSS_CHECK_EVERY == 0:
                    await self._cross_check(client)
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:                  # noqa: BLE001 — try next source
                errors.append(f"{name}: {type(e).__name__}: {str(e)[:50]}")
        self.source = "none"
        raise RuntimeError(" | ".join(errors) or "no sources configured")

    async def _cross_check(self, client: httpx.AsyncClient) -> None:
        """Compare the live source's latest price with a second venue. Advisory
        only: sets divergence/cross_* fields, never rejects data or raises."""
        mine = self.last_price()
        if mine is None or mine <= 0:
            return
        for name, fn in self._sources:
            if name == self.source:
                continue
            try:
                bars = await fn(client, self.entry_interval, 2)
                other = sorted(bars, key=lambda c: c.ts_ms)[-1].close if bars else None
                if other and other > 0:
                    dev = abs(mine - other) / other * 100.0
                    self.cross_source = name
                    self.cross_dev_pct = round(dev, 3)
                    self.divergence = dev > MAX_SOURCE_DEV_PCT
                    return
            except asyncio.CancelledError:
                raise
            except Exception:                       # noqa: BLE001 — advisory only
                continue
        # no second venue reachable — can't cross-check
        self.cross_source = ""
        self.cross_dev_pct = None
        self.divergence = False

    async def run(self, stop: asyncio.Event, poll_sec: float = 2.0) -> None:
        """Poll until stopped; never raises out."""
        backoff = 2.0
        ua = "Mozilla/5.0 (compatible; coinmaster-pro/1.0)"
        async with httpx.AsyncClient(timeout=12, headers={"User-Agent": ua}) as client:
            while not stop.is_set():
                try:
                    await self.poll_once(client)
                    backoff = 2.0
                    wait = poll_sec
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    self.last_error = f"{type(e).__name__}: {e}"[:200]
                    wait = backoff
                    backoff = min(30.0, backoff * 1.6)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=wait)
                except asyncio.TimeoutError:
                    pass
        self.source = "none"

    def status(self) -> dict:
        return {
            "source": self.source,
            "sources": [n for n, _ in self._sources],
            "entry_bars": len(self._bars[self.entry_interval]),
            "htf_bars": len(self._bars[self.htf_interval]),
            "last_price": self.last_price(),
            "polls": self._polls,
            "last_error": self.last_error,
            "cross_source": self.cross_source,
            "cross_dev_pct": self.cross_dev_pct,
            "divergence": self.divergence,
        }
