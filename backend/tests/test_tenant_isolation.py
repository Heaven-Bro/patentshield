"""Security regression tests for PatentShield tenant boundaries.

These tests expect PatentShield's Django apps and API routes to be installed,
plus PostgreSQL with the pgvector extension for vector-search coverage.
"""

from __future__ import annotations

import uuid
from typing import Any, TypedDict

import pytest
from asgiref.sync import async_to_sync
from channels.auth import AuthMiddlewareStack
from channels.routing import URLRouter
from channels.testing import WebsocketCommunicator
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.base_user import AbstractBaseUser
from django.urls import Resolver404
from django.db import connection
from django.test import Client, override_settings
from django.urls import resolve
from rest_framework.test import APIClient

from analyses.models import AnalysisReport, ClaimAssessment
from analyses.routing import websocket_urlpatterns
from indexing.models import TextChunk
from patents.models import Claim, Patent, PatentDocumentVersion
from tenants.models import Tenant, TenantMembership


TEST_PASSWORD = "PatentShield-test-only-Password-93!"


class TenantPair(TypedDict):
    """Two independent tenants with one active member each."""

    tenant_a: Tenant
    tenant_b: Tenant
    user_a: AbstractBaseUser
    user_b: AbstractBaseUser


def _create_user(label: str) -> AbstractBaseUser:
    """Create a user without assuming whether username or email is the key."""
    user_model = get_user_model()
    identity_field = user_model.USERNAME_FIELD
    identity = f"ps-{label}-{uuid.uuid4().hex[:12]}"
    return user_model.objects.create_user(
        password=TEST_PASSWORD,
        **{identity_field: identity},
    )


def _create_tenant(name: str) -> Tenant:
    """Create a uniquely named tenant for one test."""
    unique_suffix = uuid.uuid4().hex[:10]
    return Tenant.objects.create(
        name=f"{name} {unique_suffix}",
        slug=f"{name.lower().replace(' ', '-')}-{unique_suffix}",
        status=Tenant.Status.ACTIVE,
    )


def _create_patent_version(
    *,
    tenant: Tenant,
    user: AbstractBaseUser,
    label: str,
) -> tuple[Patent, PatentDocumentVersion]:
    """Create a tenant-consistent patent and first document version."""
    patent = Patent.objects.create(
        tenant=tenant,
        created_by=user,
        title=f"{label} confidential patent title",
        abstract="A sensor system with a processor and communication interface.",
        jurisdiction="US",
        publication_number=f"US-{uuid.uuid4().hex[:10].upper()}",
    )
    version = PatentDocumentVersion.objects.create(
        tenant=tenant,
        patent=patent,
        version_number=1,
        storage_key=f"test-only/{uuid.uuid4()}.pdf",
        content_sha256="0" * 64,
    )
    return patent, version


@pytest.fixture
def tenant_pair() -> TenantPair:
    """Create tenants A/B and active memberships for users A/B respectively."""
    tenant_a = _create_tenant("tenant-a")
    tenant_b = _create_tenant("tenant-b")
    user_a = _create_user("user-a")
    user_b = _create_user("user-b")

    TenantMembership.objects.create(
        tenant=tenant_a,
        user=user_a,
        role=TenantMembership.Role.REVIEWER,
        status=TenantMembership.Status.ACTIVE,
    )
    TenantMembership.objects.create(
        tenant=tenant_b,
        user=user_b,
        role=TenantMembership.Role.OWNER,
        status=TenantMembership.Status.ACTIVE,
    )
    return {
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "user_a": user_a,
        "user_b": user_b,
    }


