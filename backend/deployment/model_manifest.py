"""Validate that installed offline model files match the signed bundle manifest."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any


class ModelManifestError(RuntimeError):
    """Raised when an installed model does not match its verified release manifest."""


_REVISION_RE = re.compile(r"^(?:[a-f0-9]{40}|[a-f0-9]{64})$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(raw: Any) -> Path:
    if not isinstance(raw, str) or not raw:
        raise ModelManifestError("Model manifest contains an empty or non-string file path.")
    pure = PurePosixPath(raw)
    if (
        pure.is_absolute()
        or ".." in pure.parts
        or "\\" in raw
        or "\x00" in raw
        or pure.as_posix() != raw
    ):
        raise ModelManifestError("Model manifest contains an unsafe file path.")
    return Path(*pure.parts)


def verify_installed_model(
    manifest_path: str | Path,
    *,
    model_key: str,
    expected_model_id: str,
    expected_revision: str,
    installed_model_dir: str | Path,
) -> dict[str, Any]:
    """Verify model identity and every installed model file against its bundle record.

    ``manifest_path`` is the verified metadata file copied by
    ``deployment.model_bundle install``. ``installed_model_dir`` points to the
    extracted model directory (for FastEmbed, its Hub-cache snapshot directory).
    """
    manifest_file = Path(manifest_path)
    if manifest_file.is_symlink() or not manifest_file.is_file():
        raise ModelManifestError("The installed offline model-bundle manifest is missing or unsafe.")
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ModelManifestError("The installed offline model-bundle manifest is invalid JSON.") from exc

    if not isinstance(manifest, dict) or manifest.get("schema_version") != 2:
        raise ModelManifestError("Unsupported installed model-bundle manifest schema.")
    models = manifest.get("models")
    if not isinstance(models, dict) or set(models) != {
        "bge-m3", "reranker", "public-patent-embedding"
    }:
        raise ModelManifestError("Installed model bundle must contain all three supported model records.")
    if model_key not in models:
        raise ModelManifestError(f"Installed model bundle does not contain {model_key!r}.")

    record = models[model_key]
    if not isinstance(record, dict):
        raise ModelManifestError(f"Installed model record for {model_key!r} is malformed.")
    if record.get("model_id") != expected_model_id:
        raise ModelManifestError(f"Installed {model_key} model ID does not match runtime settings.")
    if not isinstance(expected_revision, str) or not _REVISION_RE.fullmatch(expected_revision):
        raise ModelManifestError(f"Runtime revision for {model_key!r} must be an immutable commit ID.")
    if record.get("revision") != expected_revision:
        raise ModelManifestError(f"Installed {model_key} revision does not match runtime settings.")

    model_dir = Path(installed_model_dir)
    if model_dir.is_symlink() or not model_dir.is_dir():
        raise ModelManifestError(f"Installed {model_key} model directory is missing or unsafe.")
    resolved_root = model_dir.resolve(strict=True)

    expected_files = record.get("files")
    if not isinstance(expected_files, list) or not expected_files:
        raise ModelManifestError(f"Installed {model_key} manifest has no file inventory.")

    expected: dict[str, dict[str, Any]] = {}
    for item in expected_files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "size"}:
            raise ModelManifestError(f"Installed {model_key} file inventory is malformed.")
        relative = _safe_relative_path(item.get("path"))
        digest = item.get("sha256")
        size = item.get("size")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise ModelManifestError(f"Installed {model_key} file inventory has an invalid SHA-256.")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ModelManifestError(f"Installed {model_key} file inventory has an invalid file size.")
        if relative.as_posix() in expected:
            raise ModelManifestError(f"Installed {model_key} manifest contains duplicate file paths.")
        expected[relative.as_posix()] = item

    actual_paths: set[str] = set()
    for item in model_dir.rglob("*"):
        if item.is_symlink():
            raise ModelManifestError(f"Installed {model_key} contains a symbolic link.")
        if item.is_file():
            if not item.resolve(strict=True).is_relative_to(resolved_root):
                raise ModelManifestError(f"Installed {model_key} contains a path outside its model directory.")
            actual_paths.add(item.relative_to(model_dir).as_posix())
    if actual_paths != set(expected):
        raise ModelManifestError(f"Installed {model_key} file inventory differs from the verified release manifest.")

    for relative, entry in expected.items():
        file_path = model_dir / _safe_relative_path(relative)
        if file_path.is_symlink() or not file_path.is_file():
            raise ModelManifestError(f"Installed {model_key} contains a missing or unsafe file.")
        if file_path.stat().st_size != entry["size"] or _sha256(file_path) != entry["sha256"]:
            raise ModelManifestError(f"Installed {model_key} file failed SHA-256/size verification.")

    return record
