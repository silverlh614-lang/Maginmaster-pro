# Coinmaster Pro — BTC 사이클 바닥 모델 (Railway 앱)

비트코인 사이클·바닥 분석 지식 베이스와 몬테카를로 모델(`btc_bottom_model.py`)을
Railway 배포 가능한 FastAPI 서비스로 이식한 프로젝트입니다.
**Harness(팀 아키텍처 팩토리) 개념**을 적용해 `.claude/agents/` + `.claude/skills/`에
에이전트 팀과 오케스트레이터 스킬이 포함되어 있습니다.

> ⚠️ 모든 수치는 예측이 아닌 **구조화된 의견**이며, 어떤 항목도 투자 조언이 아닙니다.
> [SNAPSHOT] 값(실현가격·200주선 등)은 2026-07-03 기준으로 시간이 지나면 재검증이 필요합니다.

## 구성

| 파트 | 역할 | 실행 주기 |
|---|---|---|
| **Part 1 — 앙상블** (`app/ensemble.py`) | 6개 렌즈 삼각분포의 mixture-of-experts 몬테카를로 → 바닥 가격 분포 | 오프라인/주간 배치 (스냅샷 갱신 시 재계산) |
| **Part 2 — FSM** (`app/fsm.py`) | WATCH → CAP_WATCH → ACCUMULATE → CONFIRM → TREND 상태기계 → 일별 deploy fraction | 온라인/일별 (상태는 `data/fsm_state.json`에 영속) |
| **Part 3 — 풀사이클 체인** (`app/chain.py`) | 바닥 앙상블 → 회복 배수 → 2028 반감기 가격 → ROI regime 혼합(랠리 소멸 30%) → 2029 고점 가격·시점 | 오프라인 (파라미터 = 명시적 판단) |
| **Part 5 — Bybit 트레이더** (`app/trading_bybit/`) | 레버리지-마진 추세돌파 자동매매: kline 수집 → 상위추세+돌파 시그널 → ATR 스탑·2:1 R:R → 부분청산·트레일링 (Phase 1 = 페이퍼 전용. 레버리지 ≤ 5x, 고정 비율 리스크 — `BYBIT_*`로 조정) | 온라인/상시 (봇 start 시, 저널은 `data/bybit_trades.csv`) |
| **대시보드** (`static/index.html`) | `/model` — 스냅샷 앵커·분포 차트·P(바닥<레벨)·FSM 조작 UI | — |
| **관제탑** (`static/bybit.html`) | `/` — Bybit 레버리지 봇 상태·포지션·캔들, 수동 개입(L/S/X), 트레이드 저널 | — |
| **지식 베이스** (`knowledge/`) | 방법론·데이터·원칙 원문 (`/api/knowledge`로 서빙) | living document |

## API

| 엔드포인트 | 설명 |
|---|---|
| `GET /` | Bybit 트레이딩 관제탑 (랜딩) |
| `GET /model` | BTC 사이클 바닥 모델 대시보드 |
| `GET /healthz` | 헬스체크 (Railway healthcheckPath) |
| `GET /api/snapshot` | 온체인 스냅샷 앵커 |
| `GET /api/price` | **최신 현물가 자동 갱신** — CoinGecko → Coinbase → Binance 폴백, TTL 캐시, 전부 실패 시 스냅샷 값 (`live: false`) |
| `GET /api/simulate` | 기본 300k 시뮬레이션 요약 + 히스토그램 (캐시) |
| `POST /api/simulate` | 커스텀 렌즈/가중치로 민감도 실험 |
| `GET /api/distribution.png` | 분포 차트 (matplotlib 렌더) |
| `GET /api/chain` | 풀사이클 체인 기본 300k 시뮬레이션 (바닥/반감기/고점 percentile, 확률표, 고점 시점) |
| `POST /api/chain` | 커스텀 파라미터 (회복 배수, 랠리 소멸 확률, ROI 삼각분포, 렌즈) 실험 |
| `GET /api/chain.png` | 반감기·고점 분포 2패널 차트 |
| `GET /api/fsm/state` | FSM 현재 상태 |
| `POST /api/fsm/update` | 일별 온체인 입력 주입 → phase + deploy fraction |
| `POST /api/fsm/reset` | FSM 초기화 (`?ladder_tranches=5`) |
| `GET /api/fsm/demo` | 합성 하락→회복 경로 데모 (상태 비파괴) |
| `GET /api/knowledge` | 지식 베이스 마크다운 원문 |
| `GET /bybit` | Bybit 트레이딩 관제탑 UI (`/`와 동일) |
| `GET /api/bybit/status` | 봇 상태 (포지션 FSM·캔들·리스크·오늘 성과) |
| `POST /api/bybit/start` | 봇 시작 `{mode: "paper", strategy: "trend_breakout"}` |
| `POST /api/bybit/stop` | 봇 정지 (열린 페이퍼 포지션 청산) |
| `POST /api/bybit/manual` | 수동 개입 `{action: long\|short\|close, symbol}` |
| `POST /api/bybit/kill/reset` | 킬스위치 해제 |
| `GET /api/bybit/candles` | 캔들 조회 (`?symbol=BTC&tf=entry\|htf&limit=120`) |
| `GET /api/bybit/trades` | 트레이드 저널 + 승률/PnL 집계 (`?symbol=BTC`) |
| `GET /api/bybit/trades.csv` | 저널 엑셀(CSV) 다운로드 (`?symbol=BTC`) |
| `GET /api/bybit/config` | 트레이딩 설정 (환경변수 `BYBIT_*`로 오버라이드) |
| `POST /api/bybit/backtest` | 리플레이 백테스트 (전략·파라미터 스윕 → 승률/기대값/PF/Z/MDD 표) |
| `GET /docs` | OpenAPI 문서 |

