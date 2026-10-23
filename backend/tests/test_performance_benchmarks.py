"""Repeatable retrieval and PDF-ingestion SLA checks for PatentShield.

By default these tests use deterministic model stubs so they can run in CI
without downloading ML weights. Set PATENTSHIELD_RUN_REAL_ML_SLA=1 to include
locally cached BGE-M3 and BGE-reranker-v2-m3 inference in the retrieval SLA.
"""

from __future__ import annotations

import hashlib
import io
import os
import time
import tracemalloc
import uuid
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.base_user import AbstractBaseUser
from django.db import connection
from django.test import override_settings

from indexing.models import TextChunk
from indexing.services import BGEEmbedder
from patents.models import Patent, PatentDocumentVersion
from tenants.models import Tenant, TenantMembership


VECTOR = [1.0] + [0.0] * (BGEEmbedder.DIMENSIONS - 1)
TEST_PASSWORD = "PatentShield-benchmark-Password-27!"


def _env_positive_float(name: str, default: float) -> float:
    """Read a positive float threshold from an environment variable."""
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive number.") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive number.")
    return value


def _create_user_and_tenant(label: str) -> tuple[AbstractBaseUser, Tenant]:
    """Create an isolated user and tenant for a performance test."""
    user_model = get_user_model()
    identity = f"ps-{label}-{uuid.uuid4().hex[:12]}"
    user = user_model.objects.create_user(
        password=TEST_PASSWORD,
        **{user_model.USERNAME_FIELD: identity},
    )
    suffix = uuid.uuid4().hex[:10]
    tenant = Tenant.objects.create(
        name=f"{label} {suffix}",
        slug=f"{label.lower().replace(' ', '-')}-{suffix}",
        status=Tenant.Status.ACTIVE,
    )
    TenantMembership.objects.create(
        tenant=tenant,
        user=user,
        role=TenantMembership.Role.ANALYST,
        status=TenantMembership.Status.ACTIVE,
    )
    return user, tenant


def _create_index_fixture(
    *,
    tenant: Tenant,
    user: AbstractBaseUser,
    chunk_count: int,
) -> PatentDocumentVersion:
    """Create one document with enough tenant-local chunks for hybrid search."""
    patent = Patent.objects.create(
        tenant=tenant,
        created_by=user,
        title="Synthetic retrieval SLA fixture",
        abstract=(
            "A wireless sensor system includes a processor, memory, and "
            "secure communications interface for transmitting measurements."
        ),
        jurisdiction="US",
        publication_number=f"US-{uuid.uuid4().hex[:10].upper()}",
    )
    version = PatentDocumentVersion.objects.create(
        tenant=tenant,
        patent=patent,
        version_number=1,
        storage_key=f"benchmarks/{uuid.uuid4()}.pdf",
        content_sha256="0" * 64,
    )

    rows: list[TextChunk] = []
    for index in range(chunk_count):
        text = (
            "A wireless sensor communicates measurements to a processor. "
            f"Technical description passage {index} covers a memory circuit, "
            "signal acquisition, and secure communication protocols."
        )
        rows.append(
            TextChunk(
                tenant=tenant,
                patent=patent,
                document_version=version,
                section_type=TextChunk.SectionType.SPECIFICATION,
                text=text,
                char_start=index * 500,
                char_end=index * 500 + len(text),
                embedding=VECTOR,
            )
        )
    TextChunk.objects.bulk_create(rows, batch_size=100)
    return version


def _fake_embedding_model(
    _cls: type[BGEEmbedder],
    texts: list[str],
) -> list[list[float]]:
    """Fast deterministic embedding stub for non-ML CI runs."""
    return [VECTOR.copy() for _text in texts]


def _mock_reranker(
    _cls: type[Any],
    query: str,
    candidates: list[Any],
    top_n: int = 10,
) -> list[Any]:
    """Keep the deterministic benchmark independent of model weights."""
    del query
    for index, candidate in enumerate(candidates):
        candidate.rerank_score = 1.0 / (index + 1)
    return candidates[:top_n]


