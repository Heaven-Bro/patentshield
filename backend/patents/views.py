
    
  
"""Tenant-isolated REST views for patent upload and document lifecycle APIs."""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import Any
from uuid import UUID

from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import Prefetch
from django.http import FileResponse, HttpRequest, HttpResponse
from django.urls import reverse
from rest_framework import status
from rest_framework.exceptions import APIException, NotFound, PermissionDenied
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from audit.models import AuditLog
from ingestion.tasks import process_patent_document_version
from patents.models import Patent, PatentDocumentVersion
from patents.serializers import PatentDocumentUploadSerializer
from tenants.models import Tenant, TenantMembership
from tenants.permissions import HasTenantAccess, RequireTenantRole

logger = logging.getLogger(__name__)

_UPLOAD_ROLES = (
    TenantMembership.Role.OWNER,
    TenantMembership.Role.ADMIN,
    TenantMembership.Role.ATTORNEY,
    TenantMembership.Role.ANALYST,
)


class TenantPatentAPIView(APIView):
    """Base for patent endpoints that require authenticated tenant membership."""

    permission_classes = [IsAuthenticated, HasTenantAccess]

    def get_tenant(self, request: Request) -> Tenant:
        """Return the tenant resolved by ``HasTenantAccess``."""
        tenant = getattr(request, "tenant", None)
        if not isinstance(tenant, Tenant):
            raise PermissionDenied("Tenant context missing.")
        return tenant

    def get_tenant_id(self, request: Request) -> UUID:
        """Return the selected tenant's primary key."""
        return self.get_tenant(request).pk


class PatentListView(TenantPatentAPIView):
    """List patents visible inside the selected tenant."""

    def get(self, request: Request) -> Response:
        tenant_id = self.get_tenant_id(request)
        version_queryset = PatentDocumentVersion.objects.for_tenant(tenant_id)
        patents = (
            Patent.objects.for_tenant(tenant_id)
            .order_by("-created_at")
            .prefetch_related(Prefetch("versions", queryset=version_queryset))
        )
        return Response({"results": [_serialize_patent(request, p) for p in patents]})


class PatentDocumentUploadView(TenantPatentAPIView):
    """Create a tenant-owned patent and first PDF version, then enqueue ingest."""

    parser_classes = [MultiPartParser, FormParser]

    def get_permissions(self) -> list[Any]:
        """Require an upload-capable tenant role for this mutating endpoint."""
        permission_classes = [
            IsAuthenticated,
            HasTenantAccess,
            RequireTenantRole(*_UPLOAD_ROLES),
        ]
        return [permission() for permission in permission_classes]

    def post(self, request: Request) -> Response:
        serializer = PatentDocumentUploadSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = dict(serializer.validated_data)
        uploaded_file = data.pop("file")

        tenant = self.get_tenant(request)
        patent_id = uuid.uuid4()
        version_id = uuid.uuid4()
        content_sha256 = _sha256_uploaded_file(uploaded_file)
        storage_key = (