> ⚠️ Part 5는 현재 **Phase 1 — 페이퍼 트레이딩 전용**입니다. 어떤 주문도 실제 거래소로
> 전송되지 않으며, live 모드는 Phase 3(백테스트 게이트 통과 후)에서 활성화됩니다.
> 설계·운영 문서: `docs/bybit_phase2_runbook.md`

## Railway 배포

1. 이 리포를 Railway 프로젝트에 연결하면 `railway.json`이 Dockerfile 빌드를 지정합니다.
2. 헬스체크는 `/healthz`, 포트는 Railway가 주입하는 `PORT`를 사용합니다.
3. **[SNAPSHOT] 값 갱신은 코드 수정 없이 환경변수로**:

   | 변수 | 기본값 | 의미 |
   |---|---|---|
   | `REALIZED_PRICE` | 53600 | 실현가격 (결정적 선) |
   | `MA_200W` | 61800 | 200주 이동평균 |
   | `SPOT` | 61500 | 현물가 |
   | `ATH` | 126198 | 사상 최고가 |
   | `SNAPSHOT_DATE` | 2026-07-03 | 스냅샷 기준일 |
   | `DATA_DIR` | `./data` | FSM 상태/차트/트레이딩 데이터 저장 경로 (볼륨 마운트 시 지정). 봇 가동 중 Bybit 봇 상태·kline 캐시·저널(`bybit_trades.csv`)이 여기 쌓임 — Phase 2 백테스트 재료 |
   | `PRICE_TTL_SECONDS` | 60 | 실시간 현물가 캐시 수명 |
   | `PRICE_FEED_DISABLED` | (없음) | `1`이면 실시간 피드 끄고 항상 스냅샷 값 사용 |

   현물가는 `/api/price`가 자동 갱신하므로 `SPOT`은 실시간 피드 불가 시의 폴백입니다.
   실현가격·200주선은 무료 공개 API가 없는 온체인 집계값이라 env 기반을 유지합니다
   (`/refresh-snapshot` 파이프라인으로 재검증).

4. FSM 상태를 재배포 간에 유지하려면 Railway Volume을 붙이고 `DATA_DIR`를 마운트 경로로 지정하세요.

### 로컬 실행

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
# http://localhost:8000
```

## Harness 개념 적용 — 에이전트 팀

이 리포는 Harness의 **파이프라인 + 생성-검증 패턴**으로 설계된 에이전트 팀을 포함합니다.

```
snapshot-analyst ──▶ quant-modeler ──▶ signal-operator
   (스냅샷 검증)        (모델/분포 갱신)      (FSM 일별 운영)
                          │
                          ▼
                     qa-reviewer  ◀── 생성-검증 게이트 (§1 원칙 8종 감사)
```

| 파일 | 역할 |
|---|---|
| `.claude/agents/snapshot-analyst.md` | [SNAPSHOT] 값 교차 검증 (파이프라인 1단계) |
| `.claude/agents/quant-modeler.md` | 렌즈/앵커 갱신 + 분포 재계산 (2단계) |
| `.claude/agents/signal-operator.md` | FSM 운영·phase 해석 (3단계) |
| `.claude/agents/qa-reviewer.md` | 지식 베이스 §1 원칙 감사 게이트 (생성-검증) |
| `.claude/skills/refresh-snapshot/` | 검증→갱신→감사 파이프라인 오케스트레이터 |
| `.claude/skills/bottom-report/` | 분포+FSM 팬아웃/팬인 종합 리포트 |

Claude Code에서 `/refresh-snapshot`, `/bottom-report`로 트리거하거나
"스냅샷 갱신해줘" 같은 자연어로 사용합니다.

## 디렉토리

```
├── app/                # FastAPI 서비스 (ensemble / fsm / snapshot / main)
├── static/index.html   # 대시보드
├── knowledge/          # 분석 지식 베이스 원문
├── docs/               # 참고 산출물 (샘플 분포 차트)
├── .claude/            # Harness 에이전트 팀 + 스킬
├── Dockerfile          # Railway 빌드
└── railway.json        # Railway 배포 설정
```

## 라이선스 / 면책

분석 프레임워크 예제일 뿐이며 투자 조언이 아닙니다.
