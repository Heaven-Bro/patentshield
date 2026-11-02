"""History-aware release hygiene regression tests (uses a temporary Git repo)."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from secure_repository_hygiene import list_historical_sensitive_paths


class HistoricalSecretPathTests(unittest.TestCase):
    def test_deleted_env_file_is_still_detected_in_git_history(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "audit-test@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Audit Test"],
                cwd=root,
                check=True,
            )

            (root / ".env").write_text(
                "DJANGO_SECRET_KEY=not-a-real-secret-for-unit-test\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", ".env"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "add local environment fixture"],
                cwd=root,
                check=True,
            )

            (root / ".env").unlink()
            media_dir = root / "backend" / "media" / "tenant-1"
            media_dir.mkdir(parents=True)
            (media_dir / "patent.pdf").write_bytes(b"redacted test fixture")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "add tenant media fixture"],
                cwd=root,
                check=True,
            )
            shutil.rmtree(root / "backend")
            (root / "README.md").write_text("removed local env file\n", encoding="utf-8")
            subprocess.run(["git", "add", "-A"], cwd=root, check=True)
            subprocess.run(
                ["git", "commit", "-qm", "remove local environment fixture"],
                cwd=root,
                check=True,
            )

            paths = list_historical_sensitive_paths(root)
            self.assertIn(".env", paths)
            self.assertTrue(any(path.startswith("backend/media/") for path in paths))


if __name__ == "__main__":
    unittest.main(verbosity=2)
