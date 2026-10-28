"""Reproducible snapshot, assignee, taxonomy, embedding and analytics services."""
from __future__ import annotations

import hashlib
import json
import math
import random
import re
import unicodedata
from collections import Counter
from typing import Any, Sequence

from django.db import transaction
from django.utils import timezone

from audit.models import AuditLog
from indexing.services import BGEEmbedder
from landscapes.models import (
    AssigneeAlias,
    AssigneeEntity,
    LandscapeCluster,
    LandscapePublicationAssignee,
    LandscapePublicationEmbedding,
    LandscapePublicationTechnology,
    LandscapeStudy,
    LandscapeStudyPublication,
    TechnologyTaxonomy,
)
from patents.intelligence_models import PatentPublication, PatentSourceSnapshot


_CORPORATE_SUFFIXES = frozenset({
    "inc", "incorporated", "corp", "corporation", "co", "company", "ltd",
    "limited", "llc", "llp", "plc", "gmbh", "ag", "sa", "sarl", "bv",
    "nv", "pty", "pte", "kg", "oy", "ab", "kk", "kabushiki", "kaisha",
})
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "from", "into", "based", "using", "use",
    "method", "methods", "system", "systems", "device", "devices", "apparatus",
    "apparatuses", "application", "patent", "technology", "technologies", "new",
})
_CLASS_CODE_RE = re.compile(r"^[A-HY][0-9]{2}")
_TITLE_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9-]{2,}|[\u3400-\u9fff]{2,}|[\u3040-\u30ff]{2,}|[\uac00-\ud7af]{2,}")


def normalize_assignee_name(value: str) -> str:
    """Normalize an applicant name conservatively for exact alias resolution.

    Unicode normalization, case-folding and punctuation removal are deterministic.
    Corporate suffixes are removed only as trailing tokens; no fuzzy match or
    automatic merger is performed across distinct normalized keys.
    """
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    normalized = normalized.replace("&", " and ")
    normalized = re.sub(r"[^\w\s]", " ", normalized, flags=re.UNICODE)
    tokens = re.sub(r"\s+", " ", normalized).split()
    while tokens and tokens[-1] in _CORPORATE_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)[:500]


