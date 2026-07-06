---
name: refresh-snapshot
description: >
  [SNAPSHOT] 온체인 앵커(실현가격·200주선·현물가·ATH) 재검증 → 모델 갱신 →
  QA 감사까지 파이프라인으로 실행. "스냅샷 갱신해줘", "앵커 재검증" 요청 시 사용.
---

# /refresh-snapshot — 스냅샷 갱신 파이프라인

지식 베이스의 [SNAPSHOT] 값은 시간이 지나면 틀린다. 이 스킬은 파이프라인 패턴으로
검증 → 갱신 → 재계산 → 감사를 순차 실행한다.

## 파이프라인 (순차 의존 — pipeline pattern)

```
snapshot-analyst ──▶ quant-modeler ──▶ qa-reviewer
   (검증 보고)         (코드/분포 갱신)      (원칙 감사)
```

### 1단계 — snapshot-analyst 에이전트 호출
- 입력: `app/snapshot.py` 현재 기본값 + `knowledge/btc_analysis_knowledge.md` §2
- 출력: 앵커별 `기존값 → 검증값 + 출처` 표

### 2단계 — quant-modeler 에이전트 호출 (1단계 출력을 프롬프트에 포함)
- `app/snapshot.py` 기본값 갱신, 필요 시 `app/ensemble.py` 렌즈 조정
- 분포 재계산 후 변경 전후 비교표 (median/EV/P(바닥<실현가격))
- `knowledge/btc_analysis_knowledge.md`의 §2, §8 [SNAPSHOT] 표와
  `data_as_of` 갱신

### 3단계 — qa-reviewer 에이전트 호출 (2단계 diff를 프롬프트에 포함)
- §1 원칙 8종 감사, REJECT 시 2단계로 되돌아가 수정 후 재감사 (최대 2회)

## 완료 조건
- qa-reviewer APPROVE
- `GET /api/snapshot`과 `GET /api/simulate`가 새 값을 반영
- 사용자에게 변경 요약 보고 (Railway 환경변수로 즉시 반영하려면
  REALIZED_PRICE/MA_200W/SPOT/ATH/SNAPSHOT_DATE 설정 안내 포함)
