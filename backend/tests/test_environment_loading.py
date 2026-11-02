"""Regression tests for the single supported dotenv configuration path."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from config.environment import load_project_environment


class ProjectEnvironmentLoadingTests(SimpleTestCase):
    def test_root_env_is_loaded_and_backend_fallback_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            backend = root / "backend"
            backend.mkdir()
            (root / ".env").write_text(
                "PATENTSHIELD_TEST_ROOT_SETTING=from-root\n",
                encoding="utf-8",
            )
            (backend / ".env").write_text(
                "PATENTSHIELD_TEST_BACKEND_SETTING=from-backend\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                load_project_environment(root)
                self.assertEqual(
                    os.environ.get("PATENTSHIELD_TEST_ROOT_SETTING"),
                    "from-root",
                )
                self.assertIsNone(
                    os.environ.get("PATENTSHIELD_TEST_BACKEND_SETTING")
                )
