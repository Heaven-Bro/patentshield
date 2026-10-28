"""Tenant-authorized Office Action Assistant APIs."""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from urllib.parse import quote

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Count, Prefetch, Q
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from audit.models import AuditLog
from office_actions.models import (
    CitedReference,
    OfficeAction,
    Rejection,
    RejectionReference,
    OfficeActionReview,
    ResponseDraft,
    ResponseDraftRevision,
)
from office_actions.serializers import (
    AmendmentSupportCheckSerializer,
    AmendmentSupportSerializer,
    CreateRevisionSerializer,
    OfficeActionDetailSerializer,
    OfficeActionListSerializer,
    OfficeActionReviewSerializer,
    OfficeActionUploadSerializer,
    ResponseDraftRevisionSerializer,
    ResponseDraftSerializer,
    ReviewDecisionSerializer,
)
from office_actions.services import (
    check_amendment_support,
    create_evidence_grounded_draft,
    create_revision,
    record_review,
)
from office_actions.tasks import process_office_action
from patents.models import Patent
from tenants.models import Tenant, TenantMembership
from tenants.permissions import HasTenantAccess, RequireTenantRole

logger = logging.getLogger(__name__)

_EDITOR_ROLES = (
    TenantMembership.Role.OWNER,
    TenantMembership.Role.ADMIN,
    TenantMembership.Role.ATTORNEY,
    TenantMembership.Role.ANALYST,
)
_REVIEW_ROLES = (
    TenantMembership.Role.OWNER,
    TenantMembership.Role.ADMIN,
    TenantMembership.Role.ATTORNEY,
    TenantMembership.Role.REVIEWER,
)


def _enqueue_office_action(office_action: OfficeAction, actor) -> str | None:
    """Attempt queue dispatch without rolling back an already committed upload."""
    OfficeAction.objects.filter(pk=office_action.pk, tenant_id=office_action.tenant_id).update(
        extraction_status=OfficeAction.ExtractionStatus.PENDING,
        extraction_error_code="",
    )
    office_action.extraction_status = OfficeAction.ExtractionStatus.PENDING
    office_action.extraction_error_code = ""
    try:
        task = process_office_action.apply_async(args=[str(office_action.pk)], queue="analysis")
        return task.id
    except Exception as exc:
        OfficeAction.objects.filter(pk=office_action.pk, tenant_id=office_action.tenant_id).update(
            extraction_status=OfficeAction.ExtractionStatus.FAILED,
            extraction_error_code="ENQUEUE_FAILED",
        )
        # Keep the just-returned upload response truthful as well as updating DB state.
        office_action.extraction_status = OfficeAction.ExtractionStatus.FAILED
        office_action.extraction_error_code = "ENQUEUE_FAILED"
        try:
            AuditLog.objects.create(
                tenant_id=office_action.tenant_id,
                actor_user=actor,
                action="OFFICE_ACTION_PROCESS_ENQUEUE_FAILED",
                resource_type="OfficeAction",
                resource_id=office_action.pk,
                outcome=AuditLog.Outcome.FAILURE,
                metadata={"error_type": type(exc).__name__},
            )
        except Exception:
            logger.exception("Could not persist office-action enqueue failure audit event.")
        logger.warning("Could not enqueue office-action processing for %s (%s).", office_action.pk, type(exc).__name__)
        return None


class TenantOfficeActionAPIView(APIView):
    permission_classes = [IsAuthenticated, HasTenantAccess]

    def tenant(self, request: Request) -> Tenant:
        tenant = getattr(request, "tenant", None)
        if not isinstance(tenant, Tenant):
            raise PermissionDenied("Tenant context is unavailable.")
        return tenant

    def action(self, request: Request, action_id) -> OfficeAction:
        tenant_id = self.tenant(request).pk
        action_queryset = (
            OfficeAction.objects.for_tenant(tenant_id)
            .filter(patent__tenant_id=tenant_id)
            .select_related("patent", "created_by")
            .prefetch_related(
                Prefetch(
                    "rejections",
                    queryset=Rejection.objects.for_tenant(tenant_id).filter(office_action__tenant_id=tenant_id).prefetch_related(
                        Prefetch(
                            "reference_links",
                            queryset=RejectionReference.objects.for_tenant(tenant_id).filter(
                                rejection__tenant_id=tenant_id,
                                cited_reference__tenant_id=tenant_id,
                                cited_reference__office_action__tenant_id=tenant_id,
                            ).select_related("cited_reference"),
                        )
                    ),
                ),
                Prefetch("cited_references", queryset=CitedReference.objects.for_tenant(tenant_id).filter(office_action__tenant_id=tenant_id)),
            )
        )
        return get_object_or_404(action_queryset, pk=action_id)

    def draft(self, request: Request, draft_id) -> ResponseDraft:
        tenant_id = self.tenant(request).pk
        return get_object_or_404(
            ResponseDraft.objects.for_tenant(tenant_id).filter(office_action__tenant_id=tenant_id, office_action__patent__tenant_id=tenant_id).select_related("office_action", "created_by"),
            pk=draft_id,
        )


