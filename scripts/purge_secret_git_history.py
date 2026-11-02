#!/usr/bin/env python3
"""Remove committed environment files and tenant uploads from reachable Git history.

This is an opt-in, destructive history rewrite. Run only after credentials have
been rotated, the patch has been committed, and an encrypted offline backup has
been made. The script defaults to a dry-run.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

from secure_repository_hygiene import list_historical_sensitive_paths

SENSITIVE_HISTORY_PATHS = (
    ".env",
    "backend/.env",
    "frontend/.env",
    "backend/media",
    "backend/uploads",
)


def collect_sensitive_paths(root: Path) -> list[str]:
    """Combine known paths with every sensitive path found in reachable refs."""
    paths = set(SENSITIVE_HISTORY_PATHS)
    for filename in list_historical_sensitive_paths(root):
        parts = PurePosixPath(filename).parts
        for directory in ("media", "uploads"):
            if directory in parts:
                index = parts.index(directory)
                paths.add(PurePosixPath(*parts[: index + 1]).as_posix())
                break
        else:
            paths.add(filename)
    return sorted(paths)


def run_git(
    root: Path,
    *args: str,
    capture_output: bool = True,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=capture_output,
        text=capture_output,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the destructive Git history rewrite (default is dry-run)",
    )
    parser.add_argument(
        "--confirm-history-rewrite",
        action="store_true",
        help="required together with --apply to confirm rewriting all local refs",
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    if not (root / ".git").exists():
        print("ERROR: run this utility from a normal Git working tree.", file=sys.stderr)
        return 2

    try:
        paths_to_remove = collect_sensitive_paths(root)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(
            f"ERROR: unable to enumerate sensitive Git history ({type(exc).__name__}).",
            file=sys.stderr,
        )
        return 2

    if not args.apply:
        print("DRY RUN — no Git objects or files were changed.")
        print("The following sensitive paths will be removed from every rewritten ref:")
        for path in paths_to_remove:
            print(f"  - {path}")
        print(
            "\nAfter rotating the exposed credentials and committing this patch, "
            "make an encrypted offline backup, then rerun with both --apply and "
            "--confirm-history-rewrite."
        )
        return 0

    if not args.confirm_history_rewrite:
        print(
            "ERROR: --apply requires --confirm-history-rewrite. "
            "This rewrites commit IDs and requires coordinated force-pushes.",
            file=sys.stderr,
        )
        return 2

    if shutil.which("git-filter-repo") is None:
        print(
            "ERROR: git-filter-repo is not installed. Install it from the official "
            "project before retrying; no changes were made.",
            file=sys.stderr,
        )
        return 2

    try:
        status = run_git(root, "status", "--porcelain").stdout
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: unable to inspect repository status ({type(exc).__name__}).", file=sys.stderr)
        return 2
    if status.strip():
        print(
            "ERROR: working tree is not clean. Commit the remediation patch and "
            "resolve all staged/unstaged/untracked changes before rewriting history.",
            file=sys.stderr,
        )
        return 2

    command = ["filter-repo", "--force"]
    for path in paths_to_remove:
        command.extend(["--path", path])
    command.append("--invert-paths")

    try:
        subprocess.run(["git", *command], cwd=root, check=True)
        run_git(root, "reflog", "expire", "--expire=now", "--all", capture_output=False)
        run_git(root, "gc", "--prune=now", capture_output=False)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(
            f"ERROR: Git history rewrite/cleanup failed ({type(exc).__name__}). "
            "Inspect repository state before retrying.",
            file=sys.stderr,
        )
        return 1

    print("Completed local history rewrite and garbage collection.")
    print(
        "IMPORTANT: verify all refs with scripts/secure_repository_hygiene.py "
        "--strict --include-history. Re-add the remote if git-filter-repo removed it, "
        "then coordinate force-pushing every branch and tag. Invalidate old clones "
        "and forks where possible. Credential rotation is still mandatory."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
