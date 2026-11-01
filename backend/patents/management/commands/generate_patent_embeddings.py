"""Backfill 1,024-dimensional BAAI/bge-large-en-v1.5 embeddings for public patents.

Examples:
    python manage.py generate_patent_embeddings
    python manage.py generate_patent_embeddings --batch-size 32 --limit 100

Rows are locked and updated in bounded transactions so concurrent invocations
can safely skip records currently being processed by another worker.
"""

from __future__ import annotations

import logging
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction, models

from patents.embedding_services import PublicPatentEmbedder, PublicPatentModelUnavailable
from patents.intelligence_models import PatentPublication

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Generate BAAI/bge-large-en-v1.5 (1,024-d) embeddings for public patent records."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--batch-size",
            type=int,
            default=32,
            help="Number of patents encoded and bulk-updated per batch (1-64).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=None,
            help="Optional maximum number of records to embed in this run.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        batch_size = options["batch_size"]
        limit = options["limit"]
        if not 1 <= batch_size <= 64:
            raise CommandError("--batch-size must be between 1 and 64.")
        if limit is not None and limit < 1:
            raise CommandError("--limit must be at least 1.")

        model_name, model_revision = PublicPatentEmbedder.model_identity()
        missing_qs = PatentPublication.objects.filter(
            models.Q(embedding__isnull=True)
            | ~models.Q(embedding_model=model_name)
            | ~models.Q(embedding_revision=model_revision)
        )
        missing = missing_qs.distinct().count()
        target = min(missing, limit) if limit is not None else missing
        if target == 0:
            self.stdout.write(self.style.SUCCESS("No public patent records need embeddings."))
            return

        self.stdout.write(
            f"Generating {model_name}@{model_revision} embeddings for up to {target:,} of {missing:,} records "
            f"(batch size {batch_size:,})."
        )

        processed = 0
        last_pk = None
        try:
            while processed < target:
                with transaction.atomic():
                    queryset = PatentPublication.objects.filter(
                        models.Q(embedding__isnull=True)
                        | ~models.Q(embedding_model=model_name)
                        | ~models.Q(embedding_revision=model_revision)
                    )
                    if last_pk is not None:
                        queryset = queryset.filter(pk__gt=last_pk)
                    records = list(
                        queryset.order_by("pk")
                        .select_for_update(skip_locked=True)[: min(batch_size, target - processed)]
                    )
                    if not records:
                        break

                    texts = [
                        "\n\n".join(
                            part.strip()
                            for part in (record.title or "", record.abstract or "")
                            if part and part.strip()
                        )
                        for record in records
                    ]
                    # A title is mandatory in the canonical model, but retain a
                    # clear fail-fast guard for hand-edited/legacy database rows.
                    if any(not text for text in texts):
                        raise CommandError(
                            "A selected patent has no title/abstract text to embed. "
                            "Correct the source record and rerun the command."
                        )
                    vectors = PublicPatentEmbedder.embed_texts(texts, batch_size=batch_size)
                    for record, vector in zip(records, vectors, strict=True):
                        if len(vector) != PublicPatentEmbedder.DIMENSIONS:
                            raise CommandError(
                                "BGE-large generated a vector with an unexpected dimension."
                            )
                        record.embedding = vector
                        record.embedding_model = model_name
                        record.embedding_revision = model_revision
                    PatentPublication.objects.bulk_update(
                        records,
                        ["embedding", "embedding_model", "embedding_revision"],
                        batch_size=batch_size,
                    )
                    last_pk = records[-1].pk
                    processed += len(records)
                    remaining = max(0, missing - processed)
                    logger.info(
                        "Public patent embeddings generated: batch=%d processed=%d remaining~=%d",
                        len(records),
                        processed,
                        remaining,
                    )
                    self.stdout.write(
                        f"Embedded {processed:,}/{target:,} records "
                        f"(batch {len(records):,}; approximately {remaining:,} still missing)."
                    )
        except PublicPatentModelUnavailable as exc:
            raise CommandError(str(exc)) from exc
        except Exception:
            logger.exception("Public patent embedding generation failed after %d records.", processed)
            raise

        still_missing = PatentPublication.objects.filter(
            models.Q(embedding__isnull=True)
            | ~models.Q(embedding_model=model_name)
            | ~models.Q(embedding_revision=model_revision)
        ).count()
        if processed == 0 and missing:
            self.stdout.write(
                self.style.WARNING(
                    "No records were acquired. Another embedding process may hold the rows; rerun later."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Embedding run complete: {processed:,} updated; "
                    f"{still_missing:,} records remain without embeddings."
                )
            )
