"""Bearer authentication for inbound SCIM clients; no browser session fallback."""
from __future__ import annotations

import hashlib

from django.contrib.auth.models import AnonymousUser
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import BasePermission

from enterprise_identity.models import SCIMToken
from tenants.models import Tenant


class SCIMBearerAuthentication(BaseAuthentication):
    keyword = b"bearer"

    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts:
            return None
        if len(parts) != 2 or parts[0].lower() != self.keyword:
            raise AuthenticationFailed("A valid SCIM bearer token is required.")
        raw = parts[1].decode("utf-8", errors="ignore")
        if not raw.startswith("ps_scim_") or len(raw) < 40:
            raise AuthenticationFailed("A valid SCIM bearer token is required.")
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        token = SCIMToken.objects.select_related("tenant", "created_by").filter(token_hash=digest, revoked_at__isnull=True).first()
        if token is None or not token.is_active or token.tenant.status != Tenant.Status.ACTIVE:
            raise AuthenticationFailed("The SCIM bearer token is invalid or expired.")
        token.last_used_at = timezone.now()
        token.save(update_fields=("last_used_at",))
        return AnonymousUser(), token

    def authenticate_header(self, request):
        return "Bearer"


class HasValidSCIMToken(BasePermission):
    message = "This SCIM token is not authorized for the requested tenant or operation."

    def has_permission(self, request, view):
        token = getattr(request, "auth", None)
        if not isinstance(token, SCIMToken) or not token.is_active:
            return False
        tenant_slug = getattr(view, "kwargs", {}).get("tenant_slug")
        if not tenant_slug or token.tenant.slug != tenant_slug:
            return False
        required_scope = "Users:write" if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"} else "Users:read"
        scopes = token.scopes if isinstance(token.scopes, list) else []
        if required_scope in scopes or "*" in scopes:
            return True
        return False
