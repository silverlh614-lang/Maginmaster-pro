"""Offline tests for GLOBAL (cross-symbol) risk caps — the concurrent-position
and open-risk limits must sum EVERY symbol's exposure, not just the entering
symbol's. Run:  python -m tests.test_risk_global"""
from __future__ import annotations

# tests.test_bybit isolates DATA_DIR before importing the app package.
from tests.test_bybit import _c                            # noqa: F401

from app.trading_bybit.config import SYMBOL_SPECS, BybitConfig  # noqa: E402
from app.trading_bybit.execution.position import PositionManager  # noqa: E402
from app.trading_bybit.models import Side, TradeSignal    # noqa: E402
from app.trading_bybit.risk import BybitRiskManager       # noqa: E402
from app.trading_bybit.store import BotState, Journal     # noqa: E402

SIG_BTC = TradeSignal(Side.LONG, "T", 80, stop_price=98, entry_hint=100)
SIG_ETH = TradeSignal(Side.LONG, "T", 80, stop_price=1720, entry_hint=1750)


def _pair(cfg):
    """One shared risk gate, two symbol books (the live BybitManager shape)."""
    risk = BybitRiskManager(cfg, Journal(), BotState())
    btc = PositionManager(SYMBOL_SPECS["BTC"], cfg, risk, Journal(), "paper", "t")
    eth = PositionManager(SYMBOL_SPECS["ETH"], cfg, risk, Journal(), "paper", "t")
    return risk, btc, eth


def test_concurrent_cap_across_symbols():
    """max_concurrent_positions=1 must block a SECOND symbol's entry while the
    first symbol holds a position (regression: the cap was per-symbol)."""
    cfg = BybitConfig()
    cfg.max_concurrent_positions = 1
    risk, btc, eth = _pair(cfg)
    assert btc.try_open(SIG_BTC, 100, 2.0, 0)
    assert not eth.try_open(SIG_ETH, 1750, 8.0, 1)
    assert "concurrent" in eth.note, eth.note
    # BTC closes (stop hit) -> the slot frees up and ETH may enter
    btc.manage(_c(60000, 100, 100.5, 97, 97.5), 2.0)
    assert btc.pos.state.value == "CLOSED"
    assert eth.try_open(SIG_ETH, 1750, 8.0, 2), eth.note
    print("ok  concurrent cap across symbols (block while open, free on close)")


def test_open_risk_cap_across_symbols():
    """The open-risk cap must sum risk across symbols: BTC $2 on the table +
    ETH $1.8 new > 1.5% cap ($3 on $200) -> ETH blocked."""
    cfg = BybitConfig()
    cfg.max_concurrent_positions = 2
    cfg.max_total_open_risk_pct = 1.5
    risk, btc, eth = _pair(cfg)
    assert btc.try_open(SIG_BTC, 100, 2.0, 0)
    n, r = risk.global_exposure()
    assert n == 1 and abs(r - 2.0) < 1e-9, (n, r)
    assert not eth.try_open(SIG_ETH, 1750, 8.0, 1)
    assert "total_open_risk" in eth.note, eth.note
    print("ok  open-risk cap across symbols (sum of books vs cap)")


def test_restart_reregisters_without_double_count():
    """A bot restart builds a NEW PositionManager for the same symbol — the
    book must be replaced, not appended, or every restart doubles exposure."""
    cfg = BybitConfig()
    risk, btc, _eth = _pair(cfg)
    assert btc.try_open(SIG_BTC, 100, 2.0, 0)
    st = btc.to_state()
    btc2 = PositionManager(SYMBOL_SPECS["BTC"], cfg, risk, Journal(), "paper", "t")
    btc2.load_state(st)
    n, r = risk.global_exposure()
    assert n == 1 and abs(r - 2.0) < 1e-9, (n, r)
    print("ok  restart re-registers the book (no double counting)")


def test_backtest_shim_fallback():
    """The backtest's permissive risk shim has no book registry — the manager
    must fall back to own-symbol exposure and still fill."""
    from app.trading_bybit.backtest.engine import _MemJournal, _PermissiveRisk
    cfg = BybitConfig()
    pm = PositionManager(SYMBOL_SPECS["BTC"], cfg, _PermissiveRisk(),
                         _MemJournal(), "backtest", "t")
    assert pm.try_open(SIG_BTC, 100, 2.0, 0)
    print("ok  backtest permissive shim fallback (own-symbol exposure)")


if __name__ == "__main__":
    test_concurrent_cap_across_symbols()
    test_open_risk_cap_across_symbols()
    test_restart_reregisters_without_double_count()
    test_backtest_shim_fallback()
    print("\nall global risk-cap tests passed ✅")
