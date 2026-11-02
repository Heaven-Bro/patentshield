#!/usr/bin/env python3
"""Find or untrack secrets and generated artifacts in the Git index.

This utility never prints file contents and never deletes working-tree files.
With --include-history it also checks reachable Git objects for sensitive paths.
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
    "uploads",
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
        if part == ".env" or (part.startswith(".env.") and not (part == ".env.example" and index == len(parts) - 1)):
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


def list_historical_sensitive_paths(root: Path) -> list[str]:
    """Return unsafe paths found in Git objects reachable from local refs.

    Git's object listing exposes path names only; this deliberately avoids
    printing or copying historical credential values.
    """
    result = subprocess.run(
        ["git", "rev-list", "--objects", "--all"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    unsafe: set[str] = set()
    for entry in result.stdout.decode("utf-8", errors="surrogateescape").splitlines():
        _, separator, filename = entry.partition(" ")
        if not separator or not filename:
            continue
        path = PurePosixPath(filename)
        parts = {part.casefold() for part in path.parts}
        # Historical source hygiene focuses on material that can expose
        # credentials or customer documents. Generated caches/static output
        # are still checked in the current index but need not trigger a history
        # rewrite merely because an old cache blob is reachable.
        is_sensitive = (
            any(
                part == ".env"
                or (part.startswith(".env.") and part != ".env.example")
                for part in parts
            )
            or bool(parts & {"media", "uploads"})
            or path.name.casefold() in {
                "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
                "private.key", "private_key.pem",
            }
            or path.suffix.casefold() in {".sqlite", ".sqlite3", ".p12", ".pfx", ".key"}
            or (path.name.casefold().endswith(".pem") and path.name.casefold() != "cacert.pem")
        )
        if is_sensitive:
            unsafe.add(filename)
    return sorted(unsafe)


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
    parser.add_argument(
        "--include-history",
        action="store_true",
        help="also reject sensitive paths in Git objects reachable from local refs",
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
            findings.setdefault(root_path, []).append(f"current index: {filename}")

    if args.include_history:
        try:
            historical_paths = list_historical_sensitive_paths(root)
        except (OSError, subprocess.CalledProcessError) as exc:
            print(
                f"Cannot inspect Git history ({type(exc).__name__}); no files were changed.",
                file=sys.stderr,
            )
            return 2
        for filename in historical_paths:
            root_path = offending_root(PurePosixPath(filename))
            if root_path is not None:
                findings.setdefault(root_path, []).append(f"Git history: {filename}")

    if not findings:
        print("Git index/history hygiene passed: no known secret or customer-data paths found.")
        return 0

    print("Potential secret/generated paths found (paths only; no contents):")
    for root_path, matched in sorted(findings.items(), key=lambda item: str(item[0])):
        current_count = sum(item.startswith("current index: ") for item in matched)
        history_count = sum(item.startswith("Git history: ") for item in matched)
        print(
            f"  {root_path} ({current_count} current-index path(s), "
            f"{history_count} historical path(s))"
        )

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
        "Use --untrack to remove current paths from the index without deleting working files. "
        "Use --include-history with --strict in CI; historical secrets require a history rewrite "
        "after credential rotation.",
        file=sys.stderr,
    )
    return 1 if args.strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
