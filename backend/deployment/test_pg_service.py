"""Tests for safe creation of temporary libpq service configuration."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


def _resolve_script_path() -> Path:
    """Locate prepare_pg_service.py safely across host and container environments."""
    current = Path(__file__).resolve()

    # 1. Standard pathlib resolution relative to file structure
    candidates = [
        current.parents[2] / "scripts" / "prepare_pg_service.py",
        current.parents[1] / "scripts" / "prepare_pg_service.py",
        current.parent / "scripts" / "prepare_pg_service.py",
        Path.cwd() / "scripts" / "prepare_pg_service.py",
        Path.cwd() / "backend" / "scripts" / "prepare_pg_service.py",
        Path("/app/scripts/prepare_pg_service.py"),
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate

    # 2. Try Django settings only if configured
    try:
        from django.conf import settings
        if settings.configured:
            base = Path(settings.BASE_DIR)
            for p in [base, base.parent]:
                candidate = p / "scripts" / "prepare_pg_service.py"
                if candidate.is_file():
                    return candidate
    except Exception:
        pass

    return current.parents[2] / "scripts" / "prepare_pg_service.py"


SCRIPT = _resolve_script_path()


class PostgreSQLServiceFileTests(unittest.TestCase):
    def test_password_is_written_to_restricted_file_not_stdout(self) -> None:
        self.assertTrue(SCRIPT.is_file(), f"Script not found at {SCRIPT}.")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "pg_service.conf"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--output", str(target)],
                env={**os.environ, "DATABASE_URL": "postgresql://user:p%40ssword@db.internal:5432/patentshield?sslmode=verify-full"},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("p@ssword", result.stdout)
            self.assertNotIn("p@ssword", result.stderr)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            text = target.read_text(encoding="utf-8")
            self.assertIn('password="p@ssword"', text)
            self.assertIn('sslmode="verify-full"', text)

    def test_rejects_unsupported_query_options(self) -> None:
        self.assertTrue(SCRIPT.is_file(), f"Script not found at {SCRIPT}.")
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "pg_service.conf"
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--output", str(target)],
                env={**os.environ, "DATABASE_URL": "postgresql://user:secret@db.internal:5432/patentshield?sslmode=require&password=leak"},
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 2)
            self.assertNotIn("secret", result.stdout + result.stderr)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()