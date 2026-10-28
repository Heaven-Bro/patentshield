"""Tests for safe creation of temporary libpq service configuration."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "prepare_pg_service.py"


class PostgreSQLServiceFileTests(unittest.TestCase):
    def test_password_is_written_to_restricted_file_not_stdout(self) -> None:
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
