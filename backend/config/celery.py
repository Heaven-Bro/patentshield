"""Celery application and queue routing for PatentShield."""

from __future__ import annotations

import os

from celery import Celery
from kombu import Queue

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

app = Celery("patentshield")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.conf.update(
    task_queues=(
        Queue("ingestion", routing_key="ingestion"),
        Queue("embedding", routing_key="embedding"),
        Queue("analysis", routing_key="analysis"),
        Queue("external_search", routing_key="external_search"),
        Queue("exports", routing_key="exports"),
    ),
    imports=("reports.claim_chart_tasks", "patents.surveillance_tasks", "office_actions.tasks", "fto.tasks", "landscapes.tasks"),
    task_routes={
        "ingestion.tasks.*": {"queue": "ingestion"},
        "indexing.tasks.extract_and_index_schematics": {"queue": "ingestion"},
        "indexing.tasks.generate_document_embeddings": {"queue": "embedding"},
        "ai_engine.tasks.*": {"queue": "analysis"},
        "analyses.tasks.*": {"queue": "analysis"},
        "retrieval.tasks.*": {"queue": "external_search"},
        "reports.tasks.generate_analysis_report": {"queue": "exports"},
        "reports.claim_chart_tasks.*": {"queue": "exports"},
        "patents.surveillance_tasks.*": {"queue": "external_search"},
        "office_actions.tasks.*": {"queue": "analysis"},
        "fto.tasks.*": {"queue": "ingestion"},
        "landscapes.tasks.*": {"queue": "embedding"},
    },
    task_default_queue="ingestion",
    task_default_routing_key="ingestion",
    task_time_limit=1800,
    task_soft_time_limit=1500,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        # Both are opt-in via PATENTSHIELD_SURVEILLANCE_ENABLED. Jobs use
        # database checkpoints, so duplicate scheduler dispatch is harmless.
        "surveillance-enqueue-due-watchlists": {
            "task": "patents.surveillance_tasks.enqueue_due_watchlists",
            "schedule": 300.0,
            "options": {"queue": "external_search"},
        },
        "surveillance-recover-deliveries": {
            "task": "patents.surveillance_tasks.recover_pending_deliveries",
            "schedule": 60.0,
            "options": {"queue": "external_search"},
        },
    },
)

app.autodiscover_tasks()
