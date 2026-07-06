"""Offline tests for the static guard scripts (scripts/check_*.py) —
boundary values and false-positive checks, no filesystem walking.
Run:  python -m tests.test_guards

가드가 조용히 '항상 통과'로 무력화되는 것을 막는 메타 회귀 테스트
(QuantMaster Pro 의 가드-자체-테스트 규율 이식)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import check_complexity as cc  # noqa: E402
import check_responsibility as cr  # noqa: E402


# ── complexity ──────────────────────────────────────────────────────────────

def test_count_lines():
    assert cc.count_lines("") == 0
    assert cc.count_lines("a\n") == 1
    assert cc.count_lines("a\nb") == 2
    assert cc.count_lines("a\nb\n") == 2
    print("ok  count_lines")


def test_complexity_boundary():
    # 500 = 통과, 501 = 실패 (한계는 '초과'에서만 발동)
    assert cc.evaluate_file("app/x.py", cc.MAX_LINES - 1) is None
    assert cc.evaluate_file("app/x.py", cc.MAX_LINES) is None
    v = cc.evaluate_file("app/x.py", cc.MAX_LINES + 1)
    assert v is not None and "분할" in v
    print("ok  complexity boundary 499/500/501")


def test_complexity_baseline_ratchet():
    # baseline 항목은 cap 까지 허용, cap 초과 시 실패 — BASELINE 이 비어 있어도
    # 래칫 메커니즘 자체를 합성 항목으로 검증한다.
    rel, cap = "tests/_synthetic_baseline.py", 550
    saved = cc.BASELINE.get(rel)
    cc.BASELINE[rel] = (cap, "합성 baseline — 래칫 메커니즘 회귀 테스트")
    try:
        assert cc.evaluate_file(rel, cap) is None
        v = cc.evaluate_file(rel, cap + 1)
        assert v is not None and "래칫" in v
    finally:
        if saved is None:
            del cc.BASELINE[rel]
        else:
            cc.BASELINE[rel] = saved
    # 실제 등재된 baseline 항목은 모두 사유를 가져야 한다 (있을 경우)
    for _rel, (_cap, reason) in cc.BASELINE.items():
        assert reason.strip(), f"{_rel}: baseline 항목에는 사유가 필수다"
    print("ok  baseline ratchet cap")


def test_complexity_scan_repo_clean():
    violations, _infos = cc.scan()
    assert violations == [], f"현재 저장소가 복잡도 한계 위반: {violations}"
    print("ok  repo scan clean (complexity)")


# ── responsibility ──────────────────────────────────────────────────────────

def _tagged(desc: str, pad_lines: int = 0) -> str:
    pad = "# pad\n" * pad_lines
    return f'{pad}"""@responsibility {desc}"""\nx = 1\n'


def test_responsibility_ok():
    assert cr.evaluate_text("app/x.py", _tagged("가격 피드 단일 통로")) is None
    # 주석 형태도 허용
    assert cr.evaluate_text("app/x.py", "# @responsibility 전략 레지스트리\nx = 1\n") is None
    print("ok  responsibility tag accepted (docstring + comment)")


def test_responsibility_missing():
    v = cr.evaluate_text("app/x.py", '"""그냥 설명"""\nx = 1\n')
    assert v is not None and "@responsibility" in v
    print("ok  missing tag rejected")


def test_responsibility_word_limit():
    ok_desc = " ".join(["w"] * cr.MAX_WORDS)          # 25단어 = 통과
    bad_desc = " ".join(["w"] * (cr.MAX_WORDS + 1))   # 26단어 = 실패
    assert cr.evaluate_text("app/x.py", _tagged(ok_desc)) is None
    v = cr.evaluate_text("app/x.py", _tagged(bad_desc))
    assert v is not None and "단어" in v
    print("ok  word limit 25/26")


def test_responsibility_empty_tag():
    v = cr.evaluate_text("app/x.py", '"""@responsibility"""\nx = 1\n')
    assert v is not None and "설명이 없다" in v
    print("ok  empty tag rejected")


def test_responsibility_beyond_head():
    # 태그가 21번째 줄에 있으면 못 찾은 것으로 처리
    v = cr.evaluate_text("app/x.py", _tagged("늦은 태그", pad_lines=cr.HEAD_LINES))
    assert v is not None
    # 20줄 안이면 통과 (pad 19 + 태그 = 20번째 줄)
    assert cr.evaluate_text("app/x.py", _tagged("경계 태그", pad_lines=cr.HEAD_LINES - 1)) is None
    print("ok  head-20-lines boundary")


def test_responsibility_empty_file_exempt():
    assert cr.evaluate_text("app/__init__.py", "") is None
    assert cr.evaluate_text("app/__init__.py", "  \n\n") is None
    print("ok  empty file exempt")


def test_responsibility_scan_repo_clean():
    violations = cr.scan()
    assert violations == [], f"현재 저장소가 책임 태그 위반: {violations}"
    print("ok  repo scan clean (responsibility)")


if __name__ == "__main__":
    test_count_lines()
    test_complexity_boundary()
    test_complexity_baseline_ratchet()
    test_complexity_scan_repo_clean()
    test_responsibility_ok()
    test_responsibility_missing()
    test_responsibility_word_limit()
    test_responsibility_empty_tag()
    test_responsibility_beyond_head()
    test_responsibility_empty_file_exempt()
    test_responsibility_scan_repo_clean()
    print("\nall guard tests passed ✅")
