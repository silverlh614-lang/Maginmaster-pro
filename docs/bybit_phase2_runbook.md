# Bybit Part 5 — Phase 2 백테스트 게이트 실행 런북 (Railway 기준)

> 목적: Railway에 배포한 앱에서 페이퍼 봇을 띄우고 **백테스트 게이트**를 돌려 전략이
> +EV인지 확인한다. 이 게이트를 통과해야 Phase 3(실주문) 작성이 허용된다
> (Paper-First 불변식). 실주문에는 이 단계가 **선행 조건**이다.

관련 코드: `app/trading_bybit/` · UI `/bybit` · API `/api/bybit/*` · 빌드 `Dockerfile`+`railway.json`

---

## 0. ⚠️ 리전(Region) — 가장 먼저 확인 (가장 흔한 실패 원인)

**Bybit은 미국 등 일부 지역 IP를 차단한다.** Railway 기본 배포 리전은 **US(Oregon/Virginia)** 라,
그대로 두면 Bybit에 `403`/blocked가 난다.

**자동 폴백(v1.1+):** 데이터 피드는 이제 **Bybit → Binance(USDⓈ-M) → OKX** 순으로 소스를
자동 전환한다. 그리고 kline 피드는 **봇 START와 무관하게 앱 부팅 시 상시 가동**되므로,
페이지를 열면 봇이 정지 상태여도 라이브 차트에 캔들이 바로 뜬다. Bybit이 막힌 리전에서도
Binance/OKX가 뚫리면 차트·지표는 정상 동작한다(데이터 피드 소스가 `binance`/`okx`로 표시).

→ 그래도 **실거래(Phase 3)는 Bybit 직결이 전제**이므로, 리전은 Bybit 허용 지역을 권장한다.
- Service → **Settings → Region** → `Southeast Asia (Singapore)` 또는 `EU (Amsterdam)` → **Redeploy**
- 확인법: `/bybit`에서 데이터 피드 **소스가 `bybit`** 로 뜨고 "에러: 없음"이면 OK.
  `binance`/`okx`로 떠 있으면 Bybit이 차단된 것 — 차트는 살아 있으나 리전 교체 권장.

> 세 소스가 모두 막히면 차트에 "⚠ kline 소스 연결 실패"와 에러 원문이 뜬다.
> 그 경우 로컬(한국 등 허용 지역)에서 백테스트만 먼저 돌리는 방법(부록 A)을 쓴다.

---

## 1. 배포

1. Railway → **New Project → Deploy from GitHub repo** → `silverlh614-lang/coinmaster-pro`
2. 브랜치 **main** 선택 (Part 5 병합 완료). `railway.json`이 Dockerfile 빌드를 자동 지정한다.
3. 첫 빌드 완료 후 **Settings → Networking → Generate Domain** 으로 공개 URL 생성
   (`https://<앱>.up.railway.app`).
4. 헬스체크 `/healthz`는 자동(`railway.json`). 포트는 Railway가 주입하는 `PORT`를 Dockerfile이 사용.

## 2. 리전 설정
위 **0번** 대로 Service → Settings → Region → Singapore(or EU) → Redeploy.

## 3. 환경변수 (Variables 탭)

Bybit 페이퍼/백테스트는 **API 키가 필요 없다.** 최소 설정:

| 변수 | 값(예) | 의미 |
|---|---|---|
| `DATA_DIR` | `/data` | 상태·저널 저장 경로(아래 Volume과 함께) |

> **Bybit 전용 앱**: 이 repo는 Bybit 레버리지 봇만 활성화되어 있다. 랜딩(`/`)은 항상
> 레버리지 관제탑이며 BTC 사이클 바닥 모델(`/model`)이 함께 서빙된다.

선택 튜닝(전부 기본값 있음, `BYBIT_*`):

| 변수 | 기본 | 의미 |
|---|---|---|
| `BYBIT_SYMBOLS` | `BTC` | 대상 심볼 (예: `BTC,ETH`) |
| `BYBIT_EQUITY_USD` | `200` | 페이퍼 시작 자산(소액) |
| `BYBIT_ENTRY_INTERVAL` | `15` | 진입 시간봉(분) |
| `BYBIT_HTF_INTERVAL` | `60` | 상위 추세 시간봉(분) |
| `BYBIT_RISK_PER_TRADE_PCT` | `1.0` | 1회 리스크(총자산 %) |
| `BYBIT_LEVERAGE` / `BYBIT_LEVERAGE_MAX` | `3` / `5` | 목표/상한 레버리지 |
| `BYBIT_RR_TARGET` | `2.0` | 손익비 R:R |
| `BYBIT_ATR_STOP_MULT` | `1.5` | ATR 손절 배수 |

## 4. Volume (상태 영속성 — ⚠️ 필수)

Service → **Volumes → New Volume** → Mount path `/data` (`DATA_DIR` 기본값과 일치).

**볼륨을 붙여야만 재배포/재시작에도 다음이 전부 유지된다:**
- 봇 **START/정지 상태** → START로 두면 재배포 시 **auto-resume**(자동 재가동)
- **복리 EQUITY** (실현손익이 누적된 계좌 잔고 — `bybit_positions.json`)
- **진행 중인 오픈 포지션** (진입가·수량·손절·트레일·부분익절 상태까지 그대로 복원)
- **트레이드 저널** (`bybit_trades.csv` — 승률·R기대값·PnL)
- **킬스위치·일일 카운터** (`bybit_state.json`)

