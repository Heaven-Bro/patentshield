#!/usr/bin/env python3
"""Find or untrack secrets and generated artifacts in the Git index.

This utility never prints file contents and never deletes working-tree files.
It does not rewrite commit history; leaked credentials must be rotated first.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path, PurePosixPath

FORBIDDEN_DIRS = {
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "staticfiles",
    "media",
    "ml_model_cache",
    "htmlcov",
}
PRIVATE_KEY_NAMES = {
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "id_dsa",
}
ALLOWED_ENV_TEMPLATES = {".env.example"}


def is_secret_env_name(name: str) -> bool:
    """Return true for real env files, but not the checked-in template."""
    if name in ALLOWED_ENV_TEMPLATES:
        return False
    if name == ".env":
        return True
    return name.startswith(".env.")


def offending_root(path: PurePosixPath) -> PurePosixPath | None:
    """Return the smallest index path to untrack, if the path is unsafe."""
    parts = path.parts
    for index, part in enumerate(parts):
        if part in FORBIDDEN_DIRS:
            return PurePosixPath(*parts[: index + 1])

    name = path.name
    if is_secret_env_name(name) or name in PRIVATE_KEY_NAMES:
        return path
    if path.suffix.casefold() in {".sqlite3", ".sqlite", ".p12", ".pfx"}:
        return path
    if path.suffix.casefold() == ".key":
        return path
    if name.endswith(".pem") and name not in {"cacert.pem"}:
        return path
    return None


def list_tracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [
        entry.decode("utf-8", errors="surrogateescape")
        for entry in result.stdout.split(b"\0")
        if entry
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--untrack",
        action="store_true",
        help="remove detected paths from Git's index but keep local working files",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="return a nonzero exit code when findings remain",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    try:
        tracked_files = list_tracked_files(root)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(
            "Cannot inspect Git index. Run this command from a Git checkout; "
            "no files were changed.",
            file=sys.stderr,
        )
        return 2

    findings: dict[PurePosixPath, list[str]] = {}
    for filename in tracked_files:
        path = PurePosixPath(filename)
        root_path = offending_root(path)
        if root_path is not None:
            findings.setdefault(root_path, []).append(filename)

    if not findings:
        print("Git index hygiene passed: no known secret/generated paths found.")
        return 0

    print("Potential secret/generated paths are tracked (paths only; no contents):")
    for root_path, matched in sorted(findings.items(), key=lambda item: str(item[0])):
        print(f"  {root_path} ({len(matched)} tracked path(s))")

    if args.untrack:
        targets = [str(path) for path in sorted(findings, key=str)]
        subprocess.run(
            ["git", "rm", "--cached", "-r", "--ignore-unmatch", "--", *targets],
            cwd=root,
            check=True,
        )
        print("\nRemoved these paths from the Git index only; local files remain.")
        print(
            "Review and commit the index changes. This does not rewrite Git history "
            "or rotate credentials."
        )
        return 0

    print(
        "Use --untrack to remove them from the index without deleting working files. "
        "Use --strict in CI to fail while findings remain.",
        file=sys.stderr,
    )
    return 1 if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
