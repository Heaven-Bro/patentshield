"""Idempotent landscape embedding, normalization, clustering and projection tasks."""
from __future__ import annotations

import hashlib
import logging

from celery import shared_task
from django.db import transaction
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
        LandscapeStudy.objects.filter(pk=study_id, tenant_id=tenant_id).update(
            status=LandscapeStudy.Status.FAILED,
            error_code="NO_PUBLICATIONS_SELECTED",
            completed_at=timezone.now(),
            updated_at=timezone.now(),
        )
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
        LandscapeStudy.objects.filter(pk=study_id, tenant_id=tenant_id).update(
            status=LandscapeStudy.Status.FAILED,
            error_code="LANDSCAPE_BUILD_FAILED",
            completed_at=timezone.now(),
        )
        return {"study_id": str(study_id), "status": "FAILED", "error_code": "LANDSCAPE_BUILD_FAILED"}
