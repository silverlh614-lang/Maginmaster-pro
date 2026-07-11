"""Offline tests for the Bybit v5 client skeleton — signing, read-only calls
(MockTransport), and the Phase 3 order-path lock. No real network.
Run:  python -m tests.test_bybit_client"""
from __future__ import annotations

from tests.test_bybit import _c                            # noqa: F401  (DATA_DIR isolation)

from app.trading_bybit.bybit_client import (BybitClient, LiveTradingLocked,  # noqa: E402
                                            MAINNET_REST, TESTNET_REST, sign)
from app.trading_bybit.config import BybitConfig           # noqa: E402


def test_signature_vector():
    """v5 서명 규칙(timestamp+key+recv+payload, HMAC-SHA256 hex) 고정 벡터."""
    s = sign("test-secret", "1700000000000" + "test-key" + "5000"
             + "accountType=UNIFIED")
    assert s == "3f10586267639c9f3f4f5e32e491a6ef80d157db06f51eb79e4988e24f97adba", s
    print("ok  v5 signature vector")


def test_base_url_and_keys():
    cfg = BybitConfig()
    assert cfg.testnet                                     # 기본은 테스트넷
    c = BybitClient(cfg, api_key="", api_secret="")
    assert c.base == TESTNET_REST and not c.keys_configured
    cfg2 = BybitConfig()
    cfg2.testnet = False
    c2 = BybitClient(cfg2, api_key="k", api_secret="s")
    assert c2.base == MAINNET_REST and c2.keys_configured
    print("ok  testnet/mainnet switch + keys_configured")


def test_read_only_calls_and_auth_headers():
    import httpx
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = dict(request.headers)
        if request.url.path == "/v5/market/time":
            return httpx.Response(200, json={"retCode": 0, "result":
                                             {"timeSecond": "1700000000"}})
        if request.url.path == "/v5/account/wallet-balance":
            return httpx.Response(200, json={"retCode": 0, "result": {"list": [
                {"totalEquity": "207.4", "totalAvailableBalance": "180.1"}]}})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler),
                          base_url="https://api-testnet.bybit.com")
    c = BybitClient(BybitConfig(), api_key="test-key", api_secret="test-secret")
    assert c.server_time(client=client) == 1700000000.0
    assert "x-bapi-sign" not in seen["/v5/market/time"]     # 공개 호출은 무서명
    w = c.wallet_balance(client=client)
    assert w == {"total_equity_usd": 207.4, "available_usd": 180.1}, w
    h = seen["/v5/account/wallet-balance"]
    assert h["x-bapi-api-key"] == "test-key" and h["x-bapi-sign"], h
    print("ok  read-only calls (public unsigned, wallet signed + parsed)")


def test_wallet_requires_keys():
    c = BybitClient(BybitConfig(), api_key="", api_secret="")
    try:
        c.wallet_balance()
        assert False, "should have raised"
    except RuntimeError as e:
        assert "not configured" in str(e)
    print("ok  wallet without keys → clear error (no network attempted)")


def test_order_path_locked():
    """키·live_enabled 가 있어도 주문 메서드는 잠금 — 불변식 1."""
    cfg = BybitConfig()
    cfg.live_enabled = True
    c = BybitClient(cfg, api_key="k", api_secret="s")
    for m in (c.place_order, c.cancel_all, c.set_leverage, c.set_trading_stop):
        try:
            m()
            assert False, f"{m.__name__} should be locked"
        except LiveTradingLocked as e:
            assert "게이트" in str(e)
    print("ok  order path hard-locked (even with keys + live_enabled)")


if __name__ == "__main__":
    test_signature_vector()
    test_base_url_and_keys()
    test_read_only_calls_and_auth_headers()
    test_wallet_requires_keys()
    test_order_path_locked()
    print("\nall bybit-client skeleton tests passed ✅")
