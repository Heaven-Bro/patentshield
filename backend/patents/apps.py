"""Django application configuration for patent records and intelligence."""

from __future__ import annotations

import logging

from django.apps import AppConfig
from django.conf import settings

logger = logging.getLogger(__name__)


class PatentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "patents"

    def ready(self) -> None:
        """Optionally preload BAAI/bge-large-en-v1.5 once in each API process.

        Model weights remain optional for migrations and lightweight test jobs.
        When preloading is enabled but ML extras or weights are absent, Django
        startup continues and the semantic endpoint returns a clear 503.
        """
        if not getattr(settings, "PATENTSHIELD_PUBLIC_PATENT_EMBEDDING_PRELOAD_ON_STARTUP", False):
            return
        try:
            from patents.embedding_services import PublicPatentEmbedder

            PublicPatentEmbedder.preload()
        except Exception:
            logger.exception("Unexpected error during public patent BGE-large startup preload.")
