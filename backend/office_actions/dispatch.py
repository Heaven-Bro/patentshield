"""Durable dispatch reservations for Office Action extraction jobs."""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from audit.models import AuditLog
from office_actions.models import OfficeAction

logger = logging.getLogger(__name__)

QUEUE_LEASE = timedelta(minutes=10)
PROCESSING_LEASE = timedelta(minutes=45)
RETRY_LEASE = timedelta(minutes=15)


def enqueue_office_action(office_action_id, actor=None) -> tuple[str | None, str]:
    """Reserve and dispatch an extraction task.

    Returns (task_id, state). A recent reservation is reused. The persisted
    task ID is a fencing token checked by the worker at every state write.
    """
    from office_actions.tasks import process_office_action

    now = timezone.now()
    with transaction.atomic():
        try:
            action = OfficeAction.objects.select_for_update().get(pk=office_action_id)
        except OfficeAction.DoesNotExist:
            return None, "NOT_FOUND"

        if (
            action.extraction_status == OfficeAction.ExtractionStatus.EXTRACTED
            and action.parse_status in {
                OfficeAction.ParseStatus.COMPLETED,
                OfficeAction.ParseStatus.NEEDS_REVIEW,
            }
        ):
            return None, "ALREADY_COMPLETE"

        if (
            action.extraction_status == OfficeAction.ExtractionStatus.QUEUED
            and action.processing_reserved_at
            and action.processing_reserved_at > now - QUEUE_LEASE
            and action.processing_task_id
        ):
            return action.processing_task_id, "QUEUED"

        if (
            action.extraction_status == OfficeAction.ExtractionStatus.PROCESSING
            and action.processing_started_at
            and action.processing_started_at > now - PROCESSING_LEASE
            and action.processing_task_id
        ):
            return action.processing_task_id, "PROCESSING"

        if (
            action.extraction_status == OfficeAction.ExtractionStatus.RETRYING
            and action.processing_reserved_at
            and action.processing_reserved_at > now - RETRY_LEASE
            and action.processing_task_id
        ):
            return action.processing_task_id, "RETRYING"

        task_id = str(uuid.uuid4())
        action.extraction_status = OfficeAction.ExtractionStatus.QUEUED
        action.processing_task_id = task_id
        action.processing_reserved_at = now
        action.processing_started_at = None
        action.extraction_error_code = ""
        action.save(update_fields=(
            "extraction_status", "processing_task_id", "processing_reserved_at",
            "processing_started_at", "extraction_error_code", "updated_at",
        ))

    try:
        process_office_action.apply_async(
            args=[str(office_action_id)],
            kwargs={"reservation_id": task_id},
            task_id=task_id,
            queue="analysis",
        )
    except Exception as exc:
        # Only release the reservation we created. If another dispatch has
        # already superseded it, this failure must not clear the new task.
        with transaction.atomic():
            current = OfficeAction.objects.select_for_update().filter(
                pk=office_action_id,
                processing_task_id=task_id,
            ).first()
            if current:
                current.extraction_status = OfficeAction.ExtractionStatus.PENDING
                current.processing_task_id = ""
                current.processing_reserved_at = None
                current.processing_started_at = None
                current.extraction_error_code = "ENQUEUE_FAILED"
                current.save(update_fields=(
                    "extraction_status", "processing_task_id", "processing_reserved_at",
                    "processing_started_at", "extraction_error_code", "updated_at",
                ))
                try:
                    AuditLog.objects.create(
                        tenant_id=current.tenant_id,
                        actor_user_id=(
                            actor.pk if actor is not None and getattr(actor, "is_authenticated", False)
                            else None
                        ),
                        action="OFFICE_ACTION_PROCESS_ENQUEUE_FAILED",
                        resource_type="OfficeAction",
                        resource_id=current.pk,
                        outcome=AuditLog.Outcome.FAILURE,
                        metadata={"error_type": type(exc).__name__},
                    )
                except Exception:
                    logger.exception("Could not persist office-action enqueue failure audit event.")
        logger.warning(
            "Could not enqueue office-action processing for %s (%s).",
            office_action_id,
            type(exc).__name__,
        )
        return None, "ENQUEUE_FAILED"

    return task_id, "QUEUED"


def recoverable_office_action_ids(*, limit: int = 100) -> list[str]:
    """Return bounded IDs whose queued/running/retry lease has expired."""
    from django.db.models import Q

    now = timezone.now()
    candidates = OfficeAction.objects.filter(
        Q(
            extraction_status=OfficeAction.ExtractionStatus.QUEUED,
            processing_reserved_at__lt=now - QUEUE_LEASE,
        )
        | Q(
            extraction_status=OfficeAction.ExtractionStatus.PROCESSING,
            processing_started_at__lt=now - PROCESSING_LEASE,
        )
        | Q(
            extraction_status=OfficeAction.ExtractionStatus.PROCESSING,
            processing_started_at__isnull=True,
        )
        | Q(
            extraction_status=OfficeAction.ExtractionStatus.RETRYING,
            processing_reserved_at__lt=now - RETRY_LEASE,
        )
        | Q(
            extraction_status=OfficeAction.ExtractionStatus.RETRYING,
            processing_reserved_at__isnull=True,
        )
        | Q(
            extraction_status__in=(
                OfficeAction.ExtractionStatus.PENDING,
                OfficeAction.ExtractionStatus.FAILED,
            ),
            extraction_error_code="ENQUEUE_FAILED",
        )
    ).order_by("updated_at").values_list("pk", flat=True)[: max(1, min(int(limit), 500))]
    return [str(value) for value in candidates]