def _annotated_actions(tenant_id):
    return OfficeAction.objects.for_tenant(tenant_id).filter(patent__tenant_id=tenant_id).select_related("patent").annotate(
        rejection_count=Count("rejections", filter=Q(rejections__tenant_id=tenant_id), distinct=True),
        cited_reference_count=Count("cited_references", filter=Q(cited_references__tenant_id=tenant_id), distinct=True),
        draft_count=Count("drafts", filter=Q(drafts__tenant_id=tenant_id), distinct=True),
    )


class OfficeActionListCreateView(TenantOfficeActionAPIView):
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    def get(self, request: Request) -> Response:
        tenant_id = self.tenant(request).pk
        queryset = _annotated_actions(tenant_id)
        patent_id = request.query_params.get("patent_id")
        if patent_id:
            try:
                uuid.UUID(patent_id)
            except (TypeError, ValueError):
                raise ValidationError({"patent_id": "patent_id must be a UUID."})
            queryset = queryset.filter(patent_id=patent_id)
        try:
            requested_limit = int(request.query_params.get("limit", 50))
        except (TypeError, ValueError) as exc:
            raise ValidationError({"limit": "limit must be an integer."}) from exc
        limit = min(max(requested_limit, 1), 100)
        total = queryset.count()
        results = OfficeActionListSerializer(queryset[:limit], many=True).data
        return Response({"count": total, "results": results})

    def post(self, request: Request) -> Response:
        tenant = self.tenant(request)
        role_permission = RequireTenantRole(*_EDITOR_ROLES)()
        if not role_permission.has_permission(request, self):
            raise PermissionDenied("Your tenant role cannot upload office-action documents.")
        serializer = OfficeActionUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        patent = get_object_or_404(Patent.objects.for_tenant(tenant.pk), pk=values["patent_id"])
        if patent.tenant_id != tenant.pk:
            raise NotFound("Patent not found in this tenant.")
        upload = values["file"]
        upload.seek(0)
        file_bytes = upload.read()
        digest = hashlib.sha256(file_bytes).hexdigest()
        file_suffix = os.path.splitext(os.path.basename(getattr(upload, "name", "office-action.pdf")))[1].lower()
        if file_suffix != ".pdf":
            file_suffix = ".pdf"
        storage_key = f"office-actions/{tenant.pk}/{uuid.uuid4().hex}{file_suffix}"
        stored_key = default_storage.save(storage_key, ContentFile(file_bytes))
        safe_filename = os.path.basename(getattr(upload, "name", "office-action.pdf"))[:255] or "office-action.pdf"
        try:
            with transaction.atomic():
                office_action = OfficeAction.objects.create(
                    tenant=tenant,
                    patent=patent,
                    created_by=request.user,
                    jurisdiction=values["jurisdiction"],
                    action_type=values["action_type"],
                    application_number=values.get("application_number") or patent.application_number or "",
                    examiner_name=values.get("examiner_name", ""),
                    issued_date=values.get("issued_date"),
                    response_due_date=values.get("response_due_date"),
                    source_filename=safe_filename,
                    storage_key=stored_key,
                    content_sha256=digest,
                    mime_type="application/pdf",
                )
                AuditLog.objects.create(
                    tenant=tenant,
                    actor_user=request.user,
                    action="OFFICE_ACTION_UPLOADED",
                    resource_type="OfficeAction",
                    resource_id=office_action.pk,
                    metadata={"patent_id": str(patent.pk), "content_sha256": digest, "jurisdiction": office_action.jurisdiction, "action_type": office_action.action_type},
                )
                transaction.on_commit(lambda: _enqueue_office_action(office_action, request.user), robust=True)
        except Exception:
            default_storage.delete(stored_key)
            raise
        office_action.rejection_count = 0
        office_action.cited_reference_count = 0
        office_action.draft_count = 0
        return Response(OfficeActionListSerializer(office_action).data, status=status.HTTP_202_ACCEPTED)


