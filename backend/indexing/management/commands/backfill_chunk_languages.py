"""Backfill language metadata for previously indexed text chunks."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from indexing.language import detect_language_code
from indexing.models import TextChunk


class Command(BaseCommand):
    help = "Detect CJK language metadata for existing patent text chunks marked 'und'."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--tenant", help="Optional tenant UUID to scope the backfill.")
        parser.add_argument(
            "--batch-size",
            type=int,
            default=500,
            help="Rows per update batch (default: 500).",
        )

    def handle(self, *args, **options) -> None:
        batch_size = options["batch_size"]
        if not 1 <= batch_size <= 5000:
            raise CommandError("--batch-size must be between 1 and 5000.")

        queryset = TextChunk.objects.filter(language_code="und").order_by("pk")
        tenant_id = options.get("tenant")
        if tenant_id:
            queryset = queryset.filter(tenant_id=tenant_id)

        pending: list[TextChunk] = []
        scanned = 0
        updated = 0
        for chunk in queryset.iterator(chunk_size=batch_size):
            scanned += 1
            detected = detect_language_code(chunk.text)
            if detected == chunk.language_code:
                continue
            chunk.language_code = detected
            pending.append(chunk)
            if len(pending) >= batch_size:
                TextChunk.objects.bulk_update(pending, ["language_code"], batch_size=batch_size)
                updated += len(pending)
                pending.clear()
        if pending:
            TextChunk.objects.bulk_update(pending, ["language_code"], batch_size=batch_size)
            updated += len(pending)

        self.stdout.write(
            self.style.SUCCESS(
                f"Scanned {scanned} chunks; updated language metadata for {updated}."
            )
        )
