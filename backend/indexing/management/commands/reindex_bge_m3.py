"""Rebuild active text-chunk vectors when the configured BGE-M3 identity changes.

The command preserves each document's currently active chunk generation until
its replacement generation has been embedded and atomically activated by the
existing ``generate_document_embeddings`` task. BM25 remains available while
reindexing is in progress.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from indexing.models import TextChunk
from patents.models import PatentDocumentVersion


class Command(BaseCommand):
    help = "Safely queue re-embedding for indexed documents using a stale BGE-M3 identity."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--tenant", help="Optional tenant UUID to scope the reindex.")
        scope = parser.add_mutually_exclusive_group(required=True)
        scope.add_argument(
            "--max-rows",
            type=int,
            help="Maximum document versions to queue; use for staged rollouts.",
        )
        scope.add_argument(
            "--all",
            action="store_true",
            help="Queue every eligible document version with stale active vectors.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List the number of stale document versions without dispatching tasks.",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Confirm dispatch without an interactive REINDEX prompt.",
        )

    def handle(self, *args, **options) -> None:
        max_rows = options.get("max_rows")
        if max_rows is not None and max_rows < 1:
            raise CommandError("--max-rows must be a positive integer.")

        from indexing.services import BGEEmbedder

        model_name, model_revision = BGEEmbedder.model_identity()
        stale_version_ids = (
            TextChunk.objects.filter(is_active=True, embedding__isnull=False)
            .exclude(
                embedding_model=model_name,
                embedding_revision=model_revision,
            )
            .values_list("document_version_id", flat=True)
            .distinct()
        )
        versions = PatentDocumentVersion.objects.filter(
            pk__in=stale_version_ids,
            extraction_status=PatentDocumentVersion.ExtractionStatus.EXTRACTED,
            text_indexing_status=PatentDocumentVersion.StageStatus.INDEXED,
        ).order_by("created_at", "pk")
        tenant_id = options.get("tenant")
        if tenant_id:
            versions = versions.filter(tenant_id=tenant_id)
        if max_rows is not None:
            versions = versions[:max_rows]

        version_ids = list(versions.values_list("pk", flat=True))
        self.stdout.write(
            f"Current embedding identity: {model_name}@{model_revision}\n"
            f"Document versions with stale active vectors: {len(version_ids)}"
        )
        if not version_ids or options.get("dry_run"):
            return

        if not options.get("yes"):
            answer = input(
                "This queues PDF extraction and BGE-M3 embedding work for the listed "
                "document versions. Type REINDEX to continue: "
            )
            if answer.strip() != "REINDEX":
                self.stdout.write(self.style.WARNING("Reindex cancelled; no tasks dispatched."))
                return

        from indexing.tasks import generate_document_embeddings

        queued = 0
        skipped = 0
        failures: list[str] = []
        for version_id in version_ids:
            task_id = str(uuid.uuid4())
            previous_status = None
            tenant_for_task = None
            with transaction.atomic():
                version = (
                    PatentDocumentVersion.objects.select_for_update()
                    .filter(pk=version_id)
                    .first()
                )
                if version is None or version.extraction_status != PatentDocumentVersion.ExtractionStatus.EXTRACTED:
                    skipped += 1
                    continue
                if (
                    version.text_indexing_status != PatentDocumentVersion.StageStatus.INDEXED
                    or version.text_indexing_task_id
                ):
                    skipped += 1
                    continue

                previous_status = version.text_indexing_status
                tenant_for_task = version.tenant_id
                version.text_indexing_status = PatentDocumentVersion.StageStatus.PENDING
                version.text_indexing_task_id = task_id
                version.text_indexing_started_at = None
                version.text_indexing_error_code = ""
                version.save(
                    update_fields=(
                        "text_indexing_status",
                        "text_indexing_task_id",
                        "text_indexing_started_at",
                        "text_indexing_error_code",
                    )
                )

            try:
                generate_document_embeddings.apply_async(
                    args=[str(version_id)],
                    queue="embedding",
                    task_id=task_id,
                )
                queued += 1
            except Exception as exc:
                PatentDocumentVersion.objects.filter(
                    pk=version_id,
                    tenant_id=tenant_for_task,
                    text_indexing_task_id=task_id,
                ).update(
                    text_indexing_status=previous_status,
                    text_indexing_task_id="",
                    text_indexing_started_at=None,
                    text_indexing_error_code="REINDEX_DISPATCH_FAILED",
                )
                failures.append(str(version_id))
                self.stderr.write(
                    self.style.ERROR(
                        f"Could not queue document version {version_id} ({type(exc).__name__})."
                    )
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"Queued {queued} reindex tasks; skipped {skipped}; dispatch failures {len(failures)}."
            )
        )
        if failures:
            raise CommandError(
                "Some reindex tasks could not be dispatched. Previous active vectors were preserved."
            )