class OfficeActionDetailView(TenantOfficeActionAPIView):
    def get(self, request: Request, action_id) -> Response:
        action = self.action(request, action_id)
        action.rejection_count = action.rejections.filter(tenant_id=action.tenant_id).count()
        action.cited_reference_count = action.cited_references.filter(tenant_id=action.tenant_id).count()
        action.draft_count = action.drafts.filter(tenant_id=action.tenant_id).count()
        return Response(OfficeActionDetailSerializer(action).data)

    def delete(self, request: Request, action_id) -> Response:
        # Retain uploaded communications as part of the prosecution record.
        raise PermissionDenied("Office-action records are retained for provenance; contact an administrator for an approved retention workflow.")


class OfficeActionFileView(TenantOfficeActionAPIView):
    def get(self, request: Request, action_id):
        action = self.action(request, action_id)
        try:
            file_obj = default_storage.open(action.storage_key, "rb")
        except (OSError, FileNotFoundError):
            raise NotFound("Office-action PDF is not available in configured storage.")
        filename = quote(action.source_filename or "office-action.pdf")
        response = FileResponse(file_obj, content_type="application/pdf")
        response["Content-Disposition"] = f"attachment; filename*=UTF-8''{filename}"
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        return response


class OfficeActionProcessView(TenantOfficeActionAPIView):
    permission_classes = [IsAuthenticated, HasTenantAccess, RequireTenantRole(*_EDITOR_ROLES)]

    def post(self, request: Request, action_id) -> Response:
        action = self.action(request, action_id)
        task_id = _enqueue_office_action(action, request.user)
        if not task_id:
            return Response({"detail": "Processing could not be queued. The upload remains stored; retry when the worker broker is available."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"office_action_id": str(action.pk), "task_id": task_id, "status": "QUEUED"}, status=status.HTTP_202_ACCEPTED)


class OfficeActionDraftListGenerateView(TenantOfficeActionAPIView):
    def get(self, request: Request, action_id) -> Response:
        action = self.action(request, action_id)
        drafts = ResponseDraft.objects.for_tenant(action.tenant_id).filter(office_action=action, office_action__tenant_id=action.tenant_id, office_action__patent__tenant_id=action.tenant_id).select_related("office_action")
        return Response({"results": ResponseDraftSerializer(drafts, many=True).data})

    def post(self, request: Request, action_id) -> Response:
        role_permission = RequireTenantRole(*_EDITOR_ROLES)()
        if not role_permission.has_permission(request, self):
            raise PermissionDenied("Your tenant role cannot generate response drafts.")
        action = self.action(request, action_id)
        try:
            draft, _revision = create_evidence_grounded_draft(action, request.user)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(ResponseDraftSerializer(draft).data, status=status.HTTP_201_CREATED)


class ResponseDraftDetailView(TenantOfficeActionAPIView):
    def get(self, request: Request, draft_id) -> Response:
        draft = self.draft(request, draft_id)
        return Response(ResponseDraftSerializer(draft).data)


class ResponseDraftSubmitForReviewView(TenantOfficeActionAPIView):
    permission_classes = [IsAuthenticated, HasTenantAccess, RequireTenantRole(TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN, TenantMembership.Role.ATTORNEY)]

    def post(self, request: Request, draft_id) -> Response:
        draft = self.draft(request, draft_id)
        if draft.status == ResponseDraft.Status.WITHDRAWN:
            raise ValidationError("Withdrawn drafts cannot be submitted for review.")
        if draft.current_revision_number <= 0 or not draft.revisions.filter(revision_number=draft.current_revision_number).exists():
            raise ValidationError("Generate or save a draft revision before submitting it for review.")
        draft.status = ResponseDraft.Status.IN_REVIEW
        draft.save(update_fields=("status", "updated_at"))
        AuditLog.objects.create(
            tenant_id=draft.tenant_id,
            actor_user=request.user,
            action="OFFICE_ACTION_DRAFT_SUBMITTED_FOR_REVIEW",
            resource_type="ResponseDraft",
            resource_id=draft.pk,
            metadata={"revision_number": draft.current_revision_number},
        )
        return Response(ResponseDraftSerializer(draft).data)


class ResponseDraftRevisionListCreateView(TenantOfficeActionAPIView):
    def get(self, request: Request, draft_id) -> Response:
        draft = self.draft(request, draft_id)
        revisions = draft.revisions.filter(tenant_id=draft.tenant_id, draft__office_action__tenant_id=draft.tenant_id, draft__office_action__patent__tenant_id=draft.tenant_id).prefetch_related("reviews").all()
        return Response({"results": ResponseDraftRevisionSerializer(revisions, many=True).data})

    def post(self, request: Request, draft_id) -> Response:
        role_permission = RequireTenantRole(TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN, TenantMembership.Role.ATTORNEY)()
        if not role_permission.has_permission(request, self):
            raise PermissionDenied("Only an attorney or tenant administrator may edit a response draft.")
        draft = self.draft(request, draft_id)
        if draft.status == ResponseDraft.Status.WITHDRAWN:
            raise ValidationError("Withdrawn drafts cannot be revised.")
        serializer = CreateRevisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        revision = create_revision(draft, request.user, serializer.validated_data["content_markdown"])
        return Response(ResponseDraftRevisionSerializer(revision).data, status=status.HTTP_201_CREATED)


