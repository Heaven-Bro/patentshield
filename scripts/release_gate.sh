#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

# Enable pytest/pytest-django only in the local release-gate API image. The
# production image remains lean by default when this variable is not set.
export PATENTSHIELD_BUILD_DEV_DEPS="${PATENTSHIELD_BUILD_DEV_DEPS:-1}"
if [[ "${PATENTSHIELD_RUN_REAL_ML_SLA:-0}" == "1" ]]; then
  # Real inference runs inside the worker, where Hugging Face cache storage
  # is mounted. Install ML dependencies only for this explicitly requested run.
  export PATENTSHIELD_BUILD_ML_WORKER="${PATENTSHIELD_BUILD_ML_WORKER:-1}"
fi

printf '\n[1/9] Verify pinned/hash-locked dependencies and repository hygiene\n'
python scripts/verify_dependency_manifest.py --require-hashes
python scripts/secure_repository_hygiene.py --strict

printf '\n[2/9] Validate Docker Compose configuration\n'
docker compose config -q

printf '\n[3/9] Build and start PostgreSQL, Redis, API, and worker\n'
docker compose up -d --build postgres redis api worker

printf '\n[4/9] Apply database migrations\n'
docker compose exec -T api python manage.py migrate --noinput

printf '\n[5/9] Django system and migration drift checks\n'
docker compose exec -T api python manage.py check
docker compose exec -T api python manage.py makemigrations --check --dry-run

printf '\n[6/9] Django app regression suites\n'
docker compose exec -T api python manage.py test \
  accounts tenants audit patents ingestion indexing retrieval ai_engine analyses reports \
  office_actions multilingual_search fto landscapes enterprise_identity \
  --verbosity 1

printf '\n[7/9] Tenant isolation, release integration, retry, and SLA tests\n'
docker compose exec -T api pytest -q tests

printf '\n[8/9] Frontend lint and production build\n'
(
  cd frontend
  npm ci
  npm run lint
  npm run build
)

if [[ "${PATENTSHIELD_RUN_REAL_ML_SLA:-0}" == "1" ]]; then
  printf '\n[9/9] Real cached-model retrieval SLA\n'
  docker compose exec -T -e PATENTSHIELD_RUN_REAL_ML_SLA=1 worker \
    pytest -q tests/test_performance_benchmarks.py::test_retrieval_latency_sla
else
  printf '\n[9/9] Real-model retrieval SLA skipped (set PATENTSHIELD_RUN_REAL_ML_SLA=1 to enable)\n'
fi

printf '\nPatentShield release gate passed. Review phase-specific manual checks under docs/PHASE_3 through docs/PHASE_8.\n'
