"""@responsibility 통합 계좌 원장 — 전 심볼 공유 equity 단일 소유, 페이퍼 잔고 시뮬과 Phase 3 지갑 조회의 교체 지점

Unified account ledger (한 계좌 원칙). ONE equity pool backs every symbol:
position sizing, the risk caps' denominators and 복리 compounding all read
and write this single number — mirroring the real Bybit USDT wallet, where
every symbol's orders draw from one balance.

Paper mode simulates the balance here and persists it via AccountStore
(every change is written through, so it survives restarts). Phase 3 live
mode will swap the balance source for the exchange wallet query — this
class is the ONLY place that answers "how much money do we have".

Migration: when no account record exists yet, the ledger inherits the
legacy per-symbol equity passed in (운영 심볼 BTC 계좌 승계); test
symbols' paper PnL is intentionally discarded at the cutover. Once an
account record exists it always wins over the legacy value.

A ledger built without a store (backtests, unit tests) is a private
in-memory pool seeded from config — the old standalone behaviour.
"""
from __future__ import annotations


class AccountLedger:
    def __init__(self, cfg, store=None, legacy_equity: float | None = None):
        self.cfg = cfg
        self._store = store
        eq: float | None = None
        if store is not None:
            rec = store.load()
            if rec and rec.get("equity") is not None:
                eq = float(rec["equity"])
        if eq is None:
            eq = float(legacy_equity) if legacy_equity else float(cfg.equity_usd)
            if store is not None:
                store.save({"equity": round(eq, 8)})
        self._equity = eq

    @property
    def equity(self) -> float:
        return self._equity

    def set(self, value: float) -> None:
        self._equity = float(value)
        if self._store is not None:
            self._store.save({"equity": round(self._equity, 8)})