def clean_name_list(values: Any) -> list[str]:
    """Return bounded applicant names from provider JSON without assuming its shape."""
    if not isinstance(values, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value in values[:100]:
        if isinstance(value, str):
            name = value.strip()
        elif isinstance(value, dict):
            name = next((str(value.get(k, "")).strip() for k in ("name", "applicant", "original_name", "value") if value.get(k)), "")
        else:
            name = ""
        key = normalize_assignee_name(name)
        if name and key and key not in seen:
            result.append(name[:400])
            seen.add(key)
    return result


def canonical_snapshot(publication: PatentPublication, source_snapshot: PatentSourceSnapshot | None = None) -> dict[str, Any]:
    """Return a stable, minimal bibliographic snapshot; never copy full raw payloads."""
    applicant_names = clean_name_list(publication.applicant_names)
    snapshot = {
        "publication_id": str(publication.pk),
        "provider": publication.provider,
        "provider_record_id": publication.provider_record_id,
        "publication_number": publication.publication_number or "",
        "application_number": publication.application_number or "",
        "jurisdiction": publication.jurisdiction or "",
        "title": publication.title or "",
        "abstract": publication.abstract or "",
        "filing_date": publication.filing_date.isoformat() if publication.filing_date else None,
        "publication_date": publication.publication_date.isoformat() if publication.publication_date else None,
        "grant_date": publication.grant_date.isoformat() if publication.grant_date else None,
        "legal_status": publication.legal_status or "UNKNOWN",
        "applicant_names": applicant_names,
        "ipc_codes": list(publication.ipc_codes or [])[:200],
        "cpc_codes": list(publication.cpc_codes or [])[:200],
        "family_system": publication.family_system or "",
        "family_identifier": publication.family_identifier or "",
        "source_url": publication.source_url or "",
        "source_payload_hash": publication.source_payload_hash or "",
        "source_snapshot_id": str(source_snapshot.pk) if source_snapshot else None,
        "source_snapshot_sha256": source_snapshot.payload_sha256 if source_snapshot else None,
    }
    return snapshot


def snapshot_sha256(snapshot: dict[str, Any]) -> str:
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def taxonomy_code(value: Any) -> str:
    """Normalize a provider IPC/CPC code for stable tenant taxonomy keys."""
    return re.sub(r"\s+", "", str(value or "").strip().upper())[:100]


def taxonomy_prefix(code: str) -> str:
    """Return a broad three-character IPC/CPC section-class prefix if present."""
    if _CLASS_CODE_RE.match(code):
        return code[:3]
    return code[: min(3, len(code))]


def _ensure_taxonomy_node(tenant_id: Any, code: str, source: str) -> TechnologyTaxonomy | None:
    code = taxonomy_code(code)
    if not code or len(code) < 3:
        return None
    prefix = taxonomy_prefix(code)
    parent = None
    if len(code) > len(prefix) and prefix != code:
        parent, _ = TechnologyTaxonomy.objects.get_or_create(
            tenant_id=tenant_id,
            source=source,
            code=prefix,
            defaults={
                "label": f"{prefix} — {source} class code",
                "description": "Provider classification code; official descriptive label has not been hydrated.",
            },
        )
    node, _ = TechnologyTaxonomy.objects.get_or_create(
        tenant_id=tenant_id,
        source=source,
        code=code,
        defaults={
            "label": code if code == prefix else f"{code} — {source} classification code",
            "description": "Provider-supplied classification identifier; interpretation requires source taxonomy metadata.",
            "parent": parent,
        },
    )
    return node


def resolve_assignees_for_publication(row: LandscapeStudyPublication, actor_id: Any) -> list[str]:
    """Resolve source applicant names into exact tenant-specific entities and aliases."""
    resolved_names: list[str] = []
    for raw_name in clean_name_list(row.applicant_names):
        key = normalize_assignee_name(raw_name)
        if not key:
            continue
        alias = AssigneeAlias.objects.filter(tenant_id=row.tenant_id, normalized_key=key).select_related("entity").first()
        if alias is not None:
            entity = alias.entity
        else:
            entity, _ = AssigneeEntity.objects.get_or_create(
                tenant_id=row.tenant_id,
                normalized_key=key,
                defaults={"canonical_name": raw_name, "created_by_id": actor_id},
            )
            # A normalized alias is a match key, not proof that two legal entities
            # should be merged. Only exact normalized keys are reused.
            AssigneeAlias.objects.get_or_create(
                tenant_id=row.tenant_id,
                normalized_key=key,
                defaults={"entity": entity, "original_name": raw_name, "source": AssigneeAlias.Source.AUTO, "created_by_id": actor_id},
            )
        LandscapePublicationAssignee.objects.get_or_create(
            tenant_id=row.tenant_id,
            study_publication=row,
            entity=entity,
            defaults={"raw_name": raw_name},
        )
        if entity.canonical_name not in resolved_names:
            resolved_names.append(entity.canonical_name)
    row.normalized_assignee_names = resolved_names
    row.save(update_fields=("normalized_assignee_names",))
    return resolved_names


def assign_technology_codes(row: LandscapeStudyPublication) -> int:
    """Link source CPC/IPC codes to source-labelled taxonomy nodes."""
    count = 0
    for source, values in ((TechnologyTaxonomy.Source.CPC, row.cpc_codes), (TechnologyTaxonomy.Source.IPC, row.ipc_codes)):
        if not isinstance(values, list):
            continue
        seen: set[str] = set()
        for raw_code in values[:200]:
            code = taxonomy_code(raw_code)
            if not code or code in seen:
                continue
            seen.add(code)
            node = _ensure_taxonomy_node(row.tenant_id, code, source)
            if node is None:
                continue
            _link, created = LandscapePublicationTechnology.objects.get_or_create(
                tenant_id=row.tenant_id,
                study_publication=row,
                taxonomy=node,
                defaults={"source_code": code},
            )
            count += int(created)
    return count


def _normalise_vector(vector: Sequence[float]) -> list[float]:
    values = [float(value) for value in vector]
    norm = math.sqrt(sum(value * value for value in values))
    if norm <= 1e-12:
        return values
    return [value / norm for value in values]


def _squared_distance(left: Sequence[float], right: Sequence[float]) -> float:
    return sum((a - b) * (a - b) for a, b in zip(left, right))


def deterministic_kmeans(vectors: Sequence[Sequence[float]], max_clusters: int = 8, max_iterations: int = 20) -> list[int]:
    """Small deterministic k-means implementation with farthest-point seeding."""
    if not vectors:
        return []
    normalized = [_normalise_vector(v) for v in vectors]
    n = len(normalized)
    if n == 1:
        return [0]
    k = max(1, min(max_clusters, n, int(round(math.sqrt(n / 2.0))) or 1))
    centers = [normalized[0][:]]
    while len(centers) < k:
        farthest_index = max(
            range(n),
            key=lambda idx: (min(_squared_distance(normalized[idx], center) for center in centers), -idx),
        )
        centers.append(normalized[farthest_index][:])
    labels = [0] * n
    for _ in range(max_iterations):
        next_labels = [min(range(k), key=lambda idx: (_squared_distance(vector, centers[idx]), idx)) for vector in normalized]
        changed = next_labels != labels
        labels = next_labels
        new_centers: list[list[float]] = []
        for cluster_idx in range(k):
            members = [normalized[i] for i, label in enumerate(labels) if label == cluster_idx]
            if not members:
                # Empty clusters retain their deterministic seed rather than
                # silently shrinking the declared cluster number.
                new_centers.append(centers[cluster_idx])
                continue
            mean = [sum(v[d] for v in members) / len(members) for d in range(len(members[0]))]
            new_centers.append(_normalise_vector(mean))
        centers = new_centers
        if not changed:
            break
    # Compact cluster IDs in first-seen order so clients receive stable integers.
    remap: dict[int, int] = {}
    compact: list[int] = []
    for label in labels:
        if label not in remap:
            remap[label] = len(remap)
        compact.append(remap[label])
    return compact


def project_vectors_2d(vectors: Sequence[Sequence[float]]) -> tuple[list[tuple[float, float]], str]:
    """Project vectors with PCA where installed, else fixed-seed random projection."""
    if not vectors:
        return [], "none"
    if len(vectors) == 1:
        return [(0.0, 0.0)], "single_point"
    try:
        import numpy as np
        from sklearn.decomposition import PCA

        matrix = np.asarray(vectors, dtype=float)
        components = min(2, matrix.shape[0], matrix.shape[1])
        projected = PCA(n_components=components, random_state=42).fit_transform(matrix)
        if components == 1:
            coords = [(float(projected[i, 0]), 0.0) for i in range(len(vectors))]
        else:
            coords = [(float(projected[i, 0]), float(projected[i, 1])) for i in range(len(vectors))]
        return coords, "PCA_SKLEARN"
    except ImportError:
        # Fixed projection keeps the UI functional without a new hard dependency.
        dimensions = len(vectors[0])
        rng = random.Random(87231)
        weights_x = [rng.choice((-1.0, 1.0)) / math.sqrt(dimensions) for _ in range(dimensions)]
        weights_y = [rng.choice((-1.0, 1.0)) / math.sqrt(dimensions) for _ in range(dimensions)]
        raw = [(sum(v[d] * weights_x[d] for d in range(dimensions)), sum(v[d] * weights_y[d] for d in range(dimensions))) for v in vectors]
        mean_x = sum(x for x, _ in raw) / len(raw)
        mean_y = sum(y for _, y in raw) / len(raw)
        return [(x - mean_x, y - mean_y) for x, y in raw], "FIXED_RANDOM_PROJECTION"


def describe_cluster(rows: Sequence[LandscapeStudyPublication]) -> tuple[str, list[str], list[str], str]:
    """Build a traceable cluster label from source classification codes/title terms."""
    code_counts: Counter[str] = Counter()
    term_counts: Counter[str] = Counter()
    for row in rows:
        codes = list(row.cpc_codes or []) + list(row.ipc_codes or [])
        for code in codes:
            prefix = taxonomy_prefix(taxonomy_code(code))
            if prefix:
                code_counts[prefix] += 1
        for term in _TITLE_WORD_RE.findall(row.title or ""):
            folded = term.casefold()
            if folded not in _STOPWORDS and len(folded) >= 3:
                term_counts[folded] += 1
    top_codes = [value for value, _ in code_counts.most_common(5)]
    top_terms = [value for value, _ in term_counts.most_common(6)]
    if top_codes:
        label = f"Classification group {top_codes[0]}"
    elif top_terms:
        label = "Topic: " + ", ".join(top_terms[:3])
    else:
        label = "Unclassified group"
    explanation = (
        "Deterministic BGE-M3 vector cluster. Label hints are derived from the most frequent "
        "source IPC/CPC class prefixes and publication-title terms; they are not official taxonomy labels."
    )
    return label, top_terms, top_codes, explanation


def audit_landscape_event(*, tenant_id: Any, actor_id: Any, action: str, resource_type: str, resource_id: Any, metadata: dict[str, Any] | None = None) -> None:
    """Write a compact tenant audit entry without storing patent text."""
    try:
        AuditLog.objects.create(
            tenant_id=tenant_id,
            actor_user_id=actor_id,
            action=action[:100],
            resource_type=resource_type[:100],
            resource_id=resource_id,
            outcome=AuditLog.Outcome.SUCCESS,
            metadata=metadata or {},
        )
    except Exception:
        # Keep audit failure behavior aligned with the rest of the project;
        # the request/task remains observable through Django's regular logs.
        import logging
        logging.getLogger(__name__).exception("Landscape audit write failed (resource_type=%s).", resource_type)


def create_study_publication_snapshot(study: LandscapeStudy, publication: PatentPublication) -> tuple[LandscapeStudyPublication, bool]:
    """Pin selected canonical publication fields for reproducible study results."""
    source_snapshot = PatentSourceSnapshot.objects.filter(publication=publication).order_by("-fetched_at").first()
    snapshot = canonical_snapshot(publication, source_snapshot)
    digest = snapshot_sha256(snapshot)
    row, created = LandscapeStudyPublication.objects.get_or_create(
        tenant_id=study.tenant_id,
        study=study,
        publication=publication,
        defaults={
            "source_snapshot": source_snapshot,
            "publication_number": snapshot["publication_number"],
            "application_number": snapshot["application_number"],
            "jurisdiction": snapshot["jurisdiction"],
            "title": snapshot["title"],
            "abstract": snapshot["abstract"],
            "filing_date": publication.filing_date,
            "publication_date": publication.publication_date,
            "grant_date": publication.grant_date,
            "legal_status": snapshot["legal_status"],
            "applicant_names": snapshot["applicant_names"],
            "ipc_codes": snapshot["ipc_codes"],
            "cpc_codes": snapshot["cpc_codes"],
            "family_system": snapshot["family_system"],
            "family_identifier": snapshot["family_identifier"],
            "source_url": snapshot["source_url"],
            "source_payload_hash": snapshot["source_payload_hash"],
            "snapshot_sha256": digest,
        },
    )
    return row, created


def get_embedding_text(row: LandscapeStudyPublication) -> str:
    """Build the exact public-field text that the vector checksum identifies."""
    pieces = [row.title.strip(), row.abstract.strip()]
    pieces.extend(str(code).strip() for code in (row.cpc_codes or [])[:100] if str(code).strip())
    pieces.extend(str(code).strip() for code in (row.ipc_codes or [])[:100] if str(code).strip())
    return "\n".join(piece for piece in pieces if piece).strip() or row.publication_number or str(row.publication_id)


def save_publication_embedding(row: LandscapeStudyPublication, vector: Sequence[float], text: str) -> None:
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    LandscapePublicationEmbedding.objects.update_or_create(
        tenant_id=row.tenant_id,
        study_publication=row,
        defaults={"text_sha256": digest, "embedding_model": BGEEmbedder.MODEL_NAME, "embedding": list(vector)},
    )


def mark_build_started(study_id: Any, tenant_id: Any) -> LandscapeStudy | None:
    with transaction.atomic():
        study = LandscapeStudy.objects.select_for_update().filter(pk=study_id, tenant_id=tenant_id).first()
        if study is None:
            return None
        study.status = LandscapeStudy.Status.BUILDING
        study.error_code = ""
        study.started_at = timezone.now()
        study.completed_at = None
        study.save(update_fields=("status", "error_code", "started_at", "completed_at", "updated_at"))
        return study
