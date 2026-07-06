"""@responsibility 캔들 수집기 — Bybit v5 공개 kline REST 폴링으로 진입·상위 시간봉 OHLCV 유지 (인증 불필요)

Candle collector. Polls Bybit's PUBLIC v5 kline REST endpoint (no API key)
for both the entry and higher timeframes and keeps a deduped, time-sorted
buffer of CLOSED candles per interval. This is the package's single market-
data channel — strategy and backtest see identical Candle objects. The
newest returned bar is still forming, so it is exposed as last_price but
excluded from the closed-candle list the strategy consumes.
"""
from __future__ import annotations

import asyncio

import httpx

from ..models import Candle

BYBIT_REST = "https://api.bybit.com/v5/market/kline"
BYBIT_TESTNET_REST = "https://api-testnet.bybit.com/v5/market/kline"
MAX_BARS = 400          # rolling history kept per interval


class KlineCollector:
    def __init__(self, symbol: str, entry_interval: str, htf_interval: str,
                 limit: int = 200, testnet: bool = False):
        self.symbol = symbol
        self.entry_interval = entry_interval
        self.htf_interval = htf_interval
        self.limit = limit
        self.base = BYBIT_TESTNET_REST if testnet else BYBIT_REST
        # ts_ms -> Candle, per interval (dedupe on bar open time)
        self._bars: dict[str, dict[int, Candle]] = {entry_interval: {},
                                                     htf_interval: {}}
        self._forming: dict[str, Candle | None] = {entry_interval: None,
                                                    htf_interval: None}
        self.source = "none"
        self.last_error = ""
        self._polls = 0

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

    def _ingest(self, interval: str, rows: list[list]) -> None:
        """rows = Bybit result.list, newest-first, each
        [start, open, high, low, close, volume, turnover] as strings.
        The newest row is the in-progress bar → forming, not closed."""
        parsed = []
        for r in rows:
            parsed.append(Candle(ts_ms=int(r[0]), open=float(r[1]),
                                 high=float(r[2]), low=float(r[3]),
                                 close=float(r[4]), volume=float(r[5])))
        if not parsed:
            return
        parsed.sort(key=lambda c: c.ts_ms)
        self._forming[interval] = parsed[-1]
        store = self._bars[interval]
        for c in parsed[:-1]:                       # all but the forming bar
            store[c.ts_ms] = c
        # trim history
        if len(store) > MAX_BARS:
            for k in sorted(store)[:-MAX_BARS]:
                del store[k]

    # ------------------------------------------------------------- fetch

    async def _fetch(self, client: httpx.AsyncClient, interval: str) -> None:
        params = {"category": "linear", "symbol": self.symbol,
                  "interval": interval, "limit": self.limit}
        r = await client.get(self.base, params=params)
        r.raise_for_status()
        data = r.json()
        if data.get("retCode") != 0:
            raise RuntimeError(f"bybit retCode {data.get('retCode')}: "
                               f"{data.get('retMsg')}")
        self._ingest(interval, data.get("result", {}).get("list", []))

    async def poll_once(self, client: httpx.AsyncClient) -> None:
        for interval in (self.htf_interval, self.entry_interval):
            await self._fetch(client, interval)
        self._polls += 1
        self.source = "bybit_rest"
        self.last_error = ""

    async def run(self, stop: asyncio.Event, poll_sec: float = 2.0) -> None:
        """Poll both intervals until stopped; never raises out."""
        backoff = 2.0
        async with httpx.AsyncClient(timeout=12,
                                     headers={"User-Agent": "coinmaster-pro"}) as client:
            while not stop.is_set():
                try:
                    await self.poll_once(client)
                    backoff = 2.0
                    wait = poll_sec
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    self.last_error = f"{type(e).__name__}: {e}"
                    self.source = "none"
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
            "entry_bars": len(self._bars[self.entry_interval]),
            "htf_bars": len(self._bars[self.htf_interval]),
            "last_price": self.last_price(),
            "polls": self._polls,
            "last_error": self.last_error,
        }
