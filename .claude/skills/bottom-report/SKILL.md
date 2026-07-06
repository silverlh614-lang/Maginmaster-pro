---
name: bottom-report
description: >
  현재 앙상블 분포 + FSM 상태를 종합한 바닥 분석 리포트 생성. "바닥 리포트",
  "현재 분석 요약해줘" 요청 시 사용. 팬아웃/팬인 패턴.
---

# /bottom-report — 종합 바닥 리포트

Part 1(분포)과 Part 2(FSM)는 서로 독립이므로 팬아웃으로 병렬 수집 후 팬인으로 종합한다.

## 팬아웃 (병렬 독립 — fan-out/fan-in pattern)

```
        ┌──▶ quant-modeler   : 분포 요약 (median/EV/percentile/P(바닥<레벨))
 팬아웃 ─┤
        └──▶ signal-operator : FSM 현재 phase + 최근 전이 + 누적 투입률
                    │
 팬인 ──▶ 메인 세션에서 종합 → qa-reviewer 감사 → 리포트
```

- quant-modeler에는 `python -c` 로 `app.ensemble.simulate_bottom()` +
  `summarize()`를 실행시켜 실제 수치를 받게 한다 (추정 금지)
- signal-operator에는 `data/fsm_state.json`(없으면 초기 상태)을 읽게 한다

## 팬인 — 리포트 구성
1. 스냅샷 앵커 표 + [SNAPSHOT] 날짜 명시
2. 분포 요약: median/EV, P5–P95, 이봉 구조(얕은 $50~55k vs 깊은 $28~33k) 언급
3. 결정적 선: P(바닥 < 실현가격) 강조
4. FSM: 현재 phase, 사다리 투입 현황, 다음 전이 조건
5. 면책: 구조화된 의견이며 투자 조언 아님

## 최종 게이트
qa-reviewer로 리포트를 감사 (특히 점 추정·투자 조언 표현). APPROVE 후 사용자에게 전달.
