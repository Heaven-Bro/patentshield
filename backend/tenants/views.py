"""Reusable tenant-scoped Django REST Framework viewsets."""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ImproperlyConfigured
from django.db.models import QuerySet
from rest_framework import viewsets
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.serializers import BaseSerializer

from tenants.permissions import HasTenantAccess


class TenantScopedViewSet(viewsets.ModelViewSet):
    """Base viewset that scopes reads and writes to the resolved tenant.

    Subclasses must define a class-level ``queryset`` so its model can be
    resolved safely. Tenant authorization is handled by ``HasTenantAccess``.
    """

    permission_classes = [IsAuthenticated, HasTenantAccess]

    def get_queryset(self) -> QuerySet[Any]:
        """Return only objects owned by the tenant in the current request."""
        tenant = getattr(self.request, "tenant", None)
        if tenant is None:
            raise PermissionDenied("Tenant context missing.")

        configured_queryset = getattr(self, "queryset", None)
        if configured_queryset is None:
            raise ImproperlyConfigured(
                f"{self.__class__.__name__} must define a class-level queryset."
            )

        model = configured_queryset.model
        return model.objects.for_tenant(tenant)

    def perform_create(self, serializer: BaseSerializer[Any]) -> None:
        """Bind newly created objects to the already-authorized tenant."""
        tenant = getattr(self.request, "tenant", None)
        if tenant is None:
            raise PermissionDenied("Tenant context missing.")
        serializer.save(tenant=tenant)