def _create_analysis_with_assessment(
    *,
    tenant: Tenant,
    user: AbstractBaseUser,
    patent: Patent,
    version: PatentDocumentVersion,
) -> tuple[AnalysisReport, ClaimAssessment]:
    """Create analysis and assessment rows with consistent tenant ownership."""
    claim = Claim.objects.create(
        tenant=tenant,
        patent=patent,
        document_version=version,
        claim_number="1",
        claim_text="A device comprising a sensor and a processor.",
        claim_type="INDEPENDENT",
    )
    analysis = AnalysisReport.objects.create(
        tenant=tenant,
        patent=patent,
        target_document_version=version,
        created_by=user,
        status=AnalysisReport.Status.COMPLETED,
    )
    assessment = ClaimAssessment.objects.create(
        tenant=tenant,
        analysis=analysis,
        claim=claim,
        limitation_index=1,
        limitation_text="a sensor",
        assessment_status=ClaimAssessment.AssessmentStatus.UNCERTAIN,
        confidence=0.25,
    )
    return analysis, assessment


def _assert_route_exists(path: str) -> None:
    """Avoid accepting a missing URL configuration as a security pass."""
    try:
        resolve(path)
    except Resolver404 as exc:
        pytest.fail(
            f"Expected PatentShield API route {path!r} is not configured.",
            pytrace=True,
        )


@pytest.mark.django_db
def test_cross_tenant_rest_api_denied(tenant_pair: TenantPair) -> None:
    """Tenant A cannot read or review patent/analysis records owned by B."""
    tenant_a = tenant_pair["tenant_a"]
    tenant_b = tenant_pair["tenant_b"]
    user_a = tenant_pair["user_a"]
    user_b = tenant_pair["user_b"]

    patent_b, version_b = _create_patent_version(
        tenant=tenant_b,
        user=user_b,
        label="tenant-b",
    )
    analysis_b, assessment_b = _create_analysis_with_assessment(
        tenant=tenant_b,
        user=user_b,
        patent=patent_b,
        version=version_b,
    )

    patent_url = f"/api/v1/patents/{patent_b.pk}/"
    review_url = f"/api/v1/analyses/{analysis_b.pk}/review/"
    _assert_route_exists(patent_url)
    _assert_route_exists(review_url)

    client = APIClient()
    client.force_authenticate(user=user_a)

    patent_response = client.get(
        patent_url,
        HTTP_X_TENANT_ID=str(tenant_a.pk),
    )
    assert patent_response.status_code in {403, 404}, (
        "Cross-tenant patent access must be denied; expected 403 or a privacy-"
        f"preserving 404, got {patent_response.status_code}."
    )
    assert patent_b.title not in str(patent_response.data)

    review_payload = {
        "assessment_id": str(assessment_b.pk),
        "review_status": ClaimAssessment.ReviewStatus.ACCEPTED,
        "reviewer_notes": "This must never be applied by tenant A.",
    }
    review_response = client.post(
        review_url,
        review_payload,
        format="json",
        HTTP_X_TENANT_ID=str(tenant_a.pk),
    )
    # The current API returns 404 when a cross-tenant AnalysisReport is absent
    # from the tenant-scoped queryset. A 403 is also a valid secure denial.
    assert review_response.status_code in {403, 404}, (
        "Cross-tenant review must be denied, got "
        f"{review_response.status_code}."
    )
    assessment_b.refresh_from_db()
    assert assessment_b.review_status == ClaimAssessment.ReviewStatus.UNREVIEWED
    assert patent_b.title not in str(review_response.data)

    # Supplying tenant B's ID must not allow user A to switch into tenant B.
    membership_denial = client.post(
        review_url,
        review_payload,
        format="json",
        HTTP_X_TENANT_ID=str(tenant_b.pk),
    )
    assert membership_denial.status_code == 403
    assessment_b.refresh_from_db()
    assert assessment_b.review_status == ClaimAssessment.ReviewStatus.UNREVIEWED


