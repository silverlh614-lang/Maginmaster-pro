"""@responsibility 포지션 FSM — 진입·ATR손절·2R익절·부분청산·트레일링·피라미딩·정산을 소유하는 페이퍼 체결기

Position state machine (paper edition). Owns everything the strategy does
NOT: sizing via the risk gate, order fills, the ATR hard stop, the 2R
take-profit, the partial exit + breakeven move, ATR trailing and pyramiding
adds. Consumes CLOSED candles; a paper fill model checks stop/target
intrabar (high/low) and assumes the stop fills first when a bar spans both
(conservative). Realized PnL compounds into equity — the source's 복리.
"""
from __future__ import annotations

import time

from ..account import AccountLedger
from ..config import BybitConfig, SymbolSpec
from ..models import Candle, Position, PositionState, Side, TradeSignal, Unit
from ..risk import BybitRiskManager, size_position
from ..store import Journal


class PositionManager:
    def __init__(self, spec: SymbolSpec, cfg: BybitConfig, risk: BybitRiskManager,
                 journal: Journal, mode: str = "paper", strategy_name: str = "",
                 ledger: AccountLedger | None = None):
        self.spec = spec
        self.cfg = cfg
        self.risk = risk
        self.journal = journal
        self.mode = mode
        self.strategy_name = strategy_name
        # 자산은 전 심볼 공유 원장(한 계좌) 소유 — 미지정 시(백테스트·단위
        # 테스트) 이 심볼만의 비영속 로컬 원장으로 폴백.
        self._ledger = ledger if ledger is not None else AccountLedger(cfg)
        self.pos: Position | None = None
        self.bars_in_trade = 0
        self.note = "flat"
        # 리스크 캡은 전 심볼 합산이 원칙 — 관문에 이 심볼의 장부를 등록한다.
        # (백테스트의 permissive shim 처럼 등록 개념이 없는 관문이면 생략)
        if hasattr(risk, "register_book"):
            risk.register_book(spec.key, self)

    @property
    def equity(self) -> float:
        return self._ledger.equity

    @equity.setter
    def equity(self, value: float) -> None:
        self._ledger.set(value)

    # ------------------------------------------------------- persistence

    def to_state(self) -> dict:
        """Snapshot for restart survival. The live position only — equity is
        the account ledger's job (AccountStore), not per-symbol state."""
        return {"bars_in_trade": self.bars_in_trade,
                "note": self.note,
                "position": _pos_to_dict(self.pos) if self.pos else None}

    def load_state(self, st: dict | None) -> None:
        """Restore a snapshot saved by to_state(). Absent/empty -> fresh start.
        A legacy `equity` field in old records is ignored here — the account
        ledger inherits it once at BybitManager construction (migration)."""
        if not st:
            return
        self.bars_in_trade = int(st.get("bars_in_trade", 0))
        self.note = st.get("note", self.note)
        p = st.get("position")
        self.pos = _pos_from_dict(p) if p else None

    # ------------------------------------------------------------- helpers

    @property
    def open_positions(self) -> int:
        return 1 if self.pos and self.pos.state == PositionState.OPEN else 0

    @property
    def open_risk_usd(self) -> float:
        """Risk still on the table = open_qty * distance to the effective stop."""
        p = self.pos
        if not p or p.state != PositionState.OPEN or p.stop_price is None:
            return 0.0
        return p.open_qty * abs(p.avg_entry - p.stop_price)

    def _exposure(self) -> tuple[int, float]:
        """Exposure fed to the risk gate: GLOBAL across symbols when the gate
        keeps a book registry, own-symbol only otherwise (backtest shim)."""
        if hasattr(self.risk, "global_exposure"):
            return self.risk.global_exposure()
        return self.open_positions, self.open_risk_usd

    def _fee(self, notional: float) -> float:
        return round(abs(notional) * self.cfg.taker_fee_frac, 6)

    def _round_price(self, px: float) -> float:
        ts = self.spec.tick_size
        return round(round(px / ts) * ts, 8) if ts > 0 else px

    # ------------------------------------------------------------- entry

    def try_open(self, sig: TradeSignal, entry_price: float, atr_val: float,
                 ts: float) -> bool:
        """Open a fresh position at entry_price with an ATR stop, if the risk
        gate allows. Returns True on fill."""
        if self.pos and self.pos.state == PositionState.OPEN:
            return False
        entry = self._round_price(entry_price)
        stop = self._round_price(sig.stop_price)
        qty, risk_usd, why = size_position(
            self.equity, self.cfg.risk_per_trade_pct, entry, stop,
            self.spec, self.cfg.leverage_max)
        if qty <= 0:
            self.note = f"size skip: {why}"
            return False
        open_n, open_risk = self._exposure()
        ok, gate = self.risk.allow_entry(open_n, open_risk,
                                         risk_usd, is_add=False,
                                         equity_usd=self.equity)
        if not ok:
            self.note = f"entry blocked: {gate}"
            return False
        dist = abs(entry - stop)
        target = (entry + dist * self.cfg.rr_target if sig.side is Side.LONG
                  else entry - dist * self.cfg.rr_target)
        fee = self._fee(entry * qty)
        unit = Unit(side=sig.side, entry_price=entry, qty=qty, stop_price=stop,
                    entry_ts=ts, fee_usd=fee)
        self.pos = Position(symbol=self.spec.key, side=sig.side, units=[unit],
                            initial_risk_usd=risk_usd,
                            target_price=self._round_price(target))
        self.pos.realized_fee_usd = fee
        self.equity -= fee
        self.bars_in_trade = 0
        self.note = f"OPEN {sig.side.value} @ {entry:g} stop {stop:g}"
        self._journal("OPEN", sig, unit, "", 0.0, fee)
        return True

    def try_add(self, sig: TradeSignal, price: float, atr_val: float,
                ts: float) -> bool:
        """Pyramiding add (애드업): only when already in profit past the
        configured R and under the add cap, and only through the risk gate."""
        p = self.pos
        if not (self.cfg.pyramid_enabled and p and p.state == PositionState.OPEN):
            return False
        if sig.side is not p.side or p.adds >= self.cfg.pyramid_max_adds:
            return False
        entry = self._round_price(price)
        moved = p.side.sign * (entry - p.avg_entry)
        r_ahead = moved / (p.initial_risk_usd / p.open_qty) if p.open_qty else 0
        if r_ahead < self.cfg.pyramid_min_r:
            return False
        stop = self._round_price(sig.stop_price)
        qty, risk_usd, why = size_position(
            self.equity, self.cfg.risk_per_trade_pct, entry, stop,
            self.spec, self.cfg.leverage_max)
        if qty <= 0:
            return False
        open_n, open_risk = self._exposure()
        ok, gate = self.risk.allow_entry(open_n, open_risk,
                                         risk_usd, is_add=True,
                                         equity_usd=self.equity)
        if not ok:
            self.note = f"add blocked: {gate}"
            return False
        fee = self._fee(entry * qty)
        unit = Unit(side=sig.side, entry_price=entry, qty=qty, stop_price=stop,
                    entry_ts=ts, is_add=True, fee_usd=fee)
        p.units.append(unit)
        p.realized_fee_usd += fee
        self.equity -= fee
        self.note = f"ADD #{p.adds} {sig.side.value} @ {entry:g}"
        self._journal("ADD", sig, unit, "", 0.0, fee)
        return True

    # ------------------------------------------------------------- manage

    def manage(self, candle: Candle, atr_val: float | None) -> None:
        """Run stop / target / partial / trailing / time-stop against one
        just-closed candle."""
        p = self.pos
        if not p or p.state != PositionState.OPEN:
            return
        self.bars_in_trade += 1
        long = p.side is Side.LONG
        stop = p.stop_price

        # 1) hard stop first (conservative when a bar spans stop AND target)
        if stop is not None and ((long and candle.low <= stop)
                                 or (not long and candle.high >= stop)):
            self._close(stop, "stop hit", candle.ts_ms / 1000)
            return

        # 2) first take-profit (2R): scale out partial_tp_frac, arm breakeven
        tgt = p.target_price
        if (not p.partial_done and tgt is not None
                and ((long and candle.high >= tgt) or (not long and candle.low <= tgt))):
            self._take_partial(tgt, candle.ts_ms / 1000)

        # 3) trailing stop on the remainder, ratcheting only tighter
        if p.partial_done and atr_val and p.open_qty > 0:
            trail = (candle.close - atr_val * self.cfg.trail_atr_mult if long
                     else candle.close + atr_val * self.cfg.trail_atr_mult)
            trail = self._round_price(trail)
            if p.trail_price is None:
                p.trail_price = trail
            else:
                p.trail_price = max(p.trail_price, trail) if long \
                    else min(p.trail_price, trail)

        # 4) optional time stop
        if (self.cfg.time_stop_bars and self.bars_in_trade >= self.cfg.time_stop_bars
                and p.state == PositionState.OPEN):
            self._close(candle.close, f"time stop {self.cfg.time_stop_bars} bars",
                        candle.ts_ms / 1000)

    def _take_partial(self, price: float, ts: float) -> None:
        p = self.pos
        assert p is not None
        qty = round(p.open_qty * self.cfg.partial_tp_frac, 8)
        qty = (int(qty / self.spec.qty_step)) * self.spec.qty_step \
            if self.spec.qty_step > 0 else qty
        if qty <= 0:
            return
        pnl = p.side.sign * (price - p.avg_entry) * qty
        fee = self._fee(price * qty)
        p.closed_qty += qty
        p.realized_pnl_usd += pnl - fee
        p.realized_fee_usd += fee
        p.partial_done = True
        self.equity += pnl - fee
        if self.cfg.breakeven_after_tp:
            p.trail_price = self._round_price(p.avg_entry)
        self.note = f"PARTIAL {qty:g} @ {price:g} (+{p.r_multiple()}R)"
        # result stays blank so aggregates (settled rows only) never
        # double-count this leg — the FULL position PnL is booked at CLOSE.
        # pnl/r ARE recorded: this row's own realized cash for the journal.
        leg_r = (round((pnl - fee) / p.initial_risk_usd, 3)
                 if p.initial_risk_usd > 0 else None)
        self._journal("PARTIAL", None, None, "", round(pnl - fee, 4), fee,
                      exit_price=price, qty=qty, r=leg_r)

    def _close(self, price: float, reason: str, ts: float) -> None:
        p = self.pos
        assert p is not None
        qty = p.open_qty
        if qty > 0:
            pnl = p.side.sign * (price - p.avg_entry) * qty
            fee = self._fee(price * qty)
            p.closed_qty += qty
            p.realized_pnl_usd += pnl - fee
            p.realized_fee_usd += fee
            self.equity += pnl - fee
        p.state = PositionState.CLOSED
        total = round(p.realized_pnl_usd, 4)
        result = "WIN" if total > 0 else "LOSS"
        if p.partial_done and reason == "stop hit":
            result = "CLOSED"      # banked partial + trailed exit = mixed
        self.note = f"CLOSE @ {price:g} pnl {total:+.2f} ({p.r_multiple()}R) {reason}"
        self._journal("CLOSE", None, None, result, total,
                      round(p.realized_fee_usd, 4), exit_price=price,
                      qty=qty, r=p.r_multiple())
        # keep the closed position visible for one status cycle, then flatten
        self.pos = p

    def flatten_if_closed(self) -> None:
        if self.pos and self.pos.state == PositionState.CLOSED:
            self.pos = None
            self.note = "flat"

    # ------------------------------------------------------------- journal

    def _journal(self, event: str, sig: TradeSignal | None, unit: Unit | None,
                 result: str, pnl: float, fee: float, exit_price=None,
                 qty=None, r=None) -> None:
        p = self.pos
        entry = unit.entry_price if unit else (p.avg_entry if p else "")
        q = unit.qty if unit else (qty if qty is not None else "")
        notional = round((entry or 0) * (q or 0), 4) if entry and q else ""
        lev = round(notional / self.equity, 2) if notional and self.equity else ""
        self.journal.append({
            "symbol": self.spec.key, "mode": self.mode,
            "strategy": self.strategy_name,
            "event": event, "side": (sig.side.value if sig else (p.side.value if p else "")),
            "signal_type": sig.signal_type if sig else "",
            "signal_detail": sig.detail if sig else "",
            "entry_price": round(entry, 6) if entry else "",
            "exit_price": round(exit_price, 6) if exit_price else "",
            "qty": q, "leverage": lev, "notional_usd": notional,
            "risk_usd": round(p.initial_risk_usd, 4) if p else "",
            "result": result,
            # PARTIAL 행도 실현 현금을 기록한다 (result 는 비워 집계 중복 방지)
            "pnl_usd": pnl if (result or event == "PARTIAL") else "",
            "r_multiple": r if r is not None else "",
            "fee_usd": fee, "reason": self.note,
        })

    # ------------------------------------------------------------- view

    def snapshot(self, last_price: float | None) -> dict:
        p = self.pos
        d = {"equity_usd": round(self.equity, 4), "note": self.note,
             "bars_in_trade": self.bars_in_trade, "position": None}
        if p:
            pd = p.as_dict()
            if last_price is not None and p.state == PositionState.OPEN:
                pd["unrealized_usd"] = p.unrealized_usd(last_price)
            d["position"] = pd
        return d


