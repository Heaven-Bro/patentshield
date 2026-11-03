#!/usr/bin/env bash
set -Eeuo pipefail

# Run on a connected staging workstation matching the target Python/OS/CPU.
# Hashes are checked before pip can resolve or download any artifact.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${WHEELHOUSE_DIR:-$ROOT/offline/wheelhouse}"
INCLUDE_ML_DEPS="${INCLUDE_ML_DEPS:-0}"
INCLUDE_DEV_DEPS="${INCLUDE_DEV_DEPS:-0}"

mkdir -p "$(dirname "$DEST")"
if [[ -e "$DEST" && -n "$(find "$DEST" -mindepth 1 -maxdepth 1 -print -quit)" ]]; then
  echo "Wheelhouse destination must be empty or absent: $DEST" >&2
  exit 2
fi
mkdir -p "$DEST"

requirements=("$ROOT/requirements.txt")
if [[ "$INCLUDE_ML_DEPS" == "1" ]]; then requirements+=("$ROOT/requirements-ml.txt"); fi
if [[ "$INCLUDE_DEV_DEPS" == "1" ]]; then requirements+=("$ROOT/requirements-dev.txt"); fi
for lock in "${requirements[@]}"; do
  python "$ROOT/scripts/check_requirement_hashes.py" "$lock"
done

for lock in "${requirements[@]}"; do
  python -m pip download --require-hashes -r "$lock" --dest "$DEST"
done

python - "$DEST" "${requirements[@]}" <<'PY'
import hashlib, json, platform, sys, sysconfig
from pathlib import Path
root = Path(sys.argv[1])
locks = [Path(value) for value in sys.argv[2:]]
manifest = {
    "schema_version": 1,
    "python_version": platform.python_version(),
    "implementation": platform.python_implementation(),
    "system": platform.system(),
    "machine": platform.machine(),
    "platform_tag": sysconfig.get_platform(),
    "requirements": [
        {"file": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        for p in locks
    ],
    "artifacts": sorted(p.name for p in root.iterdir() if p.is_file()),
}
(root / "wheelhouse-manifest.json").write_text(
    json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY
(
  cd "$DEST"
  find . -maxdepth 1 -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum > SHA256SUMS
  sha256sum -c SHA256SUMS
)
printf 'Offline wheelhouse prepared at %s\n' "$DEST"
printf 'Target compatibility: Python %s / %s / %s\n' "$(python -V 2>&1)" "$(uname -s)" "$(uname -m)"
