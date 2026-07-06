"""@responsibility Bybit 트레이딩 설정 SSOT — BYBIT_* env 오버라이드 + 심볼별 SymbolSpec

Bybit leverage-margin trading configuration (Part 5). Every field can be
overridden with a BYBIT_* environment variable (Railway variables),
e.g. BYBIT_RISK_PER_TRADE_PCT=0.5.

Defaults encode the strategy source's risk discipline: leverage <= 5x,
fixed fractional risk per trade, 2:1 reward:risk, ATR-based stops. These
are conservative guesses meant to be calibrated by the Phase 2 backtest
gate before any live trading — do NOT hand-tune them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, fields


def _env(name: str, default):
    raw = os.getenv(f"BYBIT_{name.upper()}")
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return raw


@dataclass
class BybitConfig:
    # --- account (paper simulation) ----------------------------------------
    equity_usd: float = 200.0            # 소액: paper starting equity
    quote: str = "USDT"

    # --- timeframes (Bybit kline interval strings, minutes) ----------------
    entry_interval: str = "15"           # 진입 시간봉
    htf_interval: str = "60"             # 상위 추세 시간봉 (System #3 조건 1)
    warmup_bars: int = 200               # REST backfill on start

    # --- System #1: 5-period MA trend filter -------------------------------
    ema_period: int = 5

    # --- System #3: box breakout ------------------------------------------
    box_lookback: int = 20               # HTF bars that define the range
    breakout_buffer_pct: float = 0.0     # require close this % beyond the box

    # --- 근거: engulfing + volume ------------------------------------------
    require_engulfing: bool = True       # 장악형 캔들 요구
    require_volume: bool = True          # 거래량 동반 요구
    vol_ma_period: int = 20              # entry candle volume must exceed MA*k
    vol_mult: float = 1.2

    # --- System #2: risk management (ATR sizing) ---------------------------
    atr_period: int = 14
    atr_stop_mult: float = 1.5           # hard stop = k * ATR from entry
    risk_per_trade_pct: float = 1.0      # 총자산 대비 1회 리스크 (총자산대비!)
    leverage: float = 3.0                # target leverage
    leverage_max: float = 5.0            # HARD cap (source: 3~5x, never more)

    # --- exit: 2:1 R:R + partial + trailing --------------------------------
    rr_target: float = 2.0               # first take-profit at 2R
    partial_tp_frac: float = 0.5         # exit this fraction of size at TP1
    breakeven_after_tp: bool = True      # move stop to entry after partial
    trail_atr_mult: float = 2.0          # trail remainder by k * ATR
    time_stop_bars: int = 0              # 0 = off; else force-exit after N bars

    # --- 애드업 / pyramiding ------------------------------------------------
    pyramid_enabled: bool = True
    pyramid_max_adds: int = 2            # never add more than this many units
    pyramid_min_r: float = 1.0           # only add once price is >= this R ahead

    # --- 부가지표: session filter (오전매수/오후매도, KST) — off by default -
    session_filter: bool = False
    session_start_kst: int = 0           # allow entries from this KST hour
    session_end_kst: int = 24            # ...until this KST hour (exclusive)

    # --- fees (Bybit USDT perp taker; makers rebate, we assume taker) -------
    taker_fee_frac: float = 0.00055      # 0.055% of notional per side

    # --- risk caps (GLOBAL across symbols) ---------------------------------
    max_trades_per_day: int = 20
    daily_loss_cap_pct: float = 6.0      # halt entries at -6% of equity/day
    max_concurrent_positions: int = 1    # source: one clean trade at a time
    max_total_open_risk_pct: float = 2.0 # sum of open-position risk cap (adds!)
    max_consecutive_errors: int = 5

    # --- poll / data -------------------------------------------------------
    poll_sec: float = 2.0

    # --- mode (Paper-First) ------------------------------------------------
    live_enabled: bool = False           # Phase 3: BYBIT_LIVE_ENABLED=1 + creds
    testnet: bool = True                 # when live lands, default to testnet

    def __post_init__(self):
        for f in fields(self):
            setattr(self, f.name, _env(f.name, getattr(self, f.name)))
        # invariant: leverage can never exceed the hard cap, whatever env says
        self.leverage = min(self.leverage, self.leverage_max)

    def as_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


CONFIG = BybitConfig()


# --------------------------------------------------------------- symbols

@dataclass(frozen=True)
class SymbolSpec:
    """Per-symbol wiring for Bybit linear (USDT perpetual) markets."""
    key: str                    # display key, e.g. "BTC"
    symbol: str                 # Bybit symbol, e.g. "BTCUSDT"
    qty_step: float             # base-asset quantity rounding step
    min_qty: float              # exchange minimum order quantity
    tick_size: float            # price rounding step


SYMBOL_SPECS: dict[str, SymbolSpec] = {
    "BTC": SymbolSpec("BTC", "BTCUSDT", qty_step=0.001, min_qty=0.001, tick_size=0.1),
    "ETH": SymbolSpec("ETH", "ETHUSDT", qty_step=0.01, min_qty=0.01, tick_size=0.01),
    "SOL": SymbolSpec("SOL", "SOLUSDT", qty_step=0.1, min_qty=0.1, tick_size=0.001),
}

# Phase 1 default: BTC only (verify the logic on one symbol, then widen with
# BYBIT_SYMBOLS=BTC,ETH). Kept intentionally narrow — one clean trade at a time.
DEFAULT_SYMBOLS = "BTC"


def enabled_symbols() -> list[SymbolSpec]:
    raw = os.getenv("BYBIT_SYMBOLS", DEFAULT_SYMBOLS)
    out = []
    for k in raw.split(","):
        k = k.strip().upper()
        if k in SYMBOL_SPECS:
            out.append(SYMBOL_SPECS[k])
    return out or [SYMBOL_SPECS["BTC"]]
