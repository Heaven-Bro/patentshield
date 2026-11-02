#!/usr/bin/env python3
"""Verify that all PatentShield runtime and optional packages are pinned."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_LINE = re.compile(
    r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)(?:\[[^]]+\])?\s*==\s*([^\s;\\]+)"
)

REQUIRED_PACKAGES: dict[str, set[str]] = {
    "requirements.txt": {
        "django",
        "djangorestframework",
        "django-cors-headers",
        "daphne",
        "channels",
        "channels-redis",
        "celery",
        "redis",
        "pgvector",
        "psycopg",
        "python-dotenv",
        "langgraph",
        "pydantic",
        "requests",
        "rank-bm25",
        "fastembed",
        "pypdf",
        "pymupdf",
        "reportlab",
        "python-docx",
        "openpyxl",
        "authlib",
        "python3-saml",
        "xmlsec",
        "django-storages",
    },
    "requirements-dev.txt": {"pytest", "pytest-django", "psutil"},
    "requirements-ml.txt": {"fastembed"},
}


def normalize_name(name: str) -> str:
    """Normalize a distribution name per Python packaging conventions."""
    return re.sub(r"[-_.]+", "-", name).casefold()


def pinned_distributions(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"Cannot read {path.relative_to(ROOT)}: {exc}") from exc

    for line in lines:
        match = PACKAGE_LINE.match(line)
        if match:
            pins[normalize_name(match.group(1))] = match.group(2)
    return pins


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--require-hashes",
        action="store_true",
        help="fail unless all three compiled lockfiles contain hashes",
    )
    args = parser.parse_args()

    errors: list[str] = []
    for relative_path, required_names in REQUIRED_PACKAGES.items():
        path = ROOT / relative_path
        if not path.is_file():
            errors.append(f"Missing manifest: {relative_path}")
            continue
        pins = pinned_distributions(path)
        missing = sorted(
            normalize_name(name) for name in required_names if name not in pins
        )
        if missing:
            missing_text = ", ".join(missing)
            errors.append(
                f"{relative_path} missing exact version pins: {missing_text}"
            )
        if args.require_hashes:
            text = path.read_text(encoding="utf-8")
            pins_count = len(pins)
            hashes_count = text.count("--hash=sha256:")
            if pins_count == 0 or hashes_count < pins_count:
                errors.append(
                    f"{relative_path} is not a complete hash lock "
                    f"({hashes_count} hashes for {pins_count} pinned packages); "
                    "run scripts/compile_requirements.sh."
                )

    source_to_lock = {
        "requirements.in": "requirements.txt",
        "requirements-dev.in": "requirements-dev.txt",
        "requirements-ml.in": "requirements-ml.txt",
    }
    for source_name, lock_name in source_to_lock.items():
        source_path = ROOT / source_name
        lock_path = ROOT / lock_name
        if not source_path.is_file():
            errors.append(f"Missing source manifest: {source_name}")
            continue
        if not args.require_hashes or not lock_path.is_file():
            continue
        source_pins = pinned_distributions(source_path)
        lock_pins = pinned_distributions(lock_path)
        for package_name, source_version in source_pins.items():
            locked_version = lock_pins.get(package_name)
            if locked_version != source_version:
                errors.append(
                    f"{lock_name} pin mismatch for {package_name}: "
                    f"source is {source_version}, lock is {locked_version or 'missing'}"
                )

    if errors:
        print("Dependency manifest verification failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1

    print("Dependency manifests contain all required exact-version pins.")
    if args.require_hashes:
        print("All three dependency lock files contain resolved SHA-256 hashes.")
    else:
        print(
            "For release reproducibility, use scripts/compile_requirements.sh and "
            "commit the generated hash-locked files."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())