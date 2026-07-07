"""@responsibility Bybit 트레이드 저널(CSV)·봇 상태 영속화 — 포지션 이벤트 기록과 재시작 생존 단일 통로

Trade journal (CSV) and bot-state persistence for the Bybit package. One row
per position lifecycle event (OPEN / ADD / PARTIAL / CLOSE). Aggregates read
the settled rows (WIN/LOSS/CLOSED) for PnL and count OPEN rows for the daily
trade cap. CSV so results survive restarts and export cleanly to Excel.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
TRADES_CSV = DATA_DIR / "bybit_trades.csv"
STATE_JSON = DATA_DIR / "bybit_state.json"
POSITIONS_JSON = DATA_DIR / "bybit_positions.json"
ACCOUNT_JSON = DATA_DIR / "bybit_account.json"

FIELDS = [
    "ts", "symbol", "mode", "strategy", "event", "side", "signal_type",
    "signal_detail", "entry_price", "exit_price", "qty", "leverage",
    "notional_usd", "risk_usd", "result", "pnl_usd", "r_multiple",
    "fee_usd", "reason",
]

# event vocabulary
OPEN_EVENTS = ("OPEN",)                        # a new position started
SETTLED_RESULTS = ("WIN", "LOSS", "CLOSED")    # a position (or leg) realized PnL

_lock = threading.Lock()


def _utcnow() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class Journal:
    """CSV-backed journal, mtime-cached so status polling doesn't reparse."""

    def __init__(self):
        self._cache_key: tuple | None = None
        self._cache_rows: list[dict] = []

    def append(self, rec: dict) -> dict:
        row = {k: rec.get(k, "") for k in FIELDS}
        row["ts"] = row["ts"] or _utcnow()
        with _lock:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            new = not TRADES_CSV.exists()
            with TRADES_CSV.open("a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                if new:
                    w.writeheader()
                w.writerow(row)
        return row

    def _rows(self) -> list[dict]:
        if not TRADES_CSV.exists():
            return []
        with _lock:
            st = TRADES_CSV.stat()
            key = (st.st_mtime_ns, st.st_size)
            if key != self._cache_key:
                with TRADES_CSV.open(newline="", encoding="utf-8") as f:
                    self._cache_rows = list(csv.DictReader(f))
                self._cache_key = key
            return self._cache_rows

    def tail(self, n: int = 50, symbol: str | None = None) -> list[dict]:
        rows = self._rows()
        if symbol:
            rows = [r for r in rows if r["symbol"] == symbol.upper()]
        return rows[-n:][::-1]

    def aggregate(self, day: str | None = None,
                  symbol: str | None = None) -> dict:
        """Overall (or per-UTC-day / per-symbol) stats. trades = OPEN events;
        settled = realized legs; pnl = sum over settled."""
        rows = self._rows()
        if day:
            rows = [r for r in rows if r["ts"][:10] == day]
        if symbol:
            rows = [r for r in rows if r["symbol"] == symbol.upper()]
        settled = [r for r in rows if r["result"] in SETTLED_RESULTS]
        wins = sum(1 for r in settled if r["result"] == "WIN")
        pnl = sum(float(r["pnl_usd"] or 0) for r in settled)
        fees = sum(float(r["fee_usd"] or 0) for r in rows)
        rs = [float(r["r_multiple"]) for r in settled if r["r_multiple"] not in ("", None)]
        return {
            "records": len(rows),
            "trades": sum(1 for r in rows if r["event"] in OPEN_EVENTS),
            "settled": len(settled),
            "wins": wins,
            "losses": len(settled) - wins,
            "win_rate": round(wins / len(settled), 4) if settled else None,
            "pnl_usd": round(pnl, 4),
            "fees_usd": round(fees, 4),
            "avg_r": round(sum(rs) / len(rs), 3) if rs else None,
            "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None,
        }

    def by_symbol(self, symbols: list[str]) -> dict:
        return {s: self.aggregate(symbol=s) for s in symbols}


class BotState:
    """Small JSON blob: mode, kill switch, running flag — survives restarts."""

    def load(self) -> dict:
        if STATE_JSON.exists():
            try:
                return json.loads(STATE_JSON.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def save(self, state: dict) -> None:
        with _lock:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            STATE_JSON.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                                  encoding="utf-8")


class AccountStore:
    """Single JSON record for the unified account (shared equity across all
    symbols — 한 계좌). Written through by AccountLedger on every change."""

    def load(self) -> dict:
        if ACCOUNT_JSON.exists():
            try:
                return json.loads(ACCOUNT_JSON.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def save(self, rec: dict) -> None:
        with _lock:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            ACCOUNT_JSON.write_text(json.dumps(rec, ensure_ascii=False, indent=2),
                                    encoding="utf-8")


class PositionStore:
    """Per-symbol PositionManager snapshots (the live open position) so an
    in-flight trade survives restarts and redeploys. Equity lives in
    AccountStore (통합 계좌); a legacy `equity` field in old records is only
    read once, for migration. Requires DATA_DIR on a persistent volume."""

    def _read(self) -> dict:
        if POSITIONS_JSON.exists():
            try:
                return json.loads(POSITIONS_JSON.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def load(self, symbol: str) -> dict:
        return self._read().get(symbol.upper(), {})

    def save(self, symbol: str, state: dict) -> None:
        with _lock:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            allst = self._read()
            allst[symbol.upper()] = state
            POSITIONS_JSON.write_text(
                json.dumps(allst, ensure_ascii=False, indent=2), encoding="utf-8")
