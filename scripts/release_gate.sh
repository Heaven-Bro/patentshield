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

printf '\n[1/8] Validate Docker Compose configuration\n'
docker compose config -q

printf '\n[2/8] Build and start PostgreSQL, Redis, API, and worker\n'
docker compose up -d --build postgres redis api worker

printf '\n[3/8] Apply database migrations\n'
docker compose exec -T api python manage.py migrate --noinput

printf '\n[4/8] Django system and migration drift checks\n'
docker compose exec -T api python manage.py check
docker compose exec -T api python manage.py makemigrations --check --dry-run

printf '\n[5/8] Django app regression suites\n'
docker compose exec -T api python manage.py test \
  accounts tenants patents ingestion indexing retrieval ai_engine analyses reports \
  --verbosity 1

printf '\n[6/8] Tenant isolation, release integration, retry, and SLA tests\n'
docker compose exec -T api pytest -q tests

printf '\n[7/8] Frontend lint and production build\n'
(
  cd frontend
  npm ci
  npm run lint
  npm run build
)

if [[ "${PATENTSHIELD_RUN_REAL_ML_SLA:-0}" == "1" ]]; then
  printf '\n[8/8] Real cached-model retrieval SLA\n'
  docker compose exec -T -e PATENTSHIELD_RUN_REAL_ML_SLA=1 worker \
    pytest -q tests/test_performance_benchmarks.py::test_retrieval_latency_sla
else
  printf '\n[8/8] Real-model retrieval SLA skipped (set PATENTSHIELD_RUN_REAL_ML_SLA=1 to enable)\n'
fi

printf '\nPatentShield release gate passed. Review the remaining manual checks in PHASE_6_README.md.\n'
