"""Deterministic tests for per-record Celery sync and stale-job selection."""

from __future__ import annotations

from datetime import date
import os
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from patents.intelligence_models import ConnectorSyncCursor, PatentPublication
from patents.tasks import (
    enqueue_stale_publications,
    enqueue_stale_publication_refreshes,
    refresh_publication_record,
)


class PatentRefreshTaskTests(TestCase):
    def setUp(self) -> None:
        self.publication = PatentPublication.objects.create(
            provider=PatentPublication.Provider.USPTO,
            provider_record_id="US20240123456A1",
            publication_number="US20240123456A1",
            application_number="17999999",
            jurisdiction="US",
            title="A safer connector",
            publication_date=date(2024, 7, 11),
            source_url="https://patentcenter.uspto.gov/applications/17999999",
        )

    def test_refresh_task_persists_successful_cursor(self) -> None:
        from patents.intelligence_services import refresh_publication_from_provider

        with patch(
            "patents.tasks.refresh_publication_from_provider",
            return_value=(self.publication, [], ""),
        ):
            result = refresh_publication_record.apply(
                args=(str(self.publication.pk),),
                task_id="refresh-success-1",
                throw=True,
            ).get()

        cursor = ConnectorSyncCursor.objects.get(
            provider="USPTO",
            scope_key=f"publication:{self.publication.pk}",
        )
        self.assertEqual(result["status"], ConnectorSyncCursor.Status.SUCCEEDED)
        self.assertEqual(cursor.status, ConnectorSyncCursor.Status.SUCCEEDED)
        self.assertEqual(cursor.records_saved, 1)
        self.assertEqual(cursor.active_task_id, "")
        self.assertIsNotNone(cursor.last_success_at)

    def test_duplicate_active_refresh_is_skipped(self) -> None:
        ConnectorSyncCursor.objects.create(
            provider="USPTO",
            scope_key=f"publication:{self.publication.pk}",
            status=ConnectorSyncCursor.Status.RUNNING,
            started_at=timezone.now(),
            last_attempt_at=timezone.now(),
            active_task_id="already-running-task",
        )
        with patch("patents.tasks.refresh_publication_from_provider") as refresh:
            result = refresh_publication_record.apply(
                args=(str(self.publication.pk),),
                task_id="duplicate-task",
                throw=True,
            ).get()

        refresh.assert_not_called()
        self.assertEqual(result["status"], "DUPLICATE_SKIPPED")

    def test_stale_publication_sweep_honors_dry_run(self) -> None:
        with patch("patents.tasks.refresh_publication_record.apply_async") as enqueue:
            count = enqueue_stale_publications(
                older_than_hours=1,
                limit=10,
                provider="USPTO",
                dry_run=True,
            )
        self.assertEqual(count, 1)
        enqueue.assert_not_called()

    def test_periodic_scheduler_is_disabled_without_explicit_opt_in(self) -> None:
        with patch.dict(os.environ, {"PATENTSHIELD_CONNECTOR_SYNC_ENABLED": "false"}):
            with patch("patents.tasks.enqueue_stale_publications") as enqueue:
                result = enqueue_stale_publication_refreshes.apply(
                    kwargs={"older_than_hours": 24, "limit": 25},
                    task_id="periodic-disabled-test",
                    throw=True,
                ).get()
        enqueue.assert_not_called()
        self.assertEqual(result, {"enqueued": 0, "disabled": 1})

    def test_sweep_enqueues_only_canonical_record_ids(self) -> None:
        with patch(
            "patents.tasks.refresh_publication_record.apply_async"
        ) as enqueue:
            count = enqueue_stale_publications(
                older_than_hours=1,
                limit=10,
                provider="USPTO",
            )

        self.assertEqual(count, 1)
        kwargs = enqueue.call_args.kwargs
        self.assertEqual(kwargs["args"], (str(self.publication.pk),))
        self.assertEqual(kwargs["queue"], "external_search")
