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
        parser.add_argument(
            "--max-rows",
            type=int,
            default=None,
            help="Optional hard cap on scanned rows for staged backfills.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report proposed language updates without writing to the database.",
        )

    def handle(self, *args, **options) -> None:
        batch_size = options["batch_size"]
        if not 1 <= batch_size <= 5000:
            raise CommandError("--batch-size must be between 1 and 5000.")

        max_rows = options.get("max_rows")
        if max_rows is not None and max_rows < 1:
            raise CommandError("--max-rows must be a positive integer.")

        queryset = TextChunk.objects.filter(language_code="und").order_by("pk")
        tenant_id = options.get("tenant")
        if tenant_id:
            queryset = queryset.filter(tenant_id=tenant_id)
        if max_rows is not None:
            queryset = queryset[:max_rows]

        dry_run = bool(options.get("dry_run"))
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
                if not dry_run:
                    TextChunk.objects.bulk_update(
                        pending, ["language_code"], batch_size=batch_size
                    )
                updated += len(pending)
                pending.clear()
        if pending:
            if not dry_run:
                TextChunk.objects.bulk_update(
                    pending, ["language_code"], batch_size=batch_size
                )
            updated += len(pending)

        mode = "would update" if dry_run else "updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"Scanned {scanned} chunks; {mode} language metadata for {updated}."
            )
        )
