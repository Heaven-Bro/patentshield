"""Canonical dotenv loading for local PatentShield development."""
from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


def load_project_environment(repository_root: str | Path) -> None:
    """Load only ``<repository_root>/.env`` without overriding process env.

    A backend-local fallback is intentionally unsupported. Compose and native
    development use the repository-root template, while production injects
    configuration from its secret manager/orchestrator.
    """
    root = Path(repository_root).resolve()
    load_dotenv(root / ".env", override=False)
