"""Authenticated tenant discovery API for the browser workspace."""

from __future__ import annotations

from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from tenants.models import Tenant, TenantMembership


class TenantMembershipListView(APIView):
    """List only active tenant memberships belonging to the current user."""

    permission_classes = [IsAuthenticated]

    def get(self, request: Request) -> Response:
        memberships = (
            TenantMembership.objects.select_related("tenant")
            .filter(
                user=request.user,
                status=TenantMembership.Status.ACTIVE,
                tenant__status=Tenant.Status.ACTIVE,
            )
            .order_by("tenant__name", "tenant__slug")
        )
        return Response(
            {
                "results": [
                    {
                        "id": str(membership.tenant_id),
                        "name": membership.tenant.name,
                        "slug": membership.tenant.slug,
                        "role": membership.role,
                        "membership_status": membership.status,
                    }
                    for membership in memberships
                ]
            }
        )