@pytest.mark.django_db(transaction=True)
@override_settings(
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
)
def test_cross_tenant_websocket_rejected(tenant_pair: TenantPair) -> None:
    """An authenticated tenant-A user cannot subscribe to tenant-B analysis."""
    tenant_b = tenant_pair["tenant_b"]
    user_b = tenant_pair["user_b"]
    user_a = tenant_pair["user_a"]

    patent_b, version_b = _create_patent_version(
        tenant=tenant_b,
        user=user_b,
        label="tenant-b-websocket",
    )
    analysis_b, _assessment_b = _create_analysis_with_assessment(
        tenant=tenant_b,
        user=user_b,
        patent=patent_b,
        version=version_b,
    )

    django_client = Client()
    django_client.force_login(user_a)
    session_cookie = django_client.cookies[settings.SESSION_COOKIE_NAME].value
    headers = [
        (
            b"cookie",
            f"{settings.SESSION_COOKIE_NAME}={session_cookie}".encode("ascii"),
        ),
    ]
    application = AuthMiddlewareStack(URLRouter(websocket_urlpatterns))
    communicator = WebsocketCommunicator(
        application,
        f"/ws/v1/analyses/{analysis_b.pk}/",
        headers=headers,
    )

    async def _attempt_connection() -> tuple[bool, int | str | None]:
        connected, close_code_or_subprotocol = await communicator.connect()
        if connected:
            await communicator.disconnect()
        return connected, close_code_or_subprotocol

    connected, close_code = async_to_sync(_attempt_connection)()
    assert connected is False, (
        "A user without active membership in the analysis tenant must not be "
        "accepted into its progress WebSocket group."
    )
    assert close_code == 4403


