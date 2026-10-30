"""Import the curated public patent JSONL dataset into PatentPublication.

Expected JSONL schema per line:
    {"patent_number": "...", "title": "...", "abstract": "...", "sector_label": "..."}

The importer is safe to rerun: DATASET + patent_number is the stable key, and
source_payload_hash prevents unnecessary updates when a record is unchanged.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from patents.intelligence_models import PatentPublication


class Command(BaseCommand):
    help = "Import public patent records from a JSON Lines file."

    # Unknown or non-standard sectors mapping to our 6 standard Django choices
    SECTOR_MAPPING = {
        "differential_privacy_algorithms": "machine_learning_data_privacy",
        "differential_privacy": "machine_learning_data_privacy",
        "privacy_preserving_ml": "machine_learning_data_privacy",
        "federated_learning": "federated_learning_encryption",
        "5g_slicing": "5g_network_slicing",
        "sensor_network_routing": "wireless_sensor_data_routing",
        "iot_security": "iot_device_authentication",
    }

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--file",
            dest="file_path",
            default=None,
            help=(
                "JSONL file path. Relative paths are resolved from Django BASE_DIR. "
                "Defaults to BASE_DIR/data/patents_all.jsonl."
            ),
        )
        parser.add_argument(
            "--default-jurisdiction",
            default="UNK",
            help=(
                "Jurisdiction to use because the supplied JSONL schema has no "
                "jurisdiction field. Defaults to UNK; use a real code only when "
                "you have confirmed every row belongs to that jurisdiction."
            ),
        )
        parser.add_argument(
            "--strict",
            action="store_true",
            help="Abort before writing anything if any input line is invalid.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        path = self._resolve_path(options.get("file_path"))
        if not path.is_file():
            raise CommandError(f"JSONL file not found: {path}")

        jurisdiction = str(options["default_jurisdiction"]).strip().upper()
        if not jurisdiction:
            raise CommandError("--default-jurisdiction cannot be blank.")
        if len(jurisdiction) > 10:
            raise CommandError("--default-jurisdiction must be at most 10 characters.")

        validated_records: list[dict[str, Any]] = []
        errors: list[str] = []

        try:
            with path.open("r", encoding="utf-8-sig") as source_file:
                for line_number, raw_line in enumerate(source_file, start=1):
                    if not raw_line.strip():
                        continue
                    try:
                        payload = json.loads(raw_line)
                        record = self._validate_record(
                            payload=payload,
                            line_number=line_number,
                            source_filename=path.name,
                            jurisdiction=jurisdiction,
                        )
                        validated_records.append(record)
                    except (json.JSONDecodeError, TypeError, ValueError) as exc:
                        errors.append(f"line {line_number}: {exc}")
        except OSError as exc:
            raise CommandError(f"Could not read {path}: {exc}") from exc

        if errors and options.get("strict"):
            preview = "\n".join(errors[:20])
            remaining = len(errors) - min(len(errors), 20)
            suffix = f"\n... and {remaining} more invalid line(s)." if remaining else ""
            raise CommandError(
                "Strict mode: input validation failed; no database rows were written.\n"
                f"{preview}{suffix}"
            )

        if not validated_records:
            if errors:
                raise CommandError(
                    f"No valid records found in {path.name}; {len(errors)} invalid line(s)."
                )
            self.stdout.write(self.style.WARNING("The JSONL file contains no records."))
            return

        created_count = 0
        updated_count = 0
        unchanged_count = 0
        run_at = timezone.now()

        # One transaction prevents a partially imported dataset if a database
        # constraint or persistence error occurs partway through the file.
        with transaction.atomic():
            for record in validated_records:
                defaults = {
                    "publication_number": record["patent_number"],
                    "jurisdiction": record["jurisdiction"],
                    "title": record["title"],
                    "abstract": record["abstract"],
                    "sector_label": record["sector_label"],
                    "source_payload_hash": record["source_payload_hash"],
                    "raw_metadata": record["raw_metadata"],
                    "last_synced_at": run_at,
                }

                publication, created = PatentPublication.objects.get_or_create(
                    provider=PatentPublication.Provider.DATASET,
                    provider_record_id=record["patent_number"],
                    defaults=defaults,
                )

                if created:
                    created_count += 1
                    continue

                # Comparing the canonical payload hash makes repeated imports
                # true no-ops: timestamps and provenance do not churn on reruns.
                if publication.source_payload_hash == record["source_payload_hash"]:
                    unchanged_count += 1
                    continue

                for field_name, value in defaults.items():
                    setattr(publication, field_name, value)
                publication.save(
                    update_fields=[
                        *defaults.keys(),
                        "last_seen_at",
                        "updated_at",
                    ]
                )
                updated_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                "Patent dataset import completed: "
                f"created={created_count}, updated={updated_count}, "
                f"unchanged={unchanged_count}, invalid={len(errors)}."
            )
        )
        if errors:
            self.stdout.write(self.style.WARNING("Invalid input rows were skipped:"))
            for message in errors[:20]:
                self.stdout.write(f"  - {message}")
            if len(errors) > 20:
                self.stdout.write(f"  - ... and {len(errors) - 20} more.")

    @staticmethod
    def _resolve_path(file_path: str | None) -> Path:
        base_dir = Path(settings.BASE_DIR)
        if file_path is None:
            return (base_dir / "data" / "patents_all.jsonl").resolve()

        candidate = Path(file_path).expanduser()
        if not candidate.is_absolute():
            candidate = base_dir / candidate
        return candidate.resolve()

    def _validate_record(
        self,
        *,
        payload: Any,
        line_number: int,
        source_filename: str,
        jurisdiction: str,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("each JSONL row must be a JSON object")

        patent_number = payload.get("patent_number")
        title = payload.get("title")
        abstract = payload.get("abstract")
        sector_label = payload.get("sector_label")

        if not isinstance(patent_number, str) or not patent_number.strip():
            raise ValueError("patent_number must be a non-empty string")
        patent_number = patent_number.strip()
        if len(patent_number) > 100:
            raise ValueError("patent_number exceeds the 100-character publication_number limit")

        if not isinstance(title, str) or not title.strip():
            raise ValueError("title must be a non-empty string")
        title = title.strip()

        if abstract is not None and not isinstance(abstract, str):
            raise ValueError("abstract must be a string or null")
        if isinstance(abstract, str):
            abstract = abstract.strip() or None
        else:
            abstract = None

        if not isinstance(sector_label, str) or not sector_label.strip():
            sector_label = PatentPublication.Sector.MACHINE_LEARNING_DATA_PRIVACY.value
        else:
            sector_label = sector_label.strip().casefold()
            valid_sectors = set(PatentPublication.Sector.values)

            # Map unknown or unlisted sector labels
            if sector_label in self.SECTOR_MAPPING:
                sector_label = self.SECTOR_MAPPING[sector_label]

            # Safe fallback for any other unexpected sector values
            if sector_label not in valid_sectors:
                sector_label = PatentPublication.Sector.MACHINE_LEARNING_DATA_PRIVACY.value

        canonical_payload = {
            "patent_number": patent_number,
            "title": title,
            "abstract": abstract,
            "sector_label": sector_label,
            "jurisdiction": jurisdiction,
        }
        encoded_payload = json.dumps(
            canonical_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        payload_hash = hashlib.sha256(encoded_payload).hexdigest()

        return {
            **canonical_payload,
            "source_payload_hash": payload_hash,
            "raw_metadata": {
                "dataset_import": {
                    "schema": "patents_all.v1",
                    "source_file": source_filename,
                    "source_line": line_number,
                }
            },
        }