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
    task_routes={
        "ingestion.tasks.*": {"queue": "ingestion"},
        "indexing.tasks.extract_and_index_schematics": {"queue": "ingestion"},
        "indexing.tasks.generate_document_embeddings": {"queue": "embedding"},
        "ai_engine.tasks.*": {"queue": "analysis"},
        "analyses.tasks.*": {"queue": "analysis"},
        "retrieval.tasks.*": {"queue": "external_search"},
        "reports.tasks.generate_analysis_report": {"queue": "exports"},
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
)

app.autodiscover_tasks()