@pytest.mark.django_db
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="Dense retrieval isolation requires PostgreSQL with pgvector.",
)
def test_vector_search_tenant_isolation(
    tenant_pair: TenantPair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dense retrieval returns only tenant-A chunks despite identical text."""
    from retrieval.services import DenseRetriever
    from indexing.services import BGEEmbedder

    tenant_a = tenant_pair["tenant_a"]
    tenant_b = tenant_pair["tenant_b"]
    user_a = tenant_pair["user_a"]
    user_b = tenant_pair["user_b"]
    vector = [1.0] + [0.0] * (BGEEmbedder.DIMENSIONS - 1)
    embedding_model, embedding_revision = BGEEmbedder.model_identity()
    shared_text = "A wireless sensor communicates measurements to a processor."

    patent_a, version_a = _create_patent_version(
        tenant=tenant_a,
        user=user_a,
        label="tenant-a-vector",
    )
    patent_b, version_b = _create_patent_version(
        tenant=tenant_b,
        user=user_b,
        label="tenant-b-vector",
    )
    for tenant, patent, version in (
        (tenant_a, patent_a, version_a),
        (tenant_b, patent_b, version_b),
    ):
        TextChunk.objects.create(
            tenant=tenant,
            patent=patent,
            document_version=version,
            section_type=TextChunk.SectionType.SPECIFICATION,
            text=shared_text,
            char_start=0,
            char_end=len(shared_text),
            embedding=vector,
            embedding_model=embedding_model,
            embedding_revision=embedding_revision,
        )

    monkeypatch.setattr(
        BGEEmbedder,
        "embed_texts",
        classmethod(lambda _cls, texts: [vector for _text in texts]),
    )
    candidates = DenseRetriever.search(
        query="wireless sensor processor",
        tenant_id=tenant_a.pk,
        top_k=20,
    )

    assert candidates, "Expected the tenant-A fixture chunk to be retrieved."
    assert all(candidate.tenant_id == str(tenant_a.pk) for candidate in candidates)
    assert {candidate.patent_id for candidate in candidates} == {str(patent_a.pk)}
    assert str(patent_b.pk) not in {candidate.patent_id for candidate in candidates}


@pytest.mark.django_db
@pytest.mark.skipif(
    connection.vendor != "postgresql",
    reason="Full hybrid isolation requires PostgreSQL with pgvector.",
)
def test_hybrid_retrieval_tenant_isolation(
    tenant_pair: TenantPair,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dense retrieval, BM25, fusion, and reranking never cross tenant bounds."""
    from indexing.services import BGEEmbedder
    from retrieval.services import CrossEncoderReranker, HybridSearchPipeline

    tenant_a = tenant_pair["tenant_a"]
    tenant_b = tenant_pair["tenant_b"]
    user_a = tenant_pair["user_a"]
    user_b = tenant_pair["user_b"]
    vector = [1.0] + [0.0] * (BGEEmbedder.DIMENSIONS - 1)
    embedding_model, embedding_revision = BGEEmbedder.model_identity()
    shared_text = (
        "A wireless sensor transmits measurements to a processor over a bus."
    )

    patent_a, version_a = _create_patent_version(
        tenant=tenant_a,
        user=user_a,
        label="tenant-a-hybrid",
    )
    patent_b, version_b = _create_patent_version(
        tenant=tenant_b,
        user=user_b,
        label="tenant-b-hybrid",
    )
    for tenant, patent, version in (
        (tenant_a, patent_a, version_a),
        (tenant_b, patent_b, version_b),
    ):
        TextChunk.objects.create(
            tenant=tenant,
            patent=patent,
            document_version=version,
            section_type=TextChunk.SectionType.SPECIFICATION,
            text=shared_text,
            char_start=0,
            char_end=len(shared_text),
            embedding=vector,
            embedding_model=embedding_model,
            embedding_revision=embedding_revision,
        )

    monkeypatch.setattr(
        BGEEmbedder,
        "embed_texts",
        classmethod(lambda _cls, texts: [vector.copy() for _ in texts]),
    )

    def fake_rerank(
        _cls: type[CrossEncoderReranker],
        query: str,
        candidates: list[Any],
        top_n: int = 10,
    ) -> list[Any]:
        del query
        for candidate in candidates:
            candidate.rerank_score = 0.5
        return candidates[:top_n]

    monkeypatch.setattr(
        CrossEncoderReranker,
        "rerank",
        classmethod(fake_rerank),
    )

    results = HybridSearchPipeline().retrieve_evidence(
        query="wireless sensor measurements processor",
        tenant_id=tenant_a.pk,
        top_n=10,
    )

    assert results, "Expected at least one tenant-A hybrid search result."
    assert all(candidate.tenant_id == str(tenant_a.pk) for candidate in results)
    assert {candidate.patent_id for candidate in results} == {str(patent_a.pk)}
    assert str(patent_b.pk) not in {candidate.patent_id for candidate in results}


@pytest.mark.django_db(transaction=True)
@override_settings(
    CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
)
def test_same_tenant_websocket_receives_structured_progress(
    tenant_pair: TenantPair,
) -> None:
    """An authorized member receives the structured event shape expected by UI."""
    from channels.layers import get_channel_layer

    tenant_a = tenant_pair["tenant_a"]
    user_a = tenant_pair["user_a"]
    patent_a, version_a = _create_patent_version(
        tenant=tenant_a,
        user=user_a,
        label="tenant-a-websocket-positive",
    )
    analysis_a, _assessment_a = _create_analysis_with_assessment(
        tenant=tenant_a,
        user=user_a,
        patent=patent_a,
        version=version_a,
    )

    django_client = Client()
    django_client.force_login(user_a)
    session_cookie = django_client.cookies[settings.SESSION_COOKIE_NAME].value
    headers = [
        (
            b"cookie",
            f"{settings.SESSION_COOKIE_NAME}={session_cookie}".encode("ascii"),
        ),
    ]
    application = AuthMiddlewareStack(URLRouter(websocket_urlpatterns))
    communicator = WebsocketCommunicator(
        application,
        f"/ws/v1/analyses/{analysis_a.pk}/",
        headers=headers,
    )

    async def _connect_publish_receive() -> tuple[bool, dict[str, Any] | None]:
        connected, _protocol = await communicator.connect()
        if not connected:
            return False, None
        try:
            channel_layer = get_channel_layer()
            assert channel_layer is not None
            await channel_layer.group_send(
                f"analysis_{analysis_a.pk}",
                {
                    "type": "job.progress",
                    "data": {
                        "analysis_id": str(analysis_a.pk),
                        "stage": "retrieval",
                        "progress": 38,
                    },
                },
            )
            message = await communicator.receive_json_from(timeout=2)
            return True, message
        finally:
            await communicator.disconnect()

    connected, message = async_to_sync(_connect_publish_receive)()
    assert connected is True
    assert message == {
        "type": "job.progress",
        "data": {
            "analysis_id": str(analysis_a.pk),
            "stage": "retrieval",
            "progress": 38,
        },
    }


@pytest.mark.django_db
@pytest.mark.parametrize("denial", ["suspended_tenant", "revoked_membership"])
def test_inactive_tenant_context_is_denied(
    tenant_pair: TenantPair,
    denial: str,
) -> None:
    """Suspending a tenant or revoking membership immediately removes access."""
    tenant_a = tenant_pair["tenant_a"]
    user_a = tenant_pair["user_a"]
    client = APIClient()
    client.force_authenticate(user=user_a)

    membership = TenantMembership.objects.get(tenant=tenant_a, user=user_a)
    if denial == "suspended_tenant":
        tenant_a.status = Tenant.Status.SUSPENDED
        tenant_a.save(update_fields=("status",))
    else:
        membership.status = TenantMembership.Status.REVOKED
        membership.save(update_fields=("status",))

    response = client.get(
        "/api/v1/patents/",
        HTTP_X_TENANT_ID=str(tenant_a.pk),
    )
    assert response.status_code == 403


@pytest.mark.django_db
@override_settings(MEDIA_ROOT="/tmp/patentshield-cross-tenant-test-media")
def test_cross_tenant_pdf_and_report_artifacts_are_denied(
    tenant_pair: TenantPair,
) -> None:
    """Tenant A cannot fetch tenant-B PDF files, reports, or JSON/PDF exports."""
    tenant_a = tenant_pair["tenant_a"]
    tenant_b = tenant_pair["tenant_b"]
    user_b = tenant_pair["user_b"]
    patent_b, version_b = _create_patent_version(
        tenant=tenant_b,
        user=user_b,
        label="tenant-b-private-artifact",
    )
    analysis_b, _assessment_b = _create_analysis_with_assessment(
        tenant=tenant_b,
        user=user_b,
        patent=patent_b,
        version=version_b,
    )
    analysis_b.report_export_status = AnalysisReport.ReportExportStatus.COMPLETED
    analysis_b.report_storage_key = "reports/tenant-b/private.pdf"
    analysis_b.report_json_storage_key = "reports/tenant-b/private.json"
    analysis_b.save(
        update_fields=(
            "report_export_status",
            "report_storage_key",
            "report_json_storage_key",
        )
    )

    client = APIClient()
    client.force_authenticate(user=tenant_pair["user_a"])
    tenant_header = {"HTTP_X_TENANT_ID": str(tenant_a.pk)}
    pdf_response = client.get(
        f"/api/v1/patents/{patent_b.pk}/versions/{version_b.pk}/file/",
        **tenant_header,
    )
    report_status_response = client.get(
        f"/api/v1/reports/{analysis_b.pk}/status/",
        **tenant_header,
    )
    report_pdf_response = client.get(
        f"/api/v1/reports/{analysis_b.pk}/pdf/",
        **tenant_header,
    )
    report_json_response = client.get(
        f"/api/v1/reports/{analysis_b.pk}/json/",
        **tenant_header,
    )

    assert pdf_response.status_code == 404
    assert report_status_response.status_code == 404
    assert report_pdf_response.status_code == 404
    assert report_json_response.status_code == 404
    for response in (
        pdf_response,
        report_status_response,
        report_pdf_response,
        report_json_response,
    ):
        assert patent_b.title not in str(getattr(response, "data", ""))
