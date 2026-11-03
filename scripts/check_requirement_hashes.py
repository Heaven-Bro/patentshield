#!/usr/bin/env python3
"""Fail closed when a pip requirements lock has unhashed package entries."""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

PACKAGE_LINE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[^]]+\])?\s*==\s*[^\s;\\]+")


def find_unhashed(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    result: list[str] = []
    current: str | None = None
    has_hash = False

    def finish() -> None:
        if current is not None and not has_hash:
            result.append(current)

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = PACKAGE_LINE.match(line)
        if match:
            finish()
            current = match.group(1)
            has_hash = "--hash=sha256:" in line
        elif current is not None and "--hash=sha256:" in line:
            has_hash = True
    finish()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("requirements", type=Path)
    args = parser.parse_args()
    path = args.requirements.resolve()
    if not path.is_file():
        print(f"Requirements lock not found: {path}", file=sys.stderr)
        return 2
    try:
        missing = find_unhashed(path)
    except (OSError, UnicodeError) as exc:
        print(f"Cannot read requirements lock ({type(exc).__name__})", file=sys.stderr)
        return 2
    if missing:
        print("Offline wheelhouse blocked: package entries without SHA-256 hashes:", file=sys.stderr)
        for name in missing:
            print(f"  - {name}", file=sys.stderr)
        print("Regenerate the lock with scripts/compile_requirements.sh on a connected, reviewed build workstation.", file=sys.stderr)
        return 1
    print(f"All pinned packages in {path.name} have SHA-256 hashes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