class ResponseDraftReviewView(TenantOfficeActionAPIView):
    permission_classes = [IsAuthenticated, HasTenantAccess, RequireTenantRole(*_REVIEW_ROLES)]

    def post(self, request: Request, revision_id) -> Response:
        tenant_id = self.tenant(request).pk
        revision = get_object_or_404(
            ResponseDraftRevision.objects.for_tenant(tenant_id).filter(draft__tenant_id=tenant_id, draft__office_action__tenant_id=tenant_id, draft__office_action__patent__tenant_id=tenant_id).select_related("draft"),
            pk=revision_id,
        )
        serializer = ReviewDecisionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        decision = serializer.validated_data["decision"]
        if decision == OfficeActionReview.Decision.APPROVED and revision.generated_by_id == request.user.pk:
            raise PermissionDenied("A draft author cannot approve their own revision.")
        if revision.draft.status != ResponseDraft.Status.IN_REVIEW:
            raise ValidationError("The draft must be submitted for review before a decision is recorded.")
        if revision.draft.current_revision_number != revision.revision_number:
            raise ValidationError("Only the currently submitted revision can receive a review decision.")
        membership = getattr(request, "tenant_membership", None)
        if decision == OfficeActionReview.Decision.APPROVED and membership and membership.role not in {
            TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN, TenantMembership.Role.ATTORNEY,
        }:
            raise PermissionDenied("Only an attorney or tenant administrator may approve a response draft.")
        review = record_review(revision, request.user, decision, serializer.validated_data["comments"])
        return Response(OfficeActionReviewSerializer(review).data, status=status.HTTP_201_CREATED)


class AmendmentSupportView(TenantOfficeActionAPIView):
    permission_classes = [IsAuthenticated, HasTenantAccess, RequireTenantRole(*_EDITOR_ROLES)]

    def post(self, request: Request, action_id) -> Response:
        action = self.action(request, action_id)
        serializer = AmendmentSupportSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        support_check = check_amendment_support(action, serializer.validated_data["claim_number"], serializer.validated_data["proposed_claim_text"], request.user)
        AuditLog.objects.create(
            tenant_id=action.tenant_id,
            actor_user=request.user,
            action="OFFICE_ACTION_AMENDMENT_SUPPORT_CHECKED",
            resource_type="AmendmentSupportCheck",
            resource_id=support_check.pk,
            metadata={"office_action_id": str(action.pk), "status": support_check.status, "document_version_id": str(support_check.checked_document_version_id) if support_check.checked_document_version_id else None},
        )
        return Response(AmendmentSupportCheckSerializer(support_check).data, status=status.HTTP_201_CREATED)


class ProsecutionHistoryView(TenantOfficeActionAPIView):
    def get(self, request: Request) -> Response:
        patent_id = request.query_params.get("patent_id", "").strip()
        if not patent_id:
            raise ValidationError({"patent_id": "patent_id is required."})
        patent = get_object_or_404(Patent.objects.for_tenant(self.tenant(request).pk), pk=patent_id)
        actions = _annotated_actions(self.tenant(request).pk).filter(patent=patent).prefetch_related(Prefetch("rejections", queryset=Rejection.objects.for_tenant(self.tenant(request).pk).filter(office_action__tenant_id=self.tenant(request).pk)), Prefetch("cited_references", queryset=CitedReference.objects.for_tenant(self.tenant(request).pk).filter(office_action__tenant_id=self.tenant(request).pk)), Prefetch("drafts", queryset=ResponseDraft.objects.for_tenant(self.tenant(request).pk).filter(office_action__tenant_id=self.tenant(request).pk)))
        results = []
        for action in actions:
            item = OfficeActionListSerializer(action).data
            item["rejections"] = [{"id": str(row.pk), "ground": row.ground, "claim_numbers": row.claim_numbers, "source_page": row.source_page, "source_quote": row.source_quote} for row in action.rejections.all()]
            item["cited_references"] = [{"id": str(row.pk), "identifier": row.identifier, "source_page": row.source_page} for row in action.cited_references.all()]
            item["drafts"] = [{"id": str(draft.pk), "status": draft.status, "current_revision_number": draft.current_revision_number} for draft in action.drafts.all()]
            results.append(item)
        return Response({"patent_id": str(patent.pk), "patent_title": patent.title, "results": results})
