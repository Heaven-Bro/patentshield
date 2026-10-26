"""Static runtime-contract checks for the release gate."""

from __future__ import annotations

from uuid import uuid4

from django.test import SimpleTestCase
from django.urls import resolve

from config.celery import app as celery_app
from analyses.routing import websocket_urlpatterns


class ReleaseConfigurationTests(SimpleTestCase):
    """Fail early if critical API routes or Celery queues become unwired."""

    def test_required_api_routes_are_registered(self) -> None:
        analysis_id = uuid4()
        routes = (
            ("/api/v1/auth/me/", "me"),
            ("/api/v1/tenants/", "tenant-list"),
            ("/api/v1/patents/upload/", "patent-upload"),
            (f"/api/v1/analyses/{analysis_id}/", "analysis-detail"),
            (f"/api/v1/reports/{analysis_id}/status/", "report-status"),
            (f"/api/v1/reports/{analysis_id}/pdf/", "report-artifact"),
            (f"/api/v1/reports/{analysis_id}/json/", "report-artifact"),
        )
        for path, expected_name in routes:
            with self.subTest(path=path):
                match = resolve(path)
                self.assertEqual(match.url_name, expected_name)

    def test_websocket_analysis_progress_pattern_is_registered(self) -> None:
        route_text = "ws/v1/analyses/11111111-2222-3333-4444-555555555555/"
        has_route = any(
            pattern.pattern.regex.match(route_text)
            for pattern in websocket_urlpatterns
        )
        self.assertTrue(has_route)

    def test_all_celery_queues_and_task_routes_exist(self) -> None:
        required_queues = {
            "ingestion",
            "embedding",
            "analysis",
            "external_search",
            "exports",
        }
        configured_queues = {
            queue.name for queue in (celery_app.conf.task_queues or ())
        }
        self.assertTrue(required_queues.issubset(configured_queues))

        routes = celery_app.conf.task_routes
        self.assertEqual(routes["ingestion.tasks.*"]["queue"], "ingestion")
        self.assertEqual(
            routes["indexing.tasks.generate_document_embeddings"]["queue"],
            "embedding",
        )
        self.assertEqual(routes["analyses.tasks.*"]["queue"], "analysis")
        self.assertEqual(
            routes["reports.tasks.generate_analysis_report"]["queue"],
            "exports",
        )
