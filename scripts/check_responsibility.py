"""@responsibility 파일 책임 태그 가드 — 상단 20줄 내 @responsibility 25단어 이내 강제

모든 소스 파일이 자기 책임을 한 줄로 선언하게 강제한다 (QuantMaster Pro 패턴 이식).

규칙:
  1. app/ scripts/ 아래 모든 .py 파일은 상단 20줄 안에 `@responsibility <설명>`
     한 줄을 가져야 한다 (docstring 또는 주석 어디든 무관).
  2. 설명은 1~25 단어 (공백 분리). 초과·누락·빈 태그는 실패.
  3. tests/ 와 빈 파일(공백뿐)은 면제.
  4. 위반 발견 시 EXIT 1 로 커밋을 차단한다.

이 태그는 "AI/사람이 파일 전체를 읽지 않고 책임을 파악"하는 토큰 라우터다.
태그가 정확할수록 탐색 비용이 줄어든다. 접속사로 책임을 나열하게 되면
파일이 두 가지 일을 하고 있다는 신호다 — 분할을 고려하라.

사용: python scripts/check_responsibility.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

try:  # Windows cp949 콘솔 대비
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = ("app", "scripts")
HEAD_LINES = 20
MAX_WORDS = 25

TAG_RE = re.compile(r"@responsibility\b[ \t]*(.*)")


def evaluate_text(rel_path: str, text: str) -> str | None:
    """단일 파일 판정. 위반이면 사유 문자열, 통과(또는 면제)면 None."""
    if not text.strip():
        return None  # 빈 파일 면제 (__init__.py 등)
    head = text.split("\n")[:HEAD_LINES]
    for line in head:
        m = TAG_RE.search(line)
        if not m:
            continue
        desc = m.group(1).strip().rstrip('"').strip()
        words = desc.split()
        if not words:
            return f"{rel_path}: @responsibility 태그에 설명이 없다"
        if len(words) > MAX_WORDS:
            return (
                f"{rel_path}: @responsibility {len(words)}단어 > 한계 {MAX_WORDS}단어 "
                f"— 책임을 한 문장으로 압축하라"
            )
        return None
    return (
        f"{rel_path}: 상단 {HEAD_LINES}줄 내 @responsibility 태그 없음 "
        f"— 파일 책임을 한 줄로 선언하라"
    )


def scan(root: Path = ROOT) -> list[str]:
    violations: list[str] = []
    for top in SCAN_ROOTS:
        base = root / top
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            rel = p.relative_to(root).as_posix()
            v = evaluate_text(rel, p.read_text(encoding="utf-8"))
            if v:
                violations.append(v)
    return violations


def main() -> int:
    violations = scan()
    if violations:
        print(f"[responsibility] FAIL — {len(violations)}건 위반:")
        for v in violations:
            print(f"  - {v}")
        return 1
    print("[responsibility] OK — 모든 소스 파일에 @responsibility 태그 존재")
    return 0


if __name__ == "__main__":
    sys.exit(main())