# ----------------------------------------------------- (de)serialization
# Full round-trip of the position (unlike Position.as_dict, which is a lossy
# display view) so a live trade can be rebuilt exactly after a restart.

def _unit_to_dict(u: Unit) -> dict:
    return {"side": u.side.value, "entry_price": u.entry_price, "qty": u.qty,
            "stop_price": u.stop_price, "entry_ts": u.entry_ts,
            "is_add": u.is_add, "fee_usd": u.fee_usd}


def _unit_from_dict(d: dict) -> Unit:
    return Unit(side=Side(d["side"]), entry_price=d["entry_price"], qty=d["qty"],
                stop_price=d["stop_price"], entry_ts=d["entry_ts"],
                is_add=d.get("is_add", False), fee_usd=d.get("fee_usd", 0.0))


def _pos_to_dict(p: Position) -> dict:
    return {"symbol": p.symbol, "side": p.side.value,
            "units": [_unit_to_dict(u) for u in p.units],
            "initial_risk_usd": p.initial_risk_usd,
            "target_price": p.target_price, "trail_price": p.trail_price,
            "realized_pnl_usd": p.realized_pnl_usd,
            "realized_fee_usd": p.realized_fee_usd,
            "closed_qty": p.closed_qty, "partial_done": p.partial_done,
            "state": p.state.value}


def _pos_from_dict(d: dict) -> Position:
    return Position(
        symbol=d["symbol"], side=Side(d["side"]),
        units=[_unit_from_dict(u) for u in d.get("units", [])],
        initial_risk_usd=d.get("initial_risk_usd", 0.0),
        target_price=d.get("target_price"), trail_price=d.get("trail_price"),
        realized_pnl_usd=d.get("realized_pnl_usd", 0.0),
        realized_fee_usd=d.get("realized_fee_usd", 0.0),
        closed_qty=d.get("closed_qty", 0.0),
        partial_done=d.get("partial_done", False),
        state=PositionState(d["state"]))
