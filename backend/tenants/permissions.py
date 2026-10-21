"""Tenant-aware API permissions for PatentShield."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from rest_framework.permissions import BasePermission
from rest_framework.request import Request

from tenants.models import Tenant, TenantMembership


class HasTenantAccess(BasePermission):
    """Require active membership in the tenant selected by the request."""

    message = "You do not have active access to this tenant."

    def has_permission(self, request: Request, view: Any) -> bool:
        """Resolve tenant context and attach its active membership to request."""
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return False

        tenant_identifier = request.headers.get("X-Tenant-ID") or getattr(
            view, "kwargs", {}
        ).get("tenant_id")
        if not tenant_identifier:
            return False

        try:
            tenant_id = UUID(str(tenant_identifier))
        except (AttributeError, TypeError, ValueError):
            return False

        membership = (
            TenantMembership.objects.select_related("tenant")
            .filter(
                tenant_id=tenant_id,
                user=user,
                status=TenantMembership.Status.ACTIVE,
                tenant__status=Tenant.Status.ACTIVE,
            )
            .first()
        )
        if membership is None:
            return False

        # DRF's Request wraps Django's HttpRequest. Attach context to both so
        # views and later Django middleware observe the same tenant context.
        request.tenant = membership.tenant  # type: ignore[attr-defined]
        request.tenant_membership = membership  # type: ignore[attr-defined]

        django_request = getattr(request, "_request", None)
        if django_request is not None:
            django_request.tenant = membership.tenant
            django_request.tenant_membership = membership

        return True


def RequireTenantRole(*allowed_roles: str) -> type[BasePermission]:
    """Return a DRF permission class that allows only the specified tenant roles.

    Example::

        permission_classes = [
            IsAuthenticated,
            HasTenantAccess,
            RequireTenantRole(
                TenantMembership.Role.OWNER,
                TenantMembership.Role.ADMIN,
            ),
        ]
    """
    normalized_roles = frozenset(
        role.value if isinstance(role, TenantMembership.Role) else str(role)
        for role in allowed_roles
    )
    valid_roles = {value for value, _label in TenantMembership.Role.choices}

    if not normalized_roles:
        raise ValueError("RequireTenantRole requires at least one allowed role.")

    invalid_roles = normalized_roles - valid_roles
    if invalid_roles:
        invalid_values = ", ".join(sorted(invalid_roles))
        raise ValueError(f"Unknown tenant role(s): {invalid_values}.")

    class TenantRolePermission(BasePermission):
        """Check the role on the active membership resolved earlier."""

        message = "Your tenant role does not permit this action."
        allowed_roles = normalized_roles

        def has_permission(self, request: Request, view: Any) -> bool:
            membership = getattr(request, "tenant_membership", None)
            tenant = getattr(request, "tenant", None)
            if membership is None or tenant is None:
                return False

            return bool(
                membership.status == TenantMembership.Status.ACTIVE
                and membership.tenant_id == getattr(tenant, "pk", None)
                and membership.role in self.allowed_roles
            )

    role_suffix = "_".join(sorted(normalized_roles))
    TenantRolePermission.__name__ = f"RequireTenantRole_{role_suffix}"
    TenantRolePermission.__qualname__ = TenantRolePermission.__name__
    TenantRolePermission.__module__ = __name__
    return TenantRolePermission
