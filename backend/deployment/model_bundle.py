"""Create, verify, and install offline PatentShield model bundles.

This module uses only the Python standard library so it can run in a minimal
release workstation and inside the signed PatentShield image without network
access. Model weights are supplied by the operator; this tool never downloads
weights or executes code from a model repository.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = 1
MODEL_SPECS = {
    "bge-m3": {"model_id": "BAAI/bge-m3", "required_dimension": 1024},
    "reranker": {"model_id": "BAAI/bge-reranker-v2-m3", "required_dimension": None},
}
WEIGHT_PATTERNS = ("*.safetensors", "pytorch_model*.bin", "tf_model.h5", "model*.onnx")
TOKENIZER_FILES = {
    "tokenizer.json", "tokenizer.model", "sentencepiece.bpe.model", "vocab.txt",
    "spiece.model", "merges.txt", "vocab.json",
}


class BundleError(RuntimeError):
    """Raised when a model bundle is incomplete or does not match its manifest."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_relative(raw: str) -> Path:
    pure = PurePosixPath(raw)
    if not raw or pure.is_absolute() or ".." in pure.parts or "\\" in raw:
        raise BundleError(f"Unsafe relative path in model manifest: {raw!r}")
    return Path(*pure.parts)


def _validate_model_dir(model_dir: Path, model_key: str) -> None:
    if not model_dir.is_dir():
        raise BundleError(f"Model directory does not exist: {model_dir}")
    if not (model_dir / "config.json").is_file():
        raise BundleError(f"{model_key}: required config.json is missing")
    files = [item for item in model_dir.rglob("*") if item.is_file()]
    if not any(any(item.match(pattern) for pattern in WEIGHT_PATTERNS) for item in files):
        raise BundleError(f"{model_key}: no supported model weight files were found")
    if not any(item.name in TOKENIZER_FILES for item in files):
        raise BundleError(f"{model_key}: tokenizer files were not found")
    for item in model_dir.rglob("*"):
        if item.is_symlink():
            raise BundleError(
                f"{model_key}: symlinks are not allowed in a portable model bundle: {item}"
            )
        if item.is_file() and item.stat().st_size <= 0:
            raise BundleError(f"{model_key}: empty file is not allowed: {item}")


def _list_model_files(model_dir: Path) -> list[dict[str, Any]]:
    result = []
    for path in sorted(item for item in model_dir.rglob("*") if item.is_file()):
        rel = path.relative_to(model_dir).as_posix()
        _safe_relative(rel)
        result.append({"path": rel, "sha256": _sha256(path), "size": path.stat().st_size})
    return result


def export_bundle(args: argparse.Namespace) -> int:
    output = Path(args.output).expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise BundleError(f"Output directory must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    models_root = output / "models"
    models_root.mkdir(exist_ok=True)
    source_map = {
        "bge-m3": (Path(args.bge_source).expanduser().resolve(), args.bge_revision),
        "reranker": (Path(args.reranker_source).expanduser().resolve(), args.reranker_revision),
    }
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "models": {},
    }
    for key, (source, revision) in source_map.items():
        _validate_model_dir(source, key)
        target = models_root / key
        shutil.copytree(source, target, symlinks=False)
        _validate_model_dir(target, key)
        manifest["models"][key] = {
            "model_id": MODEL_SPECS[key]["model_id"],
            "revision": str(revision).strip() or "operator-supplied-unknown",
            "path": f"models/{key}",
            "files": _list_model_files(target),
        }
    manifest_path = output / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    verify_bundle(output)
    print(f"Created and verified offline model bundle: {output}")
    print(f"Manifest SHA-256: {_sha256(manifest_path)}")
    return 0