@pytest.mark.django_db
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="Retrieval SLA requires PostgreSQL with the pgvector extension.",
)
def test_retrieval_latency_sla(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hybrid retrieval, RRF, and reranking complete within the 3s warm SLA."""
    from retrieval.services import CrossEncoderReranker, HybridSearchPipeline

    user, tenant = _create_user_and_tenant("retrieval-sla")
    _create_index_fixture(tenant=tenant, user=user, chunk_count=32)

    use_real_ml = os.getenv("PATENTSHIELD_RUN_REAL_ML_SLA", "0").casefold() in {
        "1",
        "true",
        "yes",
    }
    if not use_real_ml:
        monkeypatch.setattr(
            BGEEmbedder,
            "embed_texts",
            classmethod(_fake_embedding_model),
        )
        monkeypatch.setattr(
            CrossEncoderReranker,
            "rerank",
            classmethod(_mock_reranker),
        )

    sla_seconds = _env_positive_float("PATENTSHIELD_RETRIEVAL_SLA_SECONDS", 3.0)
    # Warm query planning and, when enabled, locally cached ML weights before
    # timing. Model cold-start time is a separate operational SLA.
    with override_settings(
        DEBUG=not use_real_ml,
        PATENTSHIELD_ALLOW_MOCK_EMBEDDINGS=not use_real_ml,
    ):
        pipeline = HybridSearchPipeline()
        first_result = pipeline.retrieve_evidence(
            query="wireless sensor processor memory",
            tenant_id=tenant.pk,
            top_n=5,
        )
        assert first_result, "The benchmark fixture should return evidence."

        started_at = time.perf_counter()
        results = pipeline.retrieve_evidence(
            query="wireless sensor processor memory",
            tenant_id=tenant.pk,
            top_n=5,
        )
        elapsed_seconds = time.perf_counter() - started_at

    assert results, "Hybrid retrieval returned no candidates for the benchmark corpus."
    assert all(result.tenant_id == str(tenant.pk) for result in results)
    assert elapsed_seconds < sla_seconds, (
        "Warm hybrid retrieval + reranking exceeded its SLA: "
        f"{elapsed_seconds:.3f}s >= {sla_seconds:.3f}s. "
        "Set PATENTSHIELD_RUN_REAL_ML_SLA=1 to benchmark real cached models."
    )


def _build_multi_page_pdf(page_count: int = 12) -> bytes:
    """Generate a selectable-text multi-page synthetic patent PDF in memory."""
    canvas_module = pytest.importorskip("reportlab.pdfgen.canvas")
    from reportlab.lib.pagesizes import letter

    output = io.BytesIO()
    canvas = canvas_module.Canvas(output, pagesize=letter, pageCompression=0)
    for page_index in range(page_count):
        canvas.setFont("Helvetica", 10)
        canvas.drawString(
            48,
            735,
            f"PatentShield ingestion benchmark specification page {page_index + 1}.",
        )
        canvas.drawString(
            48,
            710,
            "A sensor communicates measured data to a processor and memory circuit.",
        )
        for line_index in range(6):
            canvas.drawString(
                48,
                675 - line_index * 18,
                (
                    f"Technical description {page_index}-{line_index}: "
                    "signal processing, data storage, and communication control."
                ),
            )
        if page_index == page_count - 2:
            canvas.drawString(48, 520, "What is claimed is:")
        if page_index == page_count - 1:
            canvas.drawString(
                48,
                700,
                "1. A computing system comprising a processor and memory.",
            )
            canvas.drawString(
                48,
                675,
                "2. The computing system of claim 1, further comprising a sensor.",
            )
        canvas.showPage()
    canvas.save()
    return output.getvalue()


@pytest.mark.django_db
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="PDF embedding task writes pgvector-backed TextChunk records.",
)
def test_pdf_ingestion_throughput_sla(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Extract and index a 12-page PDF under time and Python-memory budgets."""
    from ingestion import tasks as ingestion_tasks
    from indexing import tasks as indexing_tasks

    pdf_bytes = _build_multi_page_pdf(page_count=12)
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    user, tenant = _create_user_and_tenant("pdf-ingestion-sla")
    patent = Patent.objects.create(
        tenant=tenant,
        created_by=user,
        title="Synthetic PDF ingestion SLA patent",
        abstract=(
            "A sensor platform with a processor, memory, and communication interface."
        ),
        jurisdiction="US",
        publication_number=f"US-{uuid.uuid4().hex[:10].upper()}",
    )
    version = PatentDocumentVersion.objects.create(
        tenant=tenant,
        patent=patent,
        version_number=1,
        storage_key=f"test-memory-storage/{uuid.uuid4()}.pdf",
        content_sha256=digest,
    )

    class InMemoryPatentStorage:
        """Minimal storage adapter returning the generated PDF bytes."""

        def open(self, name: str, mode: str = "rb") -> io.BytesIO:
            assert name == version.storage_key
            assert mode == "rb"
            return io.BytesIO(pdf_bytes)

    storage = InMemoryPatentStorage()
    monkeypatch.setattr(ingestion_tasks, "default_storage", storage)
    monkeypatch.setattr(indexing_tasks, "default_storage", storage)
    monkeypatch.setattr(
        indexing_tasks.BGEEmbedder,
        "embed_texts",
        classmethod(_fake_embedding_model),
    )

    sla_seconds = _env_positive_float("PATENTSHIELD_PDF_INGESTION_SLA_SECONDS", 10.0)
    memory_limit_mb = _env_positive_float(
        "PATENTSHIELD_PDF_INGESTION_MEMORY_LIMIT_MB",
        256.0,
    )

    tracemalloc.start()
    started_at = time.perf_counter()
    try:
        extraction_result = ingestion_tasks.process_patent_document_version.run(
            str(version.pk)
        )
        embedding_result = indexing_tasks.generate_document_embeddings.run(
            str(version.pk)
        )
        elapsed_seconds = time.perf_counter() - started_at
        _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    version.refresh_from_db()
    assert extraction_result["page_count"] == 12
    assert extraction_result["claims_created"] >= 2
    assert embedding_result["chunks_created"] >= 2
    assert version.extraction_status == PatentDocumentVersion.ExtractionStatus.EXTRACTED
    assert TextChunk.objects.for_tenant(tenant.pk).filter(
        document_version=version,
        embedding__isnull=False,
    ).exists()

    peak_memory_mb = peak_bytes / (1024 * 1024)
    assert elapsed_seconds < sla_seconds, (
        "PDF extraction + embedding-index task exceeded its SLA: "
        f"{elapsed_seconds:.3f}s >= {sla_seconds:.3f}s."
    )
    assert peak_memory_mb < memory_limit_mb, (
        "PDF ingestion exceeded its Python allocation budget: "
        f"{peak_memory_mb:.1f} MiB >= {memory_limit_mb:.1f} MiB."
    )
