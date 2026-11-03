"""Idempotent landscape embedding, normalization, clustering and projection tasks."""
from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import timedelta

from celery import shared_task
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from indexing.services import BGEEmbedder
from landscapes.models import (
    LandscapeCluster,
    LandscapePublicationAssignee,
    LandscapePublicationEmbedding,
    LandscapePublicationTechnology,
    LandscapeStudy,
    LandscapeStudyPublication,
)
from landscapes.services import (
    audit_landscape_event,
    assign_technology_codes,
    describe_cluster,
    deterministic_kmeans,
    get_embedding_text,
    project_vectors_2d,
    resolve_assignees_for_publication,
    save_publication_embedding,
)

logger = logging.getLogger(__name__)


@shared_task(bind=True, acks_late=True, max_retries=2, default_retry_delay=45)
def build_landscape(self, study_id: str, tenant_id: str, force: bool = False) -> dict[str, object]:
    """Build a version-pinned landscape using local BGE-M3 embeddings.

    The tenant/resource relationship is revalidated within the task. All source
    fields come from canonical public provider records already synchronized into
    PatentShield. The task performs no outside provider request.
    """
    with transaction.atomic():
        study = LandscapeStudy.objects.select_for_update().filter(pk=study_id, tenant_id=tenant_id).first()
        if study is None:
            return {"study_id": study_id, "status": "NOT_FOUND"}
        if study.status == LandscapeStudy.Status.ARCHIVED:
            return {"study_id": study_id, "status": "ARCHIVED"}
        current_task_id = str(getattr(self.request, "id", "") or "")[:100]
        if study.status == LandscapeStudy.Status.BUILDING and study.built_by_task_id and study.built_by_task_id != current_task_id:
            return {"study_id": study_id, "status": "BUILDING", "reused": True}
        if study.status == LandscapeStudy.Status.READY and not force:
            return {"study_id": study_id, "status": "READY", "reused": True}
        study.status = LandscapeStudy.Status.BUILDING
        study.error_code = ""
        study.started_at = timezone.now()
        study.completed_at = None
        study.built_by_task_id = str(getattr(self.request, "id", "") or "")[:100]
        study.save(update_fields=("status", "error_code", "started_at", "completed_at", "built_by_task_id", "updated_at"))

    rows = list(
        LandscapeStudyPublication.objects.filter(tenant_id=tenant_id, study_id=study_id)
        .select_related("publication", "source_snapshot")
        .order_by("publication_id", "pk")
    )
    if not rows:
        now = timezone.now()
        with transaction.atomic():
            locked_study = LandscapeStudy.objects.select_for_update().filter(
                pk=study_id, tenant_id=tenant_id, status=LandscapeStudy.Status.BUILDING,
                built_by_task_id=str(getattr(self.request, "id", "") or "")[:100],
            ).first()
            if locked_study is None:
                return {"study_id": study_id, "status": "STALE_TASK_IGNORED"}
            locked_study.status = LandscapeStudy.Status.FAILED
            locked_study.error_code = "NO_PUBLICATIONS_SELECTED"
            locked_study.completed_at = now
            locked_study.save(update_fields=("status", "error_code", "completed_at", "updated_at"))
        return {"study_id": study_id, "status": "FAILED", "error_code": "NO_PUBLICATIONS_SELECTED"}

    try:
        vectors: list[list[float]] = []
        # Normalize entities and taxonomy links independently before model inference.
        for row in rows:
            if row.tenant_id != tenant_id or row.study.tenant_id != tenant_id:
                raise ValueError("Landscape publication tenant consistency check failed.")
            resolve_assignees_for_publication(row, row.study.created_by_id)
            assign_technology_codes(row)

        texts: list[str] = []
        digests: list[str] = []
        existing_embeddings: dict[str, LandscapePublicationEmbedding] = {
            str(item.study_publication_id): item
            for item in LandscapePublicationEmbedding.objects.filter(
                tenant_id=tenant_id,
                study_publication_id__in=[row.pk for row in rows],
            )
        }
        rows_to_embed = []
        for row in rows:
            text = get_embedding_text(row)
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            current = existing_embeddings.get(str(row.pk))
            if current is not None and current.text_sha256 == digest and current.embedding_model == BGEEmbedder.MODEL_NAME:
                vector = [float(value) for value in current.embedding]
                vectors.append(vector)
            else:
                rows_to_embed.append(row)
                texts.append(text)
                digests.append(digest)
                vectors.append([])

        if rows_to_embed:
            computed = BGEEmbedder.embed_texts(texts)
            if len(computed) != len(rows_to_embed):
                raise RuntimeError("Embedding provider returned an unexpected vector count.")
            index_by_id = {str(row.pk): index for index, row in enumerate(rows)}
            for row, text, digest, vector in zip(rows_to_embed, texts, digests, computed):
                if len(vector) != BGEEmbedder.DIMENSIONS:
                    raise RuntimeError("Landscape vector dimension does not match the configured BGE-M3 schema.")
                save_publication_embedding(row, vector, text)
                vectors[index_by_id[str(row.pk)]] = [float(value) for value in vector]

        if any(len(vector) != BGEEmbedder.DIMENSIONS for vector in vectors):
            raise RuntimeError("One or more landscape vectors are missing or malformed.")

        labels = deterministic_kmeans(vectors)
        coordinates, projection_method = project_vectors_2d(vectors)
        cluster_count = max(labels) + 1 if labels else 0

        # Publish a complete clustering result atomically. If any cluster or row
        # write fails, the previous complete projection remains intact on retry.
        with transaction.atomic():
            locked_study = LandscapeStudy.objects.select_for_update().filter(
                pk=study_id, tenant_id=tenant_id
            ).first()
            if locked_study is None:
                return {"study_id": study_id, "status": "NOT_FOUND"}
            current_task_id = str(getattr(self.request, "id", "") or "")[:100]
            if locked_study.status != LandscapeStudy.Status.BUILDING or (
                locked_study.built_by_task_id and locked_study.built_by_task_id != current_task_id
            ):
                return {"study_id": study_id, "status": "STALE_TASK_IGNORED"}

            # Existing publication snapshots and annotations remain untouched.
            LandscapeCluster.objects.filter(tenant_id=tenant_id, study_id=study_id).delete()
            cluster_rows: dict[int, LandscapeCluster] = {}
            for cluster_number in range(cluster_count):
                grouped = [rows[i] for i, label in enumerate(labels) if label == cluster_number]
                label, top_terms, top_codes, explanation = describe_cluster(grouped)
                cluster_rows[cluster_number] = LandscapeCluster.objects.create(
                    tenant_id=tenant_id,
                    study_id=study_id,
                    cluster_number=cluster_number,
                    label=label,
                    explanation=explanation,
                    top_terms=top_terms,
                    top_classification_codes=top_codes,
                    publication_count=len(grouped),
                    algorithm="deterministic-kmeans-v1",
                )

            for index, row in enumerate(rows):
                row.cluster = cluster_rows[labels[index]]
                row.projection_x = float(coordinates[index][0])
                row.projection_y = float(coordinates[index][1])
                row.projection_method = projection_method
                row.save(update_fields=("cluster", "projection_x", "projection_y", "projection_method"))

            assignee_count = LandscapePublicationAssignee.objects.filter(
                tenant_id=tenant_id, study_publication__study_id=study_id
            ).values("entity_id").distinct().count()
            taxonomy_count = LandscapePublicationTechnology.objects.filter(
                tenant_id=tenant_id, study_publication__study_id=study_id
            ).values("taxonomy_id").distinct().count()
            now = timezone.now()
            LandscapeStudy.objects.filter(pk=study_id, tenant_id=tenant_id).update(
                status=LandscapeStudy.Status.READY,
                error_code="",
                completed_at=now,
                algorithm_version="bge-m3-kmeans-v1",
                analysis_parameters={
                    **(locked_study.analysis_parameters or {}),
                    "embedding_model": BGEEmbedder.MODEL_NAME,
                    "embedding_dimensions": BGEEmbedder.DIMENSIONS,
                    "projection_method": projection_method,
                    "cluster_algorithm": "deterministic-kmeans-v1",
                    "cluster_count": cluster_count,
                    "publication_count": len(rows),
                    "assignee_entity_count": assignee_count,
                    "taxonomy_node_count": taxonomy_count,
                    "snapshot_mode": "immutable-study-publication-snapshots",
                    "completed_at": now.isoformat(),
                },
                updated_at=now,
            )
            audit_landscape_event(
                tenant_id=tenant_id,
                actor_id=locked_study.created_by_id,
                action="LANDSCAPE_BUILD_COMPLETED",
                resource_type="LandscapeStudy",
                resource_id=locked_study.pk,
                metadata={"publication_count": len(rows), "cluster_count": cluster_count, "projection_method": projection_method},
            )
            return {
                "study_id": str(study_id),
                "status": "READY",
                "publication_count": len(rows),
                "cluster_count": cluster_count,
                "projection_method": projection_method,
                "assignee_entity_count": assignee_count,
                "taxonomy_node_count": taxonomy_count,
            }
    except Exception as exc:
        logger.warning("Landscape build failed (study_id=%s, error_type=%s).", study_id, type(exc).__name__)
        if getattr(self.request, "retries", 0) < self.max_retries:
            # Keep the state BUILDING for the retry; retry is bounded.
            raise self.retry(exc=exc, countdown=45 * (getattr(self.request, "retries", 0) + 1))
        now = timezone.now()
        with transaction.atomic():
            locked_study = LandscapeStudy.objects.select_for_update().filter(
                pk=study_id, tenant_id=tenant_id, status=LandscapeStudy.Status.BUILDING,
                built_by_task_id=str(getattr(self.request, "id", "") or "")[:100],
            ).first()
            if locked_study is None:
                return {"study_id": study_id, "status": "STALE_TASK_IGNORED"}
            locked_study.status = LandscapeStudy.Status.FAILED
            locked_study.error_code = "LANDSCAPE_BUILD_FAILED"
            locked_study.completed_at = now
            locked_study.save(update_fields=("status", "error_code", "completed_at", "updated_at"))
        return {"study_id": str(study_id), "status": "FAILED", "error_code": "LANDSCAPE_BUILD_FAILED"}



