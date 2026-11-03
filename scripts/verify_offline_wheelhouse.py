#!/usr/bin/env python3
"""Verify wheelhouse provenance, lockfile identity, and target compatibility."""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
import sysconfig
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify(wheelhouse: Path, lockfile: Path) -> None:
    if wheelhouse.is_symlink() or not wheelhouse.is_dir():
        raise ValueError("Wheelhouse must be a regular directory")
    if lockfile.is_symlink() or not lockfile.is_file():
        raise ValueError("Requirements lock must be a regular file")
    manifest_path = wheelhouse / "wheelhouse-manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("Wheelhouse manifest is missing or unsafe")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Wheelhouse manifest is invalid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("Unsupported wheelhouse manifest schema")
    expected_runtime = {
        "python_version": platform.python_version(),
        "implementation": platform.python_implementation(),
        "system": platform.system(),
        "machine": platform.machine(),
        "platform_tag": sysconfig.get_platform(),
    }
    for key, expected in expected_runtime.items():
        if manifest.get(key) != expected:
            raise ValueError(f"Wheelhouse target mismatch for {key}: expected {expected!r}")
    requirements = manifest.get("requirements")
    if not isinstance(requirements, list):
        raise ValueError("Wheelhouse manifest requirements must be an array")
    matching = [entry for entry in requirements if isinstance(entry, dict) and entry.get("file") == lockfile.name]
    if len(matching) != 1 or matching[0].get("sha256") != sha256(lockfile):
        raise ValueError(f"Wheelhouse was not built from this exact lockfile: {lockfile.name}")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list) or any(not isinstance(item, str) for item in artifacts):
        raise ValueError("Wheelhouse artifact inventory is malformed")
    if len(artifacts) != len(set(artifacts)):
        raise ValueError("Wheelhouse artifact inventory contains duplicates")
    for name in artifacts:
        if not name or Path(name).name != name or name in {".", ".."}:
            raise ValueError(f"Unsafe wheelhouse artifact name: {name!r}")
    actual = sorted(
        item.name for item in wheelhouse.iterdir()
        if item.is_file() and item.name not in {"SHA256SUMS", "wheelhouse-manifest.json"}
    )
    if actual != sorted(artifacts):
        raise ValueError("Wheelhouse files do not match the manifest artifact inventory")
    for item in wheelhouse.iterdir():
        if item.is_symlink():
            raise ValueError(f"Symlinks are not allowed in the wheelhouse: {item.name}")
    print(f"Wheelhouse verified for {lockfile.name} on {expected_runtime['platform_tag']} / Python {expected_runtime['python_version']}.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheelhouse", type=Path)
    parser.add_argument("requirements", type=Path)
    args = parser.parse_args()
    try:
        verify(args.wheelhouse, args.requirements)
    except (OSError, ValueError) as exc:
        print(f"Wheelhouse verification failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
