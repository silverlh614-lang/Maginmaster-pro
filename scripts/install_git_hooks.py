"""@responsibility git pre-commit 훅 설치기 — 커밋 시 validate_all.py 자동 실행 배선

.git/hooks/pre-commit 에 정적 가드 실행을 심는다. 1회 실행하면 이후 모든
커밋이 가드를 통과해야 한다. clone 직후 반드시 실행할 것.

기존 훅이 있으면 pre-commit.backup 으로 보존한 뒤 덮어쓴다
(우리가 심은 훅이면 그냥 갱신).

사용: python scripts/install_git_hooks.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:  # Windows cp949 콘솔 대비
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
MARKER = "# coinmaster-guards"

HOOK = f"""#!/bin/sh
{MARKER} — scripts/install_git_hooks.py 가 생성 (수동 편집 금지)
set -e
python scripts/validate_all.py
"""


def main() -> int:
    hooks_dir = ROOT / ".git" / "hooks"
    if not hooks_dir.is_dir():
        print("[hooks] FAIL — .git/hooks 없음 (git 저장소 루트에서 실행하라)")
        return 1
    target = hooks_dir / "pre-commit"
    if target.exists():
        existing = target.read_text(encoding="utf-8", errors="replace")
        if MARKER not in existing:
            backup = hooks_dir / "pre-commit.backup"
            backup.write_text(existing, encoding="utf-8")
            print(f"[hooks] 기존 훅을 {backup.name} 으로 백업")
    target.write_text(HOOK, encoding="utf-8", newline="\n")
    os.chmod(target, 0o755)
    print("[hooks] OK — pre-commit 훅 설치 완료 (커밋마다 validate_all 실행)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
