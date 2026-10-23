"""Reviewer decision API for tenant-scoped patent claim assessments."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from django.db import transaction
from django.utils import timezone
from rest_framework import serializers, status
from rest_framework.exceptions import NotFound, PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from analyses.models import AnalysisReport, ClaimAssessment
from audit.models import AuditLog
from tenants.models import TenantMembership
from tenants.permissions import HasTenantAccess, RequireTenantRole


class AnalysisReviewRequestSerializer(serializers.Serializer):
    """Validate one review decision and its accompanying notes."""

    assessment_id = serializers.UUIDField()
    review_status = serializers.ChoiceField(
        choices=(
            ClaimAssessment.ReviewStatus.ACCEPTED,
            ClaimAssessment.ReviewStatus.CORRECTED,
            ClaimAssessment.ReviewStatus.REJECTED,
        )
    )
    reviewer_notes = serializers.CharField(
        max_length=10000,
        allow_blank=True,
        trim_whitespace=False,
    )


class AnalysisReviewView(APIView):
    """Record an explicit reviewer decision for an analysis assessment."""

    permission_classes = [
        IsAuthenticated,
        HasTenantAccess,
        RequireTenantRole(
            "OWNER",
            "ADMIN",
            "ATTORNEY",
            "ANALYST",
            "REVIEWER",
        ),
    ]

    def post(self, request: Request, analysis_id: UUID) -> Response:
        """Validate the assessment belongs to this analysis and tenant."""
        payload = AnalysisReviewRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        validated = payload.validated_data

        tenant = getattr(request, "tenant", None)
        membership = getattr(request, "tenant_membership", None)
        if tenant is None or membership is None:
            raise PermissionDenied("Tenant context missing.")
        if membership.status != TenantMembership.Status.ACTIVE:
            raise PermissionDenied("An active tenant membership is required.")

        try:
            analysis = (
                AnalysisReport.objects.for_tenant(tenant.pk)
                .select_related("patent", "target_document_version")
                .get(pk=analysis_id)
            )
        except AnalysisReport.DoesNotExist as exc:
            raise NotFound("Analysis was not found.") from exc

        if (
            analysis.patent.tenant_id != tenant.pk
            or analysis.target_document_version.tenant_id != tenant.pk
            or analysis.target_document_version.patent_id != analysis.patent_id
        ):
            raise PermissionDenied(
                "Analysis provenance does not match tenant context."
            )

        try:
            with transaction.atomic():
                assessment = (
                    ClaimAssessment.objects.for_tenant(tenant.pk)
                    .select_for_update()
                    .select_related("analysis", "claim", "claim__patent")
                    .get(
                        pk=validated["assessment_id"],
                        analysis_id=analysis.pk,
                    )
                )
                if (
                    assessment.analysis.tenant_id != tenant.pk
                    or assessment.claim.tenant_id != tenant.pk
                    or assessment.claim.patent_id != analysis.patent_id
                    or assessment.claim.patent.tenant_id != tenant.pk
                ):
                    # This should be impossible with well-formed data, but
                    # re-check FK tenant consistency before changing anything.
                    raise PermissionDenied(
                        "Assessment provenance does not match tenant context."
                    )

                assessment.review_status = validated["review_status"]
                assessment.reviewer_notes = validated["reviewer_notes"]
                assessment.reviewed_by = request.user
                assessment.reviewed_at = timezone.now()
                assessment.save(
                    update_fields=(
                        "review_status",
                        "reviewer_notes",
                        "reviewed_by",
                        "reviewed_at",
                        "updated_at",
                    )
                )

                AuditLog.objects.create(
                    tenant_id=tenant.pk,
                    actor_user_id=_uuid_or_none(request.user.pk),
                    action="REVIEW_CLAIM_ASSESSMENT",
                    resource_type="ClaimAssessment",
                    resource_id=assessment.pk,
                    request_id=_request_id(request),
                    outcome=AuditLog.Outcome.SUCCESS,
                    metadata={
                        "analysis_id": str(analysis.pk),
                        "review_status": assessment.review_status,
                    },
                )
        except ClaimAssessment.DoesNotExist as exc:
            raise NotFound(
                "Assessment was not found in the requested analysis."
            ) from exc

        return Response(
            {
                "analysis_id": str(analysis.pk),
                "assessment_id": str(assessment.pk),
                "review_status": assessment.review_status,
                "reviewer_notes": assessment.reviewer_notes,
                "reviewed_at": assessment.reviewed_at.isoformat(),
            },
            status=status.HTTP_200_OK,
        )


def _uuid_or_none(value: Any) -> UUID | None:
    """Convert compatible user IDs to the UUID type used by AuditLog."""
    try:
        return UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        return None


def _request_id(request: Request) -> str | None:
    """Return the middleware request ID or a bounded incoming request ID."""
    value = getattr(request, "request_id", None) or request.headers.get(
        "X-Request-ID"
    )
    return str(value)[:100] if value else None