def _mark_recovery_dispatch_failed(study_id: str, tenant_id: str, task_id: str) -> None:
    """Fail only the reservation created by this recovery attempt."""
    now = timezone.now()
    with transaction.atomic():
        study = LandscapeStudy.objects.select_for_update().filter(
            pk=study_id, tenant_id=tenant_id, status=LandscapeStudy.Status.BUILDING,
            built_by_task_id=task_id,
        ).first()
        if study is None:
            return
        study.status = LandscapeStudy.Status.FAILED
        study.error_code = "RECOVERY_QUEUE_FAILED"
        study.completed_at = now
        study.save(update_fields=("status", "error_code", "completed_at", "updated_at"))


def _dispatch_recovered_landscape(study_id: str, tenant_id: str, task_id: str) -> None:
    """Dispatch after the reservation commit; fence failure cleanup by task ID."""
    try:
        build_landscape.apply_async(
            args=[study_id, tenant_id],
            kwargs={"force": False},
            task_id=task_id,
            queue="embedding",
        )
    except Exception as exc:  # pragma: no cover - exercised in deployment integration tests
        logger.warning(
            "Stale landscape recovery dispatch failed (study_id=%s, error_type=%s).",
            study_id, type(exc).__name__,
        )
        _mark_recovery_dispatch_failed(study_id, tenant_id, task_id)


