"""Enqueue stale, public-source patent publication refresh jobs."""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from patents.tasks import enqueue_stale_publications


class Command(BaseCommand):
    help = "Queue stale USPTO/EPO canonical patent-publication records for refresh."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--provider",
            choices=("USPTO", "EPO"),
            default=None,
            help="Restrict refresh to a single provider.",
        )
        parser.add_argument(
            "--older-than-hours",
            type=int,
            default=24,
            help="Refresh records that have not synced in this many hours.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Maximum records to inspect (1-1000).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count stale records without sending tasks to Celery.",
        )

    def handle(self, *args, **options) -> None:
        try:
            count = enqueue_stale_publications(
                older_than_hours=options["older_than_hours"],
                limit=options["limit"],
                provider=options["provider"],
                dry_run=options["dry_run"],
            )
        except ValueError as exc:
            raise CommandError(str(exc)) from exc

        action = "would enqueue" if options["dry_run"] else "enqueued"
        self.stdout.write(
            self.style.SUCCESS(
                f"{action} {count} public patent refresh job(s)."
            )
        )
