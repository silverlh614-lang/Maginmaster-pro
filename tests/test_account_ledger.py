"""Offline tests for the unified account ledger (한 계좌) — one shared equity
pool across symbols, write-through persistence, legacy migration.
Run:  python -m tests.test_account_ledger"""
from __future__ import annotations

# tests.test_bybit isolates DATA_DIR before importing the app package.
from tests.test_bybit import _c                            # noqa: F401

from app.trading_bybit import store as store_mod           # noqa: E402
from app.trading_bybit.account import AccountLedger        # noqa: E402
from app.trading_bybit.config import SYMBOL_SPECS, BybitConfig  # noqa: E402
from app.trading_bybit.execution.position import PositionManager  # noqa: E402
from app.trading_bybit.models import Side, TradeSignal    # noqa: E402
from app.trading_bybit.risk import BybitRiskManager       # noqa: E402
from app.trading_bybit.store import (AccountStore, BotState, Journal,  # noqa: E402
                                     PositionStore)

SIG = TradeSignal(Side.LONG, "T", 80, stop_price=98, entry_hint=100)


def test_shared_equity_pool():
    """Every symbol draws from ONE pool: BTC's fees and settled PnL must be
    visible to ETH's sizing immediately (실계좌의 단일 USDT 지갑과 동일)."""
    cfg = BybitConfig()
    cfg.max_concurrent_positions = 2
    cfg.max_total_open_risk_pct = 10.0
    ledger = AccountLedger(cfg)
    risk = BybitRiskManager(cfg, Journal(), BotState())
    btc = PositionManager(SYMBOL_SPECS["BTC"], cfg, risk, Journal(),
                          "paper", "t", ledger=ledger)
    eth = PositionManager(SYMBOL_SPECS["ETH"], cfg, risk, Journal(),
                          "paper", "t", ledger=ledger)
    assert btc.equity == eth.equity == cfg.equity_usd

    assert btc.try_open(SIG, 100, 2.0, 0)
    # BTC 진입 수수료가 공유 계좌에서 차감 — ETH도 같은 잔고를 본다
    assert btc.equity < cfg.equity_usd and eth.equity == btc.equity

    btc.manage(_c(60000, 100, 104.5, 100, 103), 2.0)    # 2R partial
    btc.manage(_c(120000, 103, 103.2, 99, 99.5), 2.0)   # breakeven trail out
    assert btc.pos.state.value == "CLOSED"
    assert eth.equity == btc.equity == ledger.equity     # 정산도 한 계좌로
    assert eth.equity > cfg.equity_usd                   # net win compounds
    print("ok  shared equity pool (fees + settlements hit ONE account)")


def test_persistence_and_legacy_migration():
    cfg = BybitConfig()
    store = AccountStore()
    store_mod.ACCOUNT_JSON.unlink(missing_ok=True)

    # 1) 레코드도 레거시도 없음 -> 설정 시드 + 레코드 생성
    led = AccountLedger(cfg, store)
    assert led.equity == cfg.equity_usd
    assert abs(store.load()["equity"] - cfg.equity_usd) < 1e-9

    # 2) 레코드 없음 + 레거시(구 심볼별 equity) 있음 -> 1회 승계
    store_mod.ACCOUNT_JSON.unlink()
    pos_store = PositionStore()
    pos_store.save("BTC", {"equity": 207.4, "position": None})
    legacy = pos_store.load("BTC").get("equity")
    led2 = AccountLedger(cfg, store, legacy_equity=legacy)
    assert abs(led2.equity - 207.4) < 1e-9, led2.equity

    # 3) 레코드가 생긴 뒤에는 레거시보다 레코드가 항상 우선
    led2.set(255.5)
    led3 = AccountLedger(cfg, store, legacy_equity=legacy)
    assert abs(led3.equity - 255.5) < 1e-9, led3.equity
    print("ok  account persistence + one-shot legacy migration (record wins)")


def test_standalone_fallback():
    """No ledger passed (backtests, unit tests) -> a private in-memory pool;
    nothing leaks between managers or onto disk."""
    cfg = BybitConfig()
    risk = BybitRiskManager(cfg, Journal(), BotState())
    pm = PositionManager(SYMBOL_SPECS["BTC"], cfg, risk, Journal(), "paper", "t")
    pm.equity = 300.0
    pm2 = PositionManager(SYMBOL_SPECS["BTC"], cfg, risk, Journal(), "paper", "t")
    assert pm2.equity == cfg.equity_usd
    print("ok  standalone fallback (private non-persistent pool)")


def test_manager_wiring():
    """BybitManager must expose the unified account and hand every SymbolBot
    the same ledger instance."""
    from app.trading_bybit.bot import BybitManager
    mgr = BybitManager(BybitConfig())
    assert all(b.ledger is mgr.ledger for b in mgr.bots.values())
    st = mgr.status()
    acct = st["account"]
    assert abs(acct["equity_usd"] - mgr.ledger.equity) < 1e-9
    assert acct["start_equity_usd"] == mgr.cfg.equity_usd
    assert acct["unrealized_usd"] is None          # 열린 포지션 없음
    assert abs(acct["mark_value_usd"] - acct["equity_usd"]) < 1e-9
    assert "trades" in acct["aggregate"]           # 전 심볼 실현 집계
    print("ok  manager wiring (one ledger for all bots, account view in status)")


if __name__ == "__main__":
    test_shared_equity_pool()
    test_persistence_and_legacy_migration()
    test_standalone_fallback()
    test_manager_wiring()
    print("\nall unified-account tests passed ✅")
