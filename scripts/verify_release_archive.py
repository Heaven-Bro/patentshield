#!/usr/bin/env python3
"""Fail closed when a release ZIP contains environment secrets or build artifacts."""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import PurePosixPath

FORBIDDEN_COMPONENTS = {
    # Version-control metadata and generated/development artifacts.
    ".git",
    ".hg",
    ".svn",
    "__pycache__",
    "node_modules",
    "dist",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    # User/tenant data and generated deployment assets must never be part of a
    # source release, even when the local directory is Git-ignored.
    "media",
    "uploads",
    "staticfiles",
    "ml_model_cache",
}
FORBIDDEN_SUFFIXES = {".pyc", ".pyo", ".pyd", ".sqlite3", ".sqlite3-journal"}
PRIVATE_KEY_NAMES = {
    "id_rsa", "id_ed25519", "id_ecdsa", "private.key", "private_key.pem",
}


def reason_for_member(name: str) -> str | None:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        return "unsafe absolute/traversal path"
    parts = [part.casefold() for part in path.parts]
    basename = parts[-1] if parts else ""
    if any(part in FORBIDDEN_COMPONENTS for part in parts):
        return "forbidden VCS, customer-data, or generated directory"
    for index, part in enumerate(parts):
        if part == ".env.example" and index == len(parts) - 1 and not normalized.endswith("/"):
            continue
        if part == ".env" or part.startswith(".env."):
            return "environment file/path (only a .env.example file is permitted)"
    if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
        return "compiled bytecode or local database artifact"
    if basename in PRIVATE_KEY_NAMES or basename.endswith((".pem", ".p12", ".pfx")):
        return "private-key/certificate bundle must not be shipped in source archive"
    return None


def verify_archive(archive_path: str) -> list[str]:
    failures: list[str] = []
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            seen: set[str] = set()
            for info in archive.infolist():
                name = info.filename
                # Reject Unix symlink entries so extraction cannot introduce
                # links that later redirect file writes outside the release tree.
                unix_mode = (info.external_attr >> 16) & 0xFFFF
                if (unix_mode & 0o170000) == 0o120000:
                    failures.append(f"{name}: symbolic links are not permitted in release archives")
                if name in seen:
                    failures.append(f"{name}: duplicate ZIP member")
                seen.add(name)
                reason = reason_for_member(name)
                if reason:
                    failures.append(f"{name}: {reason}")
    except (OSError, zipfile.BadZipFile) as exc:
        return [f"{archive_path}: cannot read ZIP archive ({type(exc).__name__})"]
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", help="ZIP release artifact to inspect")
    args = parser.parse_args()
    failures = verify_archive(args.archive)
    if failures:
        print("Release archive verification FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    print(f"Release archive verification passed: {args.archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