> ⚠️ 볼륨을 안 붙이면 `/data`는 **매 배포마다 초기화**된다 — START도, 계좌($200으로 리셋)도,
> 열린 포지션도 전부 사라진다. "재배포돼도 계속 기억"하려면 볼륨이 반드시 필요하다.
> 봇은 매 캔들마다 상태를 볼륨에 스냅샷하고, START 시 그 스냅샷에서 이어받는다.

## 5. 페이퍼 봇 START & 확인

`https://<앱>.up.railway.app/bybit` 접속 → **▶ START**.
- **데이터 피드** 소스 `bybit_rest`, 1~2분 내 캔들·5EMA·게이트 채워짐 → 리전 정상.
- 게이트 4개가 다 켜지면 자동 진입 → 포지션·R-ladder 표시. **실주문 없음(페이퍼).**
- 로그 확인: Railway **Deployments → View Logs**.

## 6. 백테스트 게이트 실행 (핵심)

**방법 ⓐ UI**: `/bybit` → **백테스트 게이트(PHASE 2)** 패널 → `ATR×`·`R:R` 입력 → **▶ 리플레이**.

**방법 ⓑ curl (공개 URL)**:
```bash
APP=https://<앱>.up.railway.app
curl -s -X POST $APP/api/bybit/backtest \
  -H "Content-Type: application/json" \
  -d '{"symbol":"BTC","strategy":"trend_breakout","overrides":{}}' | python -m json.tool
```

**방법 ⓒ 파라미터 스윕**:
```bash
APP=https://<앱>.up.railway.app
for atr in 1.0 1.5 2.0 2.5; do
  curl -s -X POST $APP/api/bybit/backtest -H "Content-Type: application/json" \
    -d "{\"symbol\":\"BTC\",\"overrides\":{\"atr_stop_mult\":$atr}}" \
  | python -c "import sys,json;d=json.load(sys.stdin);m=d['metrics'];print(f\"ATR={$atr} trades={m['trades']} winR={m['win_rate']} expR={m['expectancy_r']} PF={m['profit_factor']} MDD={m['max_drawdown_usd']}\")"
done
```
바꿀 knob: `atr_stop_mult`, `rr_target`, `box_lookback`, `vol_mult`, `breakout_buffer_pct`,
`ema_period`, `pyramid_max_adds`.

## 7. 지표 해석 & 통과 기준

| 필드 | 의미 | 기준 |
|---|---|---|
| `trades` | 청산 포지션 수(표본) | **≥ 20~30** 이어야 신뢰 |
| `expectancy_r` | **거래당 평균 R (핵심 KPI)** | **> 0** 필수 |
| `profit_factor` | 총이익/총손실 | **≥ 1.2** 권장 |
| `win_rate` | 승률 | 참고(추세추종은 낮아도 됨) |
| `return_pct` | 시작자산 대비 수익률 | 방향 참고 |
| `max_drawdown_usd` | 최대 낙폭 | 감내 범위인지 |
| `z_score` | 견고성(기대값/σ×√n) | 클수록 좋음 |

**통과 = `expectancy_r>0` AND `PF≥1.2` AND `trades≥20` AND MDD 감내 가능.**
손으로 임계값을 맞추지 말고, 여러 파라미터에서 **반복 재현되는** 조합만 채택(불변식).

> ⚠️ **히스토리 한계**: Phase 1 백테스트는 호출 시 Bybit 공개 kline을 실시간으로 받는다.
> 공개 API는 요청당 **최대 1000봉** → 15m ≈ **10일**, 1h ≈ **41일**. 표본이 짧으면 방향성
> 판단용. `trades`가 한 자릿수면 아직 게이트 판정 불가 → 페이퍼를 오래 돌려 캔들 아카이브를
> 쌓는 확장이 다음 과제.

## 8. 결과 공유

`/api/bybit/backtest` **JSON 응답 전체** 또는 스윕 **한 줄 요약들**을 복사해 전달.
받으면 근거를 커밋에 남겨(불변식) 파라미터를 조정하고, +EV가 재현되면 Phase 3로 진행.

## 9. 다음 — Phase 3 (백테스트 통과 후에만)

Railway **Variables**에 추가:
- `BYBIT_TESTNET=1`, `BYBIT_API_KEY`, `BYBIT_API_SECRET` (테스트넷 키; 리포 커밋 금지)
그 후 `bybit_client.py`(v5 서명·주문·레버리지·트레이딩스톱)를 `live_enabled` 게이트 뒤에 작성,
테스트넷 검증 → `BYBIT_LIVE_ENABLED=1` + 소액 라이브. 리스크 단일 관문은 라이브에서도 유지.

> 키 보안: 파생상품 거래 권한만, **출금 권한 끄기**, IP 화이트리스트(Railway egress IP),
> 환경변수로만.

---

## 부록 A — 로컬에서 백테스트만 (Railway 리전이 Bybit 차단일 때)

Bybit 허용 지역(한국 등)의 로컬에서:
```bash
git clone https://github.com/silverlh614-lang/coinmaster-pro.git && cd coinmaster-pro
git checkout main
python -m venv .venv && source .venv/bin/activate   # Win: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.main:app --port 8000
# → http://localhost:8000/bybit 에서 리플레이, 또는 curl로 STEP 6 동일
```