@shared_task(bind=True, acks_late=True, max_retries=1, default_retry_delay=60)
def recover_stale_landscape_builds(
    self, stale_after_minutes: int = 45, batch_size: int = 100
) -> dict[str, int]:
    """Requeue lost/abandoned landscape builds with durable task-ID fencing.

    The threshold is greater than the configured 30-minute Celery hard limit.
    A new task ID is persisted before dispatch, and cleanup on broker failure
    can only change the matching reservation.
    """
    stale_after_minutes = max(45, min(int(stale_after_minutes), 24 * 60))
    batch_size = max(1, min(int(batch_size), 250))
    now = timezone.now()
    cutoff = now - timedelta(minutes=stale_after_minutes)
    candidates = list(
        LandscapeStudy.objects.filter(
            status=LandscapeStudy.Status.BUILDING,
        ).filter(Q(started_at__isnull=True) | Q(started_at__lt=cutoff))
        .order_by("started_at", "pk")
        .values_list("pk", "tenant_id")[:batch_size]
    )

    reserved = 0
    for raw_study_id, raw_tenant_id in candidates:
        study_id = str(raw_study_id)
        tenant_id = str(raw_tenant_id)
        new_task_id = str(uuid.uuid4())
        should_dispatch = False
        with transaction.atomic():
            study = LandscapeStudy.objects.select_for_update(skip_locked=True).filter(
                pk=raw_study_id, tenant_id=raw_tenant_id, status=LandscapeStudy.Status.BUILDING,
            ).filter(Q(started_at__isnull=True) | Q(started_at__lt=cutoff)).first()
            if study is None:
                continue
            study.built_by_task_id = new_task_id
            study.started_at = now
            study.error_code = ""
            study.completed_at = None
            study.save(update_fields=("built_by_task_id", "started_at", "error_code", "completed_at", "updated_at"))
            transaction.on_commit(
                lambda sid=study_id, tid=tenant_id, task_id=new_task_id: _dispatch_recovered_landscape(sid, tid, task_id)
            )
            should_dispatch = True
        if should_dispatch:
            reserved += 1

    return {"scanned": len(candidates), "reserved": reserved, "stale_after_minutes": stale_after_minutes}
