#!/usr/bin/env python3
"""Small stdlib regression tests for the release-archive verifier."""
from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from verify_release_archive import reason_for_member, verify_archive


class ReleaseArchivePolicyTests(unittest.TestCase):
    def test_env_example_allowed_but_real_env_files_rejected(self):
        self.assertIsNone(reason_for_member("PatentShield/.env.example"))
        for name in (".env", "backend/.env", "frontend/.env.production", "x/.env/private.txt"):
            with self.subTest(name=name):
                self.assertIsNotNone(reason_for_member(name))

    def test_generated_directories_and_bytecode_are_rejected(self):
        for name in (
            "repo/.git/config", "repo/__pycache__/x.pyc", "repo/node_modules/pkg/index.js",
            "repo/dist/index.js", "repo/.pytest_cache/data", "repo/app.pyc",
            "backend/media/patents/tenant-1/patent-1/versions/source.pdf",
            "backend/uploads/tenant-1/source.pdf",
            "backend/staticfiles/admin/css/base.css",
        ):
            with self.subTest(name=name):
                self.assertIsNotNone(reason_for_member(name))

    def test_traversal_and_absolute_paths_rejected(self):
        self.assertIsNotNone(reason_for_member("../escape.txt"))
        self.assertIsNotNone(reason_for_member("/absolute/path"))

    def test_archive_verification_reports_forbidden_entries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / "candidate.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("PatentShield/.env", "not a real credential")
                archive.writestr("PatentShield/src/app.py", "print('ok')")
            failures = verify_archive(str(archive_path))
            self.assertEqual(len(failures), 1)
            self.assertIn(".env", failures[0])

    def test_tenant_media_and_staticfiles_are_rejected_in_real_archives(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / "candidate.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr(
                    "PatentShield/backend/media/patents/tenant-1/patent-1/versions/fake.pdf",
                    b"%PDF-1.7\\nredacted test fixture",
                )
                archive.writestr(
                    "PatentShield/backend/staticfiles/admin/css/base.css",
                    "/* generated */",
                )
            failures = verify_archive(str(archive_path))
            self.assertEqual(len(failures), 2)
            self.assertTrue(any("media" in failure for failure in failures))
            self.assertTrue(any("staticfiles" in failure for failure in failures))

    def test_safe_archive_passes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = Path(temp_dir) / "candidate.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("PatentShield/.env.example", "DJANGO_DEBUG=False\n")
                archive.writestr("PatentShield/backend/manage.py", "pass\n")
            self.assertEqual(verify_archive(str(archive_path)), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
