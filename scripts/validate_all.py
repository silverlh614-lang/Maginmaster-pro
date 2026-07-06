"""@responsibility 정적 가드 일괄 실행기 — 모든 check_*.py 를 순차 실행, 하나라도 실패 시 EXIT 1

커밋 전 필수 관문 (pre-commit 훅이 이 스크립트를 호출한다).

가드 추가 방법: scripts/check_<이름>.py 를 만들고 아래 GUARDS 에 등록한다.
각 가드는 독립 실행 가능해야 하며(python scripts/check_x.py),
위반 시 EXIT 1 + 파일:사유 목록을 출력해야 한다.

사용: python scripts/validate_all.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

try:  # Windows cp949 콘솔 대비
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

SCRIPTS_DIR = Path(__file__).resolve().parent

GUARDS = (
    "check_complexity.py",
    "check_responsibility.py",
)


def main() -> int:
    failed: list[str] = []
    for guard in GUARDS:
        path = SCRIPTS_DIR / guard
        r = subprocess.run([sys.executable, str(path)], cwd=SCRIPTS_DIR.parent)
        if r.returncode != 0:
            failed.append(guard)
    print()
    if failed:
        print(f"[validate:all] FAIL — {len(failed)}/{len(GUARDS)} 가드 실패: {', '.join(failed)}")
        print("[validate:all] 위 위반을 수정하기 전에는 커밋할 수 없다 (--no-verify 우회 금지)")
        return 1
    print(f"[validate:all] OK — {len(GUARDS)}개 가드 전부 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
