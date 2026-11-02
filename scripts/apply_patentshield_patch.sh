#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'USAGE'
Usage:
  apply_patentshield_patch.sh apply <patch_changes.zip> <repository_root>
  apply_patentshield_patch.sh package <repository_root> <output_patch.zip>

"apply" safely overlays only the files in the changes ZIP.
"package" creates a changes-only ZIP using this script's embedded manifest.
USAGE
}

MODE="${1:-}"
if [[ "$MODE" == "apply" ]]; then
  [[ $# -eq 3 ]] || { usage >&2; exit 2; }
  ZIP_PATH="$(realpath "$2")"
  REPO_ROOT="$(realpath -m "$3")"
  mkdir -p "$REPO_ROOT"
  python3 - "$ZIP_PATH" "$REPO_ROOT" <<'PYCODE'
import os, shutil, stat, sys, tempfile, zipfile
from pathlib import Path, PurePosixPath

archive = Path(sys.argv[1]).resolve(strict=True)
root = Path(sys.argv[2]).resolve()
root.mkdir(parents=True, exist_ok=True)
seen = set()
with zipfile.ZipFile(archive) as zf:
    for info in zf.infolist():
        name = info.filename
        if info.is_dir():
            continue
        if name in seen:
            raise SystemExit(f"Duplicate archive member rejected: {name!r}")
        seen.add(name)
        if "\\" in name:
            raise SystemExit(f"Rejected non-POSIX archive path: {name!r}")
        rel = PurePosixPath(name)
        if rel.is_absolute() or not rel.parts or any(part in ("", ".", "..") for part in rel.parts):
            raise SystemExit(f"Rejected unsafe archive path: {name!r}")
        mode = (info.external_attr >> 16) & 0xFFFF
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise SystemExit(f"Rejected non-regular archive member: {name!r}")
        target = root.joinpath(*rel.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = target.parent.resolve()
        if resolved_parent != root and root not in resolved_parent.parents:
            raise SystemExit(f"Archive path traverses an external symlink: {name!r}")
        if target.is_symlink():
            raise SystemExit(f"Refusing to overwrite a symlink: {name!r}")
        fd, temp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".patch-tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as output, zf.open(info, "r") as source:
                shutil.copyfileobj(source, output, length=1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_name, target)
            safe_mode = mode & 0o755
            os.chmod(target, safe_mode or 0o644)
        except Exception:
            try: os.unlink(temp_name)
            except FileNotFoundError: pass
            raise
if not seen:
    raise SystemExit("Patch archive contains no files")
print(f"Applied {len(seen)} changed/new files to: {root}")
PYCODE
  exit 0
fi

if [[ "$MODE" == "package" ]]; then
  [[ $# -eq 3 ]] || { usage >&2; exit 2; }
  REPO_ROOT="$(realpath "$2")"
  OUT_PATH="$(realpath -m "$3")"
  python3 - "$REPO_ROOT" "$OUT_PATH" <<'PYCODE'
import os, stat, sys, zipfile
from pathlib import Path
root = Path(sys.argv[1]).resolve(strict=True)
out = Path(sys.argv[2]).resolve()
files = [
    '.env.example',
    'Dockerfile',
    'backend/ai_engine/guardrails.py',
    'backend/ai_engine/nodes.py',
    'backend/ai_engine/state.py',
    'backend/analyses/services.py',
    'backend/analyses/tasks.py',
    'backend/analyses/views.py',
    'backend/config/settings.py',
    'backend/fto/migrations/0002_productversion_ocr_extraction_statuses.py',
    'backend/fto/models.py',
    'backend/fto/services.py',
    'backend/fto/tasks.py',
    'backend/indexing/migrations/0003_embedding_identity_active_generation.py',
    'backend/indexing/models.py',
    'backend/indexing/pdf_extraction.py',
    'backend/indexing/services.py',
    'backend/indexing/tasks.py',
    'backend/ingestion/tasks.py',
    'backend/multilingual_search/services.py',
    'backend/multilingual_search/views.py',
    'backend/office_actions/migrations/0003_shared_ocr_extraction_statuses.py',
    'backend/office_actions/models.py',
    'backend/office_actions/tasks.py',
    'backend/office_actions/views.py',
    'backend/patents/ai_serializers.py',
    'backend/patents/ai_services.py',
    'backend/patents/ai_views.py',
    'backend/patents/apps.py',
    'backend/patents/embedding_services.py',
    'backend/patents/intelligence_models.py',
    'backend/patents/management/commands/generate_patent_embeddings.py',
    'backend/patents/migrations/0006_patentpublication_embedding_and_more.py',
    'backend/patents/migrations/0007_stage_status_embedding_identity_and_public_hnsw.py',
    'backend/patents/migrations/0008_index_stage_task_ids.py',
    'backend/patents/migrations/0009_index_stage_started_at.py',
    'backend/patents/models.py',
    'backend/patents/test_phase345.py',
    'backend/patents/urls.py',
    'backend/patents/views.py',
    'backend/retrieval/services.py',
    'backend/tenants/admin.py',
    'backend/tenants/migrations/0002_tenant_external_ai_processing_policy.py',
    'backend/tenants/models.py',
    'docker-compose.yml',
    'requirements-ml.in',
    'requirements-ml.txt',
    'requirements.in',
]
missing = [rel for rel in files if not (root / rel).is_file() or (root / rel).is_symlink()]
if missing:
    raise SystemExit("Missing or unsafe patch files:\\n" + "\\n".join(missing))
out.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
    for rel in files:
        source = root.joinpath(*Path(rel).parts).resolve(strict=True)
        if root not in source.parents:
            raise SystemExit(f"Path escapes repository root: {rel}")
        info = zipfile.ZipInfo(rel)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = (stat.S_IFREG | (stat.S_IMODE(source.stat().st_mode) & 0o755)) << 16
        zf.writestr(info, source.read_bytes())
print(f"Packaged {len(files)} changed/new files to: {out}")
PYCODE
  exit 0
fi

usage >&2
exit 2