def verify_bundle(bundle: Path) -> dict[str, Any]:
    bundle = bundle.expanduser().resolve(strict=True)
    manifest_path = bundle / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise BundleError("Bundle must contain a regular manifest.json file")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError("manifest.json is not valid UTF-8 JSON") from exc
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise BundleError("Unsupported model bundle manifest schema_version")
    models = manifest.get("models")
    if not isinstance(models, dict) or set(models) != set(MODEL_SPECS):
        raise BundleError("Bundle must include exactly bge-m3 and reranker model entries")
    for key, spec in models.items():
        if spec.get("model_id") != MODEL_SPECS[key]["model_id"]:
            raise BundleError(f"Unexpected model_id for {key}")
        model_rel = _safe_relative(str(spec.get("path", "")))
        model_dir = bundle / model_rel
        if model_dir.is_symlink() or not model_dir.is_dir():
            raise BundleError(f"Model directory is missing or unsafe: {model_rel}")
        if not model_dir.resolve(strict=True).is_relative_to(bundle):
            raise BundleError(f"Model directory escapes bundle root: {model_rel}")
        _validate_model_dir(model_dir, key)
        listed = spec.get("files")
        if not isinstance(listed, list) or not listed:
            raise BundleError(f"Manifest file list is missing for {key}")
        expected = {}
        for item in listed:
            if not isinstance(item, dict):
                raise BundleError(f"Malformed file entry in {key}")
            rel = _safe_relative(str(item.get("path", "")))
            if rel.as_posix() in expected:
                raise BundleError(f"Duplicate manifest file path in {key}: {rel}")
            expected[rel.as_posix()] = item
        actual_paths = {
            item.relative_to(model_dir).as_posix()
            for item in model_dir.rglob("*")
            if item.is_file()
        }
        if actual_paths != set(expected):
            missing = sorted(set(expected) - actual_paths)
            extra = sorted(actual_paths - set(expected))
            raise BundleError(f"File inventory mismatch for {key}; missing={missing}, extra={extra}")
        for rel_text, entry in expected.items():
            file_path = model_dir / _safe_relative(rel_text)
            if file_path.is_symlink() or not file_path.is_file():
                raise BundleError(f"Unsafe or missing model file: {file_path}")
            if file_path.stat().st_size != int(entry.get("size", -1)):
                raise BundleError(f"Size mismatch for model file: {file_path}")
            if _sha256(file_path) != entry.get("sha256"):
                raise BundleError(f"SHA-256 mismatch for model file: {file_path}")
    return manifest


def verify_command(args: argparse.Namespace) -> int:
    manifest = verify_bundle(Path(args.bundle))
    print("Offline model bundle verified.")
    for key, spec in sorted(manifest["models"].items()):
        total = sum(int(item["size"]) for item in spec["files"])
        print(f"  {key}: {len(spec['files'])} files, {total} bytes, revision={spec['revision']}")
    return 0


def install_command(args: argparse.Namespace) -> int:
    bundle = Path(args.bundle).expanduser().resolve(strict=True)
    manifest = verify_bundle(bundle)
    target_root = Path(args.target).expanduser().resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    install_plan = []
    for key, spec in manifest["models"].items():
        source = bundle / _safe_relative(spec["path"])
        target = target_root / key
        if target.exists() and not args.replace:
            raise BundleError(f"Target already exists: {target}; use --replace after backing it up")
        install_plan.append((key, source, target))
    staging = Path(tempfile.mkdtemp(prefix=".patentshield-model-stage-", dir=target_root))
    backups: list[tuple[Path, Path]] = []
    installed: list[Path] = []
    try:
        for key, source, _target in install_plan:
            shutil.copytree(source, staging / key, symlinks=False)
        # Verify every staged copy against the bundle inventory before publishing.
        for key, source, _target in install_plan:
            _validate_model_dir(staging / key, key)
            if _list_model_files(staging / key) != _list_model_files(source):
                raise BundleError(f"Copied model changed during installation: {key}")
        for key, _source, target in install_plan:
            if target.exists():
                backup = target_root / f".{key}.backup-{int(time.time())}"
                os.replace(target, backup)
                backups.append((target, backup))
            os.replace(staging / key, target)
            installed.append(target)
        print(f"Installed verified model bundle into {target_root}")
        for target in installed:
            print(f"  {target}")
        if backups:
            print("Previous model directories retained as hidden backups for rollback.")
        return 0
    except Exception:
        for target in reversed(installed):
            if target.exists():
                shutil.rmtree(target)
        for original, backup in reversed(backups):
            if backup.exists():
                os.replace(backup, original)
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    exporter = commands.add_parser("export", help="materialize models and produce a hash manifest")
    exporter.add_argument("--bge-source", required=True)
    exporter.add_argument("--bge-revision", default="")
    exporter.add_argument("--reranker-source", required=True)
    exporter.add_argument("--reranker-revision", default="")
    exporter.add_argument("--output", required=True)
    exporter.set_defaults(handler=export_bundle)
    verifier = commands.add_parser("verify", help="verify manifest inventory and hashes")
    verifier.add_argument("--bundle", required=True)
    verifier.set_defaults(handler=verify_command)
    installer = commands.add_parser("install", help="install verified model files without network access")
    installer.add_argument("--bundle", required=True)
    installer.add_argument("--target", required=True)
    installer.add_argument("--replace", action="store_true", help="retain backups and replace existing model directories")
    installer.set_defaults(handler=install_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (BundleError, OSError, ValueError) as exc:
        print(f"model-bundle error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
