"""@responsibility Bybit v5 인증 클라이언트 뼈대 — 서명·읽기전용 조회만, 주문 메서드는 Phase 3 게이트 하드 잠금

Bybit v5 authenticated REST client (Phase 3 skeleton). What exists NOW:

  - HMAC-SHA256 request signing (the hard part of live wiring)
  - testnet/mainnet base-URL switch (cfg.testnet)
  - READ-ONLY calls: server_time (connectivity), wallet_balance, positions

What deliberately does NOT exist: order transmission. Every write method
raises LiveTradingLocked — CLAUDE.md 불변식 1(Paper-First)은 백테스트
게이트 통과 전 실주문 경로 작성을 금지한다. 게이트 통과 후 Phase 3에서
이 잠금을 풀고 주문 바디를 채운다 (runbook §9 순서). 이 잠금을
우회·완화하는 패치는 금지.

Keys come ONLY from env (BYBIT_API_KEY / BYBIT_API_SECRET) and are never
logged or echoed back by any method here.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from urllib.parse import urlencode

TESTNET_REST = "https://api-testnet.bybit.com"
MAINNET_REST = "https://api.bybit.com"
RECV_WINDOW = "5000"


class LiveTradingLocked(RuntimeError):
    """실주문 경로 잠금 — 백테스트 게이트 통과 + Phase 3 구현 전에는 열리지 않는다."""


def sign(api_secret: str, param_str: str) -> str:
    """v5 서명: HMAC-SHA256(secret, timestamp + api_key + recv_window + payload)."""
    return hmac.new(api_secret.encode(), param_str.encode(),
                    hashlib.sha256).hexdigest()


class BybitClient:
    def __init__(self, cfg, api_key: str | None = None,
                 api_secret: str | None = None):
        self.cfg = cfg
        self.api_key = api_key if api_key is not None else os.getenv("BYBIT_API_KEY", "")
        self.api_secret = (api_secret if api_secret is not None
                           else os.getenv("BYBIT_API_SECRET", ""))
        self.base = TESTNET_REST if cfg.testnet else MAINNET_REST

    @property
    def keys_configured(self) -> bool:
        return bool(self.api_key and self.api_secret)

    # ------------------------------------------------------------ transport

    def _auth_headers(self, payload: str) -> dict:
        ts = str(int(time.time() * 1000))
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": RECV_WINDOW,
            "X-BAPI-SIGN": sign(self.api_secret,
                                ts + self.api_key + RECV_WINDOW + payload),
        }

    def _get(self, path: str, params: dict | None = None,
             auth: bool = False, client=None) -> dict:
        """GET with optional v5 auth. `client` 주입은 오프라인 테스트용."""
        import httpx    # lazy: offline 테스트에서 모듈 import 가능하게
        params = dict(sorted((params or {}).items()))
        query = urlencode(params)
        headers = {"User-Agent": "coinmaster-pro"}
        if auth:
            if not self.keys_configured:
                raise RuntimeError("BYBIT_API_KEY / BYBIT_API_SECRET not configured")
            headers.update(self._auth_headers(query))
        own = client is None
        c = client or httpx.Client(timeout=10)
        try:
            r = c.get(self.base + path, params=params, headers=headers)
            r.raise_for_status()
            data = r.json()
        finally:
            if own:
                c.close()
        if data.get("retCode") != 0:
            raise ValueError(f"bybit retCode {data.get('retCode')}: {data.get('retMsg')}")
        return data.get("result", {})

    # ------------------------------------------------------- read-only calls

    def server_time(self, client=None) -> float | None:
        """공개 연결 확인 — 인증 없이 거래소 시간을 받아오면 리전 통과."""
        res = self._get("/v5/market/time", auth=False, client=client)
        return float(res.get("timeSecond", 0)) or None

    def wallet_balance(self, client=None) -> dict:
        """UNIFIED 지갑 요약 — Phase 3에서 AccountLedger 의 잔고 원천이 된다."""
        res = self._get("/v5/account/wallet-balance",
                        {"accountType": "UNIFIED"}, auth=True, client=client)
        acct = (res.get("list") or [{}])[0]
        return {"total_equity_usd": float(acct.get("totalEquity") or 0),
                "available_usd": float(acct.get("totalAvailableBalance") or 0)}

    def positions(self, symbol: str, client=None) -> list[dict]:
        res = self._get("/v5/position/list",
                        {"category": "linear", "symbol": symbol},
                        auth=True, client=client)
        return res.get("list", [])

    # ------------------------------------------- write path: Phase 3 잠금 --

    def _locked(self, what: str):
        raise LiveTradingLocked(
            f"{what}: 실주문 경로는 잠겨 있다 — 백테스트 게이트 통과 후 "
            "Phase 3 에서 구현·해제한다 (CLAUDE.md 불변식 1, runbook §9)")

    def place_order(self, *a, **k):
        self._locked("place_order")

    def cancel_all(self, *a, **k):
        self._locked("cancel_all")

    def set_leverage(self, *a, **k):
        self._locked("set_leverage")

    def set_trading_stop(self, *a, **k):
        self._locked("set_trading_stop")
