"""Celery jobs for idempotent public patent-source synchronization."""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import Any

from celery import shared_task
from django.db import DatabaseError, transaction
from django.db.models import Q
from django.utils import timezone

from patents.connectors.base import PatentConnectorError
from patents.intelligence_models import ConnectorSyncCursor, PatentPublication
from patents.intelligence_services import refresh_publication_from_provider

logger = logging.getLogger(__name__)
STALE_RUNNING_AFTER = timedelta(minutes=45)


def _cursor_scope(publication: PatentPublication) -> str:
    return f"publication:{publication.pk}"


@shared_task(
    bind=True,
    max_retries=3,
    queue="external_search",
    name="patents.tasks.refresh_publication_record",
)
def refresh_publication_record(self, publication_id: str) -> dict[str, Any]:
    """Refresh one canonical record, suppressing duplicate active task IDs."""
    publication = PatentPublication.objects.filter(pk=publication_id).first()
    if publication is None:
        return {"status": "NOT_FOUND", "publication_id": publication_id}

    now = timezone.now()
    scope_key = _cursor_scope(publication)
    task_id = str(getattr(self.request, "id", "") or "")
    if not task_id:
        # Direct synchronous invocation is mainly used in tests/operations.
        task_id = f"sync-{publication.pk}-{int(now.timestamp())}"

    with transaction.atomic():
        ConnectorSyncCursor.objects.get_or_create(
            provider=publication.provider,
            scope_key=scope_key,
        )
        cursor = (
            ConnectorSyncCursor.objects.select_for_update()
            .get(provider=publication.provider, scope_key=scope_key)
        )
        active_is_fresh = bool(
            cursor.status == ConnectorSyncCursor.Status.RUNNING
            and cursor.last_attempt_at is not None
            and cursor.last_attempt_at >= now - STALE_RUNNING_AFTER
        )
        if (
            active_is_fresh
            and cursor.active_task_id
            and cursor.active_task_id != task_id
        ):
            return {
                "status": "DUPLICATE_SKIPPED",
                "publication_id": str(publication.pk),
            }

        cursor.status = ConnectorSyncCursor.Status.RUNNING
        cursor.started_at = now
        cursor.last_attempt_at = now
        cursor.active_task_id = task_id
        cursor.error_code = ""
        cursor.save(
            update_fields=(
                "status",
                "started_at",
                "last_attempt_at",
                "active_task_id",
                "error_code",
                "updated_at",
            )
        )

    try:
        refreshed, events, error_code = refresh_publication_from_provider(
            publication
        )
        finished_at = timezone.now()
        with transaction.atomic():
            cursor = (
                ConnectorSyncCursor.objects.select_for_update()
                .get(provider=publication.provider, scope_key=scope_key)
            )
            # A stale worker cannot finalize a cursor claimed by a newer job.
            if cursor.active_task_id != task_id:
                return {
                    "status": "STALE_WORKER_IGNORED",
                    "publication_id": str(publication.pk),
                }
            cursor.records_seen = max(cursor.records_seen, 1)
            cursor.records_saved = 1 if refreshed is not None else 0
            cursor.checkpoint = (
                refreshed.source_payload_hash if refreshed is not None else ""
            )[:500]
            cursor.status = (
                ConnectorSyncCursor.Status.SUCCEEDED
                if not error_code
                else ConnectorSyncCursor.Status.PARTIAL
            )
            cursor.error_code = error_code[:80]
            if not error_code:
                cursor.last_success_at = finished_at
            cursor.active_task_id = ""
            cursor.save(
                update_fields=(
                    "records_seen",
                    "records_saved",
                    "checkpoint",
                    "status",
                    "error_code",
                    "last_success_at",
                    "active_task_id",
                    "updated_at",
                )
            )
        return {
            "status": cursor.status,
            "publication_id": str(publication.pk),
            "records_saved": cursor.records_saved,
            "legal_events_seen": len(events),
            "error_code": cursor.error_code,
        }
    except Exception as exc:
        retry_after = (
            exc.retry_after_seconds
            if isinstance(exc, PatentConnectorError)
            else None
        )
        error_code = (
            exc.code
            if isinstance(exc, PatentConnectorError)
            else "PERSISTENCE_OR_CONNECTOR_ERROR"
        )
        retries = int(getattr(self.request, "retries", 0) or 0)
        retryable = retries < self.max_retries
        retry_delay = int(
            min(3_600, max(retry_after or 0, 30 * (2 ** retries)))
        )
        try:
            with transaction.atomic():
                cursor = (
                    ConnectorSyncCursor.objects.select_for_update()
                    .get(provider=publication.provider, scope_key=scope_key)
                )
                if cursor.active_task_id == task_id:
                    cursor.status = (
                        ConnectorSyncCursor.Status.RUNNING
                        if retryable
                        else ConnectorSyncCursor.Status.FAILED
                    )
                    cursor.error_code = (
                        "RETRY_SCHEDULED" if retryable else error_code[:80]
                    )
                    if not retryable:
                        cursor.active_task_id = ""
                    cursor.save(
                        update_fields=(
                            "status",
                            "error_code",
                            "active_task_id",
                            "updated_at",
                        )
                    )
        except DatabaseError:
            logger.exception(
                "Could not persist connector failure state for publication %s.",
                publication.pk,
            )

        if retryable:
            raise self.retry(exc=exc, countdown=retry_delay)
        logger.error(
            "Patent refresh exhausted retries: publication=%s code=%s",
            publication.pk,
            error_code,
        )
        raise


