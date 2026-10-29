#!/usr/bin/env python3
"""Create a sanitized ZIP from the current worktree for release verification.

Includes tracked, modified, and non-ignored untracked files. Secrets, generated
artifacts, VCS metadata, dependencies, local databases, and private-key files are
excluded before the ZIP is written. The resulting archive must still be passed
through verify_release_archive.py.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath

from verify_release_archive import reason_for_member


def tracked_and_untracked_files(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    paths = {item.decode("utf-8", "surrogateescape") for item in result.stdout.split(b"\0") if item}
    return sorted(paths)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", help="destination ZIP path")
    parser.add_argument("--root", default=None, help="repository root (defaults to this script's parent directory)")
    args = parser.parse_args()
    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parent.parent
    output = Path(args.output).resolve()
    if not (root / ".git").exists():
        parser.error(f"{root} is not a Git working tree")
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        members = tracked_and_untracked_files(root)
    except subprocess.CalledProcessError as exc:
        print(f"Unable to enumerate Git files: {exc.stderr.decode(errors='replace')}", file=sys.stderr)
        return 2

    written = 0
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for rel in members:
            path = PurePosixPath(rel)
            if reason_for_member(rel):
                continue
            candidate = root / Path(*path.parts)
            # Reject any symlink component before resolving; resolve() itself
            # follows links and would otherwise hide the original symlink.
            if any((root / Path(*path.parts[:index])).is_symlink() for index in range(1, len(path.parts) + 1)):
                print(f"Skipping symbolic link path: {rel}", file=sys.stderr)
                continue
            source = candidate.resolve()
            try:
                source.relative_to(root)
            except ValueError:
                print(f"Refusing path outside root: {rel}", file=sys.stderr)
                return 2
            if not source.is_file():
                continue
            if source == output:
                continue
            arcname = f"PatentShield/{path.as_posix()}"
            archive.write(source, arcname=arcname)
            written += 1
    print(f"Created sanitized release candidate ({written} files): {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
