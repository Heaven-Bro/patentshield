"""Static runtime-contract checks for the release gate."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from django.test import SimpleTestCase
from django.urls import resolve

from config.celery import app as celery_app
from analyses.routing import websocket_urlpatterns


def _repository_root() -> Path:
    """Find the repository root by its release assets, not a fixed depth."""
    test_path = Path(__file__).resolve()
    for candidate in test_path.parents:
        if (
            (candidate / "deploy/helm/patentshield/templates/api-deployment.yaml").is_file()
            and (candidate / "deploy/helm/patentshield/templates/worker-deployment.yaml").is_file()
            and (candidate / "docker-compose.yml").is_file()
        ):
            return candidate
    raise AssertionError(
        "Could not locate repository root containing Helm and Compose release assets."
    )


class ReleaseConfigurationTests(SimpleTestCase):
    """Fail early if critical API routes or Celery queues become unwired."""

    def test_required_api_routes_are_registered(self) -> None:
        analysis_id = uuid4()
        routes = (
            ("/health/live/", "health-live"),
            ("/health/ready/", "health-ready"),
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

    def test_health_and_worker_probes_are_configured(self) -> None:
        repository_root = _repository_root()
        api_template = (
            repository_root
            / "deploy/helm/patentshield/templates/api-deployment.yaml"
        ).read_text()
        worker_template = (
            repository_root
            / "deploy/helm/patentshield/templates/worker-deployment.yaml"
        ).read_text()
        compose = (repository_root / "docker-compose.yml").read_text()

        self.assertIn("path: /health/ready/", api_template)
        self.assertIn("path: /health/live/", api_template)
        self.assertIn("livenessProbe:", worker_template)
        self.assertIn("readinessProbe:", worker_template)
        self.assertIn("kill -0 1", worker_template)
        self.assertIn("celery -A config inspect ping", worker_template)
        self.assertIn("http://127.0.0.1:8000/health/ready/", compose)
        self.assertEqual(compose.count("celery -A config inspect ping"), 2)
