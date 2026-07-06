"""@responsibility 파일 복잡도 가드 — 500줄 한계 + baseline 래칫, 초과 시 EXIT 1

파일당 500줄 하드 한계를 강제한다 (QuantMaster Pro 패턴의 경량 이식).

규칙:
  1. app/ scripts/ tests/ 아래 모든 .py 파일은 500줄을 초과할 수 없다.
  2. 한계 도입 시점에 이미 초과한 파일은 BASELINE 에 등재한다 (래칫):
     - baseline 파일은 등재된 cap 까지만 허용 — cap 초과 시 즉시 실패.
     - baseline 파일이 500줄 이하로 내려오면 목록에서 제거하라고 안내한다.
     - 목록은 줄어들기만 한다. 신규 등재는 금지 (분할이 답이다).
  3. 위반 발견 시 EXIT 1 로 커밋을 차단한다.

사용: python scripts/check_complexity.py
"""
from __future__ import annotations

import sys
from pathlib import Path

try:  # Windows cp949 콘솔 대비
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = ("app", "scripts", "tests")
MAX_LINES = 500

# ── baseline 래칫 ──────────────────────────────────────────────────────────
# path (posix, ROOT 기준 상대) -> (cap, 사유). cap 은 절대 늘리지 않는다.
# 파일을 분할해 500줄 이하가 되면 해당 항목을 삭제한다.
BASELINE: dict[str, tuple[int, str]] = {}


def count_lines(text: str) -> int:
    """개행 기준 물리적 줄 수 (마지막 줄 개행 유무 무관)."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def evaluate_file(rel_path: str, line_count: int) -> str | None:
    """단일 파일 판정. 위반이면 사유 문자열, 통과면 None."""
    if rel_path in BASELINE:
        cap, _reason = BASELINE[rel_path]
        if line_count > cap:
            return (
                f"{rel_path}: {line_count}줄 > baseline cap {cap}줄 "
                f"— 래칫 위반, 파일을 분할하라"
            )
        return None
    if line_count > MAX_LINES:
        return f"{rel_path}: {line_count}줄 > 한계 {MAX_LINES}줄 — 파일을 분할하라"
    return None


def scan(root: Path = ROOT) -> tuple[list[str], list[str]]:
    """전체 트리 스캔. (위반 목록, 안내 목록) 반환."""
    violations: list[str] = []
    infos: list[str] = []
    for top in SCAN_ROOTS:
        base = root / top
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            rel = p.relative_to(root).as_posix()
            n = count_lines(p.read_text(encoding="utf-8"))
            v = evaluate_file(rel, n)
            if v:
                violations.append(v)
            elif rel in BASELINE and n <= MAX_LINES:
                infos.append(
                    f"{rel}: {n}줄로 감소 — BASELINE 에서 제거 가능 (래칫 상환 완료)"
                )
    return violations, infos


def main() -> int:
    violations, infos = scan()
    for msg in infos:
        print(f"[complexity][INFO] {msg}")
    if violations:
        print(f"[complexity] FAIL — {len(violations)}건 위반:")
        for v in violations:
            print(f"  - {v}")
        return 1
    print(f"[complexity] OK — 모든 파일 {MAX_LINES}줄 이하 (baseline {len(BASELINE)}건)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