def enqueue_stale_publications(
    *,
    older_than_hours: int = 24,
    limit: int = 100,
    provider: str | None = None,
    dry_run: bool = False,
) -> int:
    """Enqueue stale records by public identifier, without duplicate active work."""
    if isinstance(older_than_hours, bool) or not 1 <= older_than_hours <= 8_760:
        raise ValueError("older_than_hours must be between 1 and 8760.")
    if isinstance(limit, bool) or not 1 <= limit <= 1_000:
        raise ValueError("limit must be between 1 and 1000.")
    normalized_provider = provider.upper() if provider else None
    if normalized_provider and normalized_provider not in {"USPTO", "EPO"}:
        raise ValueError("provider must be USPTO or EPO.")

    now = timezone.now()
    cutoff = now - timedelta(hours=older_than_hours)
    queryset = PatentPublication.objects.filter(
        Q(last_synced_at__isnull=True) | Q(last_synced_at__lt=cutoff)
    ).filter(
        (
            Q(publication_number__isnull=False) & ~Q(publication_number="")
        )
        | (
            Q(application_number__isnull=False)
            & ~Q(application_number="")
        )
    )
    if normalized_provider:
        queryset = queryset.filter(provider=normalized_provider)

    publications = list(
        queryset.order_by("last_synced_at", "first_seen_at")[:limit]
    )
    enqueued = 0
    active_cutoff = now - STALE_RUNNING_AFTER
    for publication in publications:
        cursor = ConnectorSyncCursor.objects.filter(
            provider=publication.provider,
            scope_key=_cursor_scope(publication),
        ).first()
        if cursor and cursor.last_attempt_at:
            if (
                cursor.status == ConnectorSyncCursor.Status.RUNNING
                and cursor.last_attempt_at >= active_cutoff
            ):
                continue
            if (
                cursor.status in {
                    ConnectorSyncCursor.Status.PARTIAL,
                    ConnectorSyncCursor.Status.FAILED,
                }
                and cursor.last_attempt_at >= cutoff
            ):
                continue
        enqueued += 1
        if not dry_run:
            refresh_publication_record.apply_async(
                args=(str(publication.pk),),
                queue="external_search",
            )
    return enqueued


@shared_task(
    bind=True,
    queue="external_search",
    name="patents.tasks.enqueue_stale_publication_refreshes",
)
def enqueue_stale_publication_refreshes(
    self,
    older_than_hours: int = 24,
    limit: int = 100,
    provider: str | None = None,
) -> dict[str, int]:
    """Periodic task: enqueue per-publication refresh jobs and return a count."""
    enabled = os.getenv(
        "PATENTSHIELD_CONNECTOR_SYNC_ENABLED", "False"
    ).strip().casefold() in {"1", "true", "yes", "on"}
    if not enabled:
        return {"enqueued": 0, "disabled": 1}
    count = enqueue_stale_publications(
        older_than_hours=older_than_hours,
        limit=limit,
        provider=provider,
    )
    return {"enqueued": count}
