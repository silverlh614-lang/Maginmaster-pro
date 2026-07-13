"""@responsibility Bybit 트레이딩 오케스트레이션 — SymbolBot 수명주기 + BybitManager 전역 리스크·저널 공유, 페이퍼 전용

Bybit trading orchestration (Part 5).

SymbolBot     — one symbol (BTCUSDT ...): its own kline collector, position
                manager and closed-bar decision loop.
BybitManager  — starts/stops all enabled SymbolBots together; shares one
                journal, one risk manager (global daily caps / kill switch)
                and one persisted state blob (auto-resume).

Phase 1: mode is always "paper" — live mode is refused until the Phase 3
gate lands (mirrors Part 4's TradingManager). No real order path exists yet.
"""
from __future__ import annotations

import asyncio
import time

from .account import AccountLedger
from .config import (CONFIG, SYMBOL_SPECS, BybitConfig, SymbolSpec,
                     enabled_symbols)
from .collectors.kline import KlineCollector
from .execution.position import PositionManager
from .indicators import atr, ema
from .models import Side
from .risk import BybitRiskManager
from .store import AccountStore, BotState, Journal, PositionStore
from .strategies import STRATEGIES, make_strategy
from .strategies.base import BybitContext


class SymbolBot:
    def __init__(self, spec: SymbolSpec, cfg: BybitConfig,
                 journal: Journal, risk: BybitRiskManager,
                 pos_store: PositionStore, ledger: AccountLedger):
        self.spec = spec
        self.cfg = cfg
        self.journal = journal
        self.risk = risk
        self.pos_store = pos_store
        self.ledger = ledger
        self.collector = KlineCollector(spec.symbol, cfg.entry_interval,
                                        cfg.htf_interval, cfg.warmup_bars,
                                        testnet=cfg.testnet and cfg.live_enabled)
        self.mode = "paper"
        self.strategy_name = "trend_breakout"
        self.strategy = None
        self.pm: PositionManager | None = None
        self.running = False
        self.note = "stopped"
        self._last_bar_ts: int | None = None
        self._last_pos_state: dict | None = None
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        # The kline feed runs independent of trading so the live chart warms up
        # even while the bot is stopped.
        self._feed_stop = asyncio.Event()
        self._feed_task: asyncio.Task | None = None

    def start_feed(self) -> None:
        """Start the market-data feed (idempotent). Warms candles regardless of
        whether the trading loop is running."""
        if self._feed_task is not None and not self._feed_task.done():
            return
        self._feed_stop = asyncio.Event()
        self._feed_task = asyncio.create_task(
            self.collector.run(self._feed_stop, self.cfg.poll_sec),
            name=f"kline-{self.spec.key}")

    async def stop_feed(self) -> None:
        if self._feed_task is None:
            return
        self._feed_stop.set()
        self._feed_task.cancel()
        await asyncio.gather(self._feed_task, return_exceptions=True)
        self._feed_task = None

    async def start(self, mode: str, strategy: str) -> None:
        if self.running:
            return
        self.mode = mode
        self.strategy_name = strategy
        self.strategy = make_strategy(strategy, self.cfg)
        self.pm = PositionManager(self.spec, self.cfg, self.risk, self.journal,
                                  mode, strategy, ledger=self.ledger)
        # Restore any live position from the last run so a trade in progress
        # survives a restart / redeploy (equity restores via the ledger).
        self.pm.load_state(self.pos_store.load(self.spec.key))
        self._last_pos_state = self.pm.to_state()
        self._stop = asyncio.Event()
        self._last_bar_ts = None
        self.running = True
        self.note = "starting"
        self.start_feed()                       # ensure market data is flowing
        self._tasks = [
            asyncio.create_task(self._decision_loop(),
                                name=f"loop-{self.spec.key}"),
        ]

    def _persist_pos(self) -> None:
        """Snapshot the position/equity after a change so it survives restarts."""
        if self.pm is None:
            return
        st = self.pm.to_state()
        if st != self._last_pos_state:
            self.pos_store.save(self.spec.key, st)
            self._last_pos_state = st

    async def shutdown(self) -> None:
        # Stop only the trading loop; the feed keeps the chart live.
        if not self.running:
            return
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self.running = False
        self.note = "stopped"

    def manual(self, action: str) -> dict:
        """Control-tower close button (paper). Entries stay strategy-driven —
        the source's discipline is to follow the system, not hand-trade."""
        if not self.pm:
            return {"ok": False, "error": f"no {self.spec.key} session"}
        if action == "close":
            p = self.pm.pos
            px = self.collector.last_price()
            if not p or px is None:
                return {"ok": False, "error": "no open position"}
            self.pm._close(px, "manual close", time.time())
            self._persist_pos()
            return {"ok": True, "pnl_usd": p.realized_pnl_usd}
        return {"ok": False, "error": f"unknown action {action}"}

    # ---------------------------------------------------------------- loop

    async def _decision_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._step(self.strategy)
                self.risk.record_ok()
                self._persist_pos()             # survive restarts/redeploys
            except Exception as e:
                self.note = f"loop error: {type(e).__name__}: {e}"
                self.risk.record_error(f"{self.spec.key}: {e}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.cfg.poll_sec)
            except asyncio.TimeoutError:
                pass

    def _in_session(self, now: float) -> bool:
        if not self.cfg.session_filter:
            return True
        kst_hour = time.gmtime(now + 9 * 3600).tm_hour
        return self.cfg.session_start_kst <= kst_hour < self.cfg.session_end_kst

    def _step(self, strategy) -> None:
        """Act once per newly-closed entry candle: manage the open position
        against the bar, then let the strategy open or pyramid."""
        assert self.pm is not None
        entry = self.collector.entry_closed()
        if not entry:
            self.note = f"warming up ({self.collector.status()['entry_bars']} bars)"
            return
        bar = entry[-1]
        if bar.ts_ms == self._last_bar_ts:
            self.note = self.pm.note      # no new bar; keep managing view fresh
            return
        self._last_bar_ts = bar.ts_ms
        atr_val = atr(entry, self.cfg.atr_period)

        self.pm.flatten_if_closed()
        self.pm.manage(bar, atr_val)

        ctx = self._build_ctx(entry)
        sig = strategy.evaluate(ctx)
        if sig is None:
            self.note = self.pm.note if self.pm.pos else "watching — no signal"
            return
        if not self._in_session(ctx.now):
            self.note = f"signal {sig.side.value} out of session window"
            return
        price = bar.close
        p = self.pm.pos
        if p and p.state.value == "OPEN" and sig.side is p.side:
            self.pm.try_add(sig, price, atr_val or 0.0, ctx.now)
        elif not (p and p.state.value == "OPEN"):
            self.pm.try_open(sig, price, atr_val or 0.0, ctx.now)
        self.note = self.pm.note

    def _build_ctx(self, entry: list) -> BybitContext:
        return BybitContext(
            symbol=self.spec.key,
            htf_candles=self.collector.htf_closed(),
            entry_candles=entry,
            equity_usd=self.pm.equity if self.pm else self.cfg.equity_usd,
            now=time.time(),
            open_position_side=(self.pm.pos.side.value
                                if self.pm and self.pm.pos
                                and self.pm.pos.state.value == "OPEN" else None),
        )

    # ---------------------------------------------------------------- view

    def diagnostics(self) -> dict | None:
        """Live entry-gate snapshot for the dashboard gauge (real data)."""
        entry = self.collector.entry_closed()
        if not entry or self.strategy is None:
            return None
        try:
            return self.strategy.diagnose(self._build_ctx(entry))
        except Exception:
            return None

    def candles(self, tf: str = "entry", limit: int = 120) -> dict:
        cs = (self.collector.htf_closed() if tf == "htf"
              else self.collector.entry_closed())[-limit:]
        closes = [c.close for c in cs]
        ema5 = ema(closes, self.cfg.ema_period) if closes else []
        return {"symbol": self.spec.key, "tf": tf,
                "interval": self.cfg.htf_interval if tf == "htf" else self.cfg.entry_interval,
                "candles": [[c.ts_ms, c.open, c.high, c.low, c.close, c.volume] for c in cs],
                "ema5": [round(x, 6) for x in ema5]}

    def status(self) -> dict:
        px = self.collector.last_price()
        return {
            "symbol": self.spec.key,
            "strategy": self.strategy_name,
            "running": self.running,
            "note": self.note,
            "collector": self.collector.status(),
            "manager": self.pm.snapshot(px) if self.pm else None,
            "diagnostics": self.diagnostics(),
        }


class BybitManager:
    def __init__(self, cfg: BybitConfig = CONFIG):
        self.cfg = cfg
        self.journal = Journal()
        self.state_store = BotState()
        self.pos_store = PositionStore()
        # 한 계좌 원칙: 전 심볼이 이 원장 하나에서 돈이 나간다. 계좌 레코드가
        # 아직 없으면 운영 심볼(BTC)의 레거시 심볼별 equity 를 1회 승계한다.
        self.ledger = AccountLedger(cfg, AccountStore(),
                                    legacy_equity=self.pos_store.load("BTC")
                                    .get("equity"))
        self.risk = BybitRiskManager(cfg, self.journal, self.state_store)
        self.bots: dict[str, SymbolBot] = {
            spec.key: SymbolBot(spec, cfg, self.journal, self.risk,
                                self.pos_store, self.ledger)
            for spec in enabled_symbols()
        }
        self.mode = "paper"
        self.strategy_name = "trend_breakout"

    @property
    def running(self) -> bool:
        return any(b.running for b in self.bots.values())

    async def start(self, mode: str = "paper",
                    strategy: str = "trend_breakout") -> dict:
        if self.running:
            return {"ok": False, "error": "already running"}
        if strategy not in STRATEGIES:
            return {"ok": False, "error": f"unknown strategy '{strategy}'"}
        if mode != "paper":
            return {"ok": False,
                    "error": "live mode is Phase 3 — only 'paper' is available"}
        self.mode = mode
        self.strategy_name = strategy
        for b in self.bots.values():
            await b.start(mode, strategy)
        st = self.state_store.load()
        st.update({"mode": mode, "strategy": strategy, "running": True})
        self.state_store.save(st)
        return {"ok": True, "mode": mode, "strategy": strategy,
                "symbols": list(self.bots)}

    def start_feeds(self) -> None:
        """Warm the live chart for every symbol without starting trading."""
        for b in self.bots.values():
            b.start_feed()

    async def stop_feeds(self) -> None:
        for b in self.bots.values():
            await b.stop_feed()

    async def shutdown(self) -> None:
        for b in self.bots.values():
            await b.shutdown()

    async def stop(self) -> dict:
        if not self.running:
            return {"ok": False, "error": "not running"}
        await self.shutdown()
        st = self.state_store.load()
        st["running"] = False
        self.state_store.save(st)
        return {"ok": True}

    def manual(self, action: str, symbol: str = "BTC") -> dict:
        bot = self.bots.get(symbol.upper())
        if bot is None:
            return {"ok": False, "error": f"symbol '{symbol}' not enabled"}
        return bot.manual(action)

    def candles(self, symbol: str, tf: str = "entry", limit: int = 120) -> dict:
        bot = self.bots.get(symbol.upper())
        if bot is None:
            return {"error": f"symbol '{symbol}' not enabled"}
        return bot.candles(tf, limit)

    def _account_view(self) -> dict:
        """한 계좌 관점의 스냅샷 — 전 심볼 열린 포지션의 미실현 합계와 전체
        실현 집계. 대시보드 ACCOUNT 패널은 탭과 무관하게 이것만 본다."""
        unreal = 0.0
        open_any = False
        for b in self.bots.values():
            pm = b.pm
            if pm and pm.pos and pm.pos.state.value == "OPEN":
                px = b.collector.last_price()
                if px is not None:
                    unreal += pm.pos.unrealized_usd(px)
                    open_any = True
        eq = round(self.ledger.equity, 4)
        return {
            "equity_usd": eq,
            "start_equity_usd": self.cfg.equity_usd,
            "unrealized_usd": round(unreal, 4) if open_any else None,
            "mark_value_usd": round(eq + unreal, 4),
            "aggregate": self.journal.aggregate(),
        }

    def status(self) -> dict:
        return {
            "running": self.running,
            "mode": self.mode,
            "strategy": self.strategy_name,
            "account": self._account_view(),
            "note": ("running: " + ",".join(self.bots)) if self.running else "stopped",
            "risk": self.risk.status(),
            "symbols": {k: b.status() for k, b in self.bots.items()},
            "by_symbol": self.journal.by_symbol(list(self.bots)),
            "server_time": time.time(),
        }


MANAGER = BybitManager()
