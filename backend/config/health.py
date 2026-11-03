"""Low-disclosure process liveness and dependency readiness endpoints."""
from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.http import require_GET

logger = logging.getLogger(__name__)


def _log_probe_failure(dependency: str, exc: Exception) -> None:
    logger.warning(
        "Readiness check failed (dependency=%s, error_type=%s).",
        dependency,
        type(exc).__name__,
    )


def _database_ready() -> bool:
    """Run SELECT 1 on a short-lived connection with bounded driver timeouts."""
    probe_connection: Any | None = None
    try:
        params = connection.get_connection_params()
        params["connect_timeout"] = 2
        existing_options = str(params.get("options") or "").strip()
        params["options"] = f"{existing_options} -c statement_timeout=1500ms".strip()
        probe_connection = connection.Database.connect(**params)
        with probe_connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            return cursor.fetchone() == (1,)
    except Exception as exc:
        _log_probe_failure("database", exc)
        return False
    finally:
        if probe_connection is not None:
            try:
                probe_connection.close()
            except Exception as exc:
                _log_probe_failure("database-close", exc)


def _redis_url_ready(url: str) -> bool:
    """Ping one Redis endpoint with bounded connect/read timeouts."""
    client = None
    try:
        from redis import Redis

        client = Redis.from_url(
            url,
            socket_connect_timeout=1,
            socket_timeout=1,
        )
        return bool(client.ping())
    except Exception as exc:
        _log_probe_failure("redis", exc)
        return False
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                _log_probe_failure("redis-close", exc)


def _redis_dependencies_ready() -> bool:
    """Check the broker and Channels Redis endpoints once each."""
    urls = dict.fromkeys(
        (settings.CELERY_BROKER_URL, settings.CHANNELS_REDIS_URL)
    )
    return all(_redis_url_ready(url) for url in urls)


@require_GET
def health_live(_request) -> JsonResponse:
    """Report only that Django can serve an HTTP request."""
    return JsonResponse({"status": "alive"})


@require_GET
def health_ready(_request) -> JsonResponse:
    """Report generic dependency status without exposing connection details."""
    database_ok = _database_ready()
    redis_ok = _redis_dependencies_ready()
    ready = database_ok and redis_ok
    payload = {
        "status": "ready" if ready else "unavailable",
        "checks": {
            "database": "ok" if database_ok else "unavailable",
            "redis": "ok" if redis_ok else "unavailable",
        },
    }
    return JsonResponse(payload, status=200 if ready else 503)
