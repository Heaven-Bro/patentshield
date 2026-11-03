"""OIDC/SAML federation, SCIM 2.0, matter ACLs, session revocation and audit exports."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import secrets
import uuid
from datetime import timedelta
from urllib.parse import quote

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ImproperlyConfigured, ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator

from audit.models import AuditLog
from enterprise_identity.models import (
    FederatedIdentity, IdentityProvider, ManagedSession, MatterGrant,
    MatterPolicy, SCIMToken, SCIMUserBinding,
)
from enterprise_identity.scim_auth import HasValidSCIMToken, SCIMBearerAuthentication
from enterprise_identity.serializers import (
    IdentityProviderSerializer, ManagedSessionSerializer, MatterGrantSerializer,
    MatterPolicySerializer, SCIMTokenSerializer,
)
from enterprise_identity.services import (
    EnterpriseAuthError, audit_identity, begin_oidc, finish_oidc, finish_saml_login,
    matter_access_allowed, register_managed_session, require_matter_access,
    resolve_matter, revoke_all_django_sessions_for_user, revoke_managed_session,
    saml_auth, saml_settings_for,
)
from tenants.models import Tenant, TenantMembership
from tenants.permissions import HasTenantAccess, RequireTenantRole

User = get_user_model()
_ADMIN_ROLES = (TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN)

_SCIM_ALLOWED_SCOPES = frozenset({"Users:read", "Users:write", "Roles:manage", "Roles:admin", "*"})

def _normalize_scim_scopes(value):
    """Validate SCIM token scopes without allowing unhashable or oversized input."""
    if (
        not isinstance(value, list)
        or not value
        or len(value) > len(_SCIM_ALLOWED_SCOPES)
        or any(not isinstance(item, str) for item in value)
    ):
        raise ValidationError({"scopes": "Provide a non-empty list of supported SCIM scope strings."})
    normalized = list(dict.fromkeys(item.strip() for item in value))
    if not normalized or any(not item for item in normalized) or set(normalized) - _SCIM_ALLOWED_SCOPES:
        raise ValidationError({"scopes": "SCIM scopes must be selected from the supported scope list."})
    if "*" in normalized and len(normalized) > 1:
        raise ValidationError({"scopes": "The wildcard scope cannot be combined with individual scopes."})
    if "Roles:admin" in normalized and "Roles:manage" not in normalized and "*" not in normalized:
        raise ValidationError({"scopes": "Roles:admin requires Roles:manage."})
    return normalized



def _tenant(request: Request) -> Tenant:
    tenant = getattr(request, "tenant", None)
    if not isinstance(tenant, Tenant):
        raise PermissionDenied("Tenant context is unavailable.")
    return tenant


def _safe_sso_redirect() -> str:
    target = str(getattr(settings, "PATENTSHIELD_SSO_SUCCESS_URL", "/") or "/")
    if not target.startswith("/") or target.startswith("//") or "\\" in target or "\r" in target or "\n" in target:
        return "/"
    return target


def _auth_error(exc: Exception) -> JsonResponse:
    if isinstance(exc, EnterpriseAuthError):
        detail = str(exc)
    else:
        detail = "Enterprise authentication could not be completed."
    response = JsonResponse({"detail": detail}, status=400)
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response


class EnterpriseAdminAPIView(APIView):
    permission_classes = [IsAuthenticated, HasTenantAccess, RequireTenantRole(*_ADMIN_ROLES)]

    def tenant(self, request: Request) -> Tenant:
        return _tenant(request)


class IdentityProviderListCreateView(EnterpriseAdminAPIView):
    def get(self, request: Request) -> Response:
        tenant = self.tenant(request)
        rows = IdentityProvider.objects.for_tenant(tenant.pk).order_by("name")
        return Response({"results": IdentityProviderSerializer(rows, many=True).data})

    @transaction.atomic
    def post(self, request: Request) -> Response:
        tenant = self.tenant(request)
        serializer = IdentityProviderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        provider = serializer.save(tenant=tenant, created_by=request.user)
        audit_identity(tenant=tenant, actor=request.user, action="IDENTITY_PROVIDER_CREATED", resource_type="IdentityProvider", resource_id=provider.pk, metadata={"protocol": provider.protocol, "enabled": provider.enabled})
        return Response(IdentityProviderSerializer(provider).data, status=status.HTTP_201_CREATED)


class IdentityProviderDetailView(EnterpriseAdminAPIView):
    def get_provider(self, request: Request, provider_id) -> IdentityProvider:
        return get_object_or_404(IdentityProvider.objects.for_tenant(self.tenant(request).pk), pk=provider_id)

    def get(self, request: Request, provider_id) -> Response:
        return Response(IdentityProviderSerializer(self.get_provider(request, provider_id)).data)

    @transaction.atomic
    def patch(self, request: Request, provider_id) -> Response:
        tenant = self.tenant(request)
        provider = self.get_provider(request, provider_id)
        serializer = IdentityProviderSerializer(provider, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        provider = serializer.save()
        audit_identity(tenant=tenant, actor=request.user, action="IDENTITY_PROVIDER_UPDATED", resource_type="IdentityProvider", resource_id=provider.pk, metadata={"fields": sorted(serializer.validated_data.keys())})
        return Response(IdentityProviderSerializer(provider).data)

    @transaction.atomic
    def delete(self, request: Request, provider_id) -> Response:
        tenant = self.tenant(request)
        provider = self.get_provider(request, provider_id)
        if provider.identities.exists():
            provider.enabled = False
            provider.save(update_fields=("enabled", "updated_at"))
            result = "disabled_identity_links_retained"
        else:
            provider.delete()
            result = "deleted"
        audit_identity(tenant=tenant, actor=request.user, action="IDENTITY_PROVIDER_DEACTIVATED", resource_type="IdentityProvider", resource_id=provider.pk, metadata={"result": result})
        return Response(status=status.HTTP_204_NO_CONTENT)


class OIDCLoginStartView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request: Request, tenant_slug: str, provider_slug: str):
        provider = get_object_or_404(IdentityProvider.objects.select_related("tenant"), tenant__slug=tenant_slug, tenant__status=Tenant.Status.ACTIVE, slug=provider_slug, protocol=IdentityProvider.Protocol.OIDC, enabled=True)
        try:
            url = begin_oidc(request._request, provider)
            response = HttpResponseRedirect(url)
            response["Cache-Control"] = "no-store"
            return response
        except Exception as exc:
            return _auth_error(exc)


class OIDCCallbackView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request: Request):
        if request.query_params.get("error"):
            return _auth_error(EnterpriseAuthError("The identity provider denied the login request."))
        try:
            finish_oidc(request._request)
            response = HttpResponseRedirect(_safe_sso_redirect())
            response["Cache-Control"] = "no-store"
            return response
        except Exception as exc:
            return _auth_error(exc)


class SAMLLoginStartView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request: Request, tenant_slug: str, provider_slug: str):
        provider = get_object_or_404(IdentityProvider.objects.select_related("tenant"), tenant__slug=tenant_slug, tenant__status=Tenant.Status.ACTIVE, slug=provider_slug, protocol=IdentityProvider.Protocol.SAML, enabled=True)
        try:
            if not settings.DEBUG and str(getattr(settings, "SESSION_COOKIE_SAMESITE", "Lax")).casefold() != "none":
                raise ImproperlyConfigured("SAML POST responses require SESSION_COOKIE_SAMESITE=None in production.")
            auth = saml_auth(request._request, provider)
            redirect_url = auth.login()
            request.session[f"enterprise_saml_request_{provider.pk}"] = auth.get_last_request_id()
            request.session.save()
            response = HttpResponseRedirect(redirect_url)
            response["Cache-Control"] = "no-store"
            return response
        except Exception as exc:
            return _auth_error(exc)


@method_decorator(csrf_exempt, name="dispatch")
class SAMLACSView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request: Request, tenant_slug: str, provider_slug: str):
        provider = get_object_or_404(IdentityProvider.objects.select_related("tenant"), tenant__slug=tenant_slug, tenant__status=Tenant.Status.ACTIVE, slug=provider_slug, protocol=IdentityProvider.Protocol.SAML, enabled=True)
        try:
            if not request._request.POST.get("SAMLResponse"):
                raise EnterpriseAuthError("A SAML response is required.")
            auth = saml_auth(request._request, provider)
            finish_saml_login(request._request, provider, auth)
            response = HttpResponseRedirect(_safe_sso_redirect())
            response["Cache-Control"] = "no-store"
            return response
        except Exception as exc:
            return _auth_error(exc)


class SAMLMetadataView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request: Request, tenant_slug: str, provider_slug: str):
        provider = get_object_or_404(IdentityProvider.objects.select_related("tenant"), tenant__slug=tenant_slug, tenant__status=Tenant.Status.ACTIVE, slug=provider_slug, protocol=IdentityProvider.Protocol.SAML, enabled=True)
        try:
            auth = saml_auth(request._request, provider)
            metadata, errors = auth.get_settings().get_sp_metadata(), None
            errors = auth.get_settings().validate_metadata(metadata)
            if errors:
                return JsonResponse({"detail": "SP metadata did not validate."}, status=500)
            response = HttpResponse(metadata, content_type="application/samlmetadata+xml; charset=utf-8")
            response["Cache-Control"] = "public, max-age=300"
            return response
        except Exception as exc:
            return _auth_error(exc)


class SCIMTokenListCreateView(EnterpriseAdminAPIView):
    def get(self, request: Request) -> Response:
        rows = SCIMToken.objects.for_tenant(self.tenant(request).pk).order_by("-created_at")
        return Response({"results": SCIMTokenSerializer(rows, many=True).data})

    @transaction.atomic
    def post(self, request: Request) -> Response:
        tenant = self.tenant(request)
        label = str(request.data.get("label", "")).strip()[:120]
        if not label:
            raise ValidationError({"label": "A descriptive token label is required."})
        scopes = _normalize_scim_scopes(request.data.get("scopes", ["Users:read", "Users:write"]))
        if set(scopes) & {"Roles:admin", "*"} and getattr(getattr(request, "tenant_membership", None), "role", None) != TenantMembership.Role.OWNER:
            raise PermissionDenied("Only a tenant owner may issue SCIM tokens with unrestricted or elevated role-management scope.")
        expires_at = request.data.get("expires_at")
        if expires_at:
            expires_at = parse_datetime(str(expires_at))
            if expires_at is None or timezone.is_naive(expires_at) or expires_at <= timezone.now() or expires_at > timezone.now() + timedelta(days=365):
                raise ValidationError({"expires_at": "Use a future timezone-aware expiry no more than 365 days away."})
        else:
            # Avoid creating unbounded-lifetime bearer credentials from the UI
            # or API when an expiry was not explicitly supplied.
            expires_at = timezone.now() + timedelta(days=90)
        raw_token = "ps_scim_" + secrets.token_urlsafe(40)
        token = SCIMToken.objects.create(tenant=tenant, label=label, token_hash=hashlib.sha256(raw_token.encode()).hexdigest(), scopes=scopes, expires_at=expires_at, created_by=request.user)
        audit_identity(tenant=tenant, actor=request.user, action="SCIM_TOKEN_CREATED", resource_type="SCIMToken", resource_id=token.pk, metadata={"scopes": scopes})
        return Response({**SCIMTokenSerializer(token).data, "token": raw_token, "warning": "Copy this token now; it will not be displayed again."}, status=status.HTTP_201_CREATED)


class SCIMTokenRevokeView(EnterpriseAdminAPIView):
    @transaction.atomic
    def post(self, request: Request, token_id) -> Response:
        tenant = self.tenant(request)
        token = get_object_or_404(SCIMToken.objects.for_tenant(tenant.pk), pk=token_id)
        token.revoked_at = timezone.now()
        token.save(update_fields=("revoked_at",))
        audit_identity(tenant=tenant, actor=request.user, action="SCIM_TOKEN_REVOKED", resource_type="SCIMToken", resource_id=token.pk)
        return Response({"id": str(token.pk), "revoked": True})


class MatterPolicyListCreateView(EnterpriseAdminAPIView):
    def get(self, request: Request) -> Response:
        tenant = self.tenant(request)
        matter_type = request.query_params.get("matter_type")
        rows = MatterPolicy.objects.for_tenant(tenant.pk)
        if matter_type:
            rows = rows.filter(matter_type=matter_type)
        return Response({"results": MatterPolicySerializer(rows.order_by("matter_type", "matter_id")[:500], many=True).data})

    @transaction.atomic
    def post(self, request: Request) -> Response:
        tenant = self.tenant(request)
        serializer = MatterPolicySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        resolve_matter(tenant, values["matter_type"], values["matter_id"])
        policy, created = MatterPolicy.objects.get_or_create(
            tenant=tenant, matter_type=values["matter_type"], matter_id=values["matter_id"],
            defaults={"enforce_acl": values["enforce_acl"], "created_by": request.user, "updated_by": request.user},
        )
        if not created:
            policy.enforce_acl = values["enforce_acl"]
            policy.updated_by = request.user
            policy.save(update_fields=("enforce_acl", "updated_by", "updated_at"))
        audit_identity(tenant=tenant, actor=request.user, action="MATTER_ACL_POLICY_UPDATED", resource_type="MatterPolicy", resource_id=policy.pk, metadata={"matter_type": policy.matter_type, "enforce_acl": policy.enforce_acl, "created": created})
        return Response(MatterPolicySerializer(policy).data, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)


class MatterPolicyDetailView(EnterpriseAdminAPIView):
    @transaction.atomic
    def patch(self, request: Request, policy_id) -> Response:
        tenant = self.tenant(request)
        policy = get_object_or_404(MatterPolicy.objects.for_tenant(tenant.pk), pk=policy_id)
        serializer = MatterPolicySerializer(policy, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        if "matter_id" in values or "matter_type" in values:
            resolve_matter(tenant, values.get("matter_type", policy.matter_type), values.get("matter_id", policy.matter_id))
        for key, value in values.items():
            setattr(policy, key, value)
        policy.updated_by = request.user
        policy.save()
        audit_identity(tenant=tenant, actor=request.user, action="MATTER_ACL_POLICY_UPDATED", resource_type="MatterPolicy", resource_id=policy.pk, metadata={"enforce_acl": policy.enforce_acl})
        return Response(MatterPolicySerializer(policy).data)


class MatterGrantListCreateView(EnterpriseAdminAPIView):
    def get(self, request: Request) -> Response:
        tenant = self.tenant(request)
        rows = MatterGrant.objects.for_tenant(tenant.pk).select_related("subject_user").order_by("matter_type", "matter_id")
        if request.query_params.get("matter_type"):
            rows = rows.filter(matter_type=request.query_params["matter_type"])
        if request.query_params.get("matter_id"):
            rows = rows.filter(matter_id=request.query_params["matter_id"])
        return Response({"results": MatterGrantSerializer(rows[:1000], many=True).data})

    @transaction.atomic
    def post(self, request: Request) -> Response:
        tenant = self.tenant(request)
        serializer = MatterGrantSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        resolve_matter(tenant, values["matter_type"], values["matter_id"])
        subject_user = values.get("subject_user")
        if subject_user and not TenantMembership.objects.filter(tenant=tenant, user=subject_user, status=TenantMembership.Status.ACTIVE).exists():
            raise ValidationError({"subject_user": "The user must have active membership in this tenant."})
        grant = MatterGrant.objects.create(tenant=tenant, created_by=request.user, **values)
        audit_identity(tenant=tenant, actor=request.user, action="MATTER_ACCESS_GRANTED", resource_type="MatterGrant", resource_id=grant.pk, metadata={"matter_type": grant.matter_type, "level": grant.level, "subject_role": grant.subject_role, "subject_user_id": str(grant.subject_user_id) if grant.subject_user_id else None})
        return Response(MatterGrantSerializer(grant).data, status=status.HTTP_201_CREATED)


class MatterGrantDeleteView(EnterpriseAdminAPIView):
    @transaction.atomic
    def delete(self, request: Request, grant_id) -> Response:
        tenant = self.tenant(request)
        grant = get_object_or_404(MatterGrant.objects.for_tenant(tenant.pk), pk=grant_id)
        audit_identity(tenant=tenant, actor=request.user, action="MATTER_ACCESS_REVOKED", resource_type="MatterGrant", resource_id=grant.pk, metadata={"matter_type": grant.matter_type, "level": grant.level})
        grant.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


def _redacted_metadata(value, key: str = ""):
    if any(marker in key.casefold() for marker in ("secret", "password", "token", "authorization", "cookie", "private_key", "webhook")):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k)[:100]: _redacted_metadata(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_redacted_metadata(v, key) for v in value[:100]]
    if isinstance(value, str):
        return value[:2000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]


def _safe_csv_cell(value):
    text = str(value if value is not None else "")
    if text.startswith(("=", "+", "-", "@", "\t", "\r")):
        text = "'" + text
    return text.replace("\x00", "")[:10000]


class AdministrativeAuditExportView(EnterpriseAdminAPIView):
    """Stream tenant-scoped CSV or JSONL audit records; secrets are redacted."""
    def get(self, request: Request):
        tenant = self.tenant(request)
        rows = AuditLog.objects.for_tenant(tenant.pk).select_related("actor_user").order_by("created_at", "id")
        start = request.query_params.get("start")
        end = request.query_params.get("end")
        for value, field, lookup in ((start, "start", "created_at__gte"), (end, "end", "created_at__lte")):
            if value:
                parsed = parse_datetime(value)
                if parsed is None or timezone.is_naive(parsed):
                    raise ValidationError({field: "Use an ISO-8601 timezone-aware datetime."})
                rows = rows.filter(**{lookup: parsed})
        action = request.query_params.get("action", "").strip()[:100]
        outcome = request.query_params.get("outcome", "").strip().upper()
        if action:
            rows = rows.filter(action=action)
        if outcome:
            if outcome not in AuditLog.Outcome.values:
                raise ValidationError({"outcome": "Unsupported audit outcome."})
            rows = rows.filter(outcome=outcome)
        try:
            limit = min(max(int(request.query_params.get("limit", 10000)), 1), 10000)
        except (TypeError, ValueError) as exc:
            raise ValidationError({"limit": "limit must be an integer."}) from exc
        fmt = request.query_params.get("format", "csv").lower()
        if fmt not in {"csv", "jsonl"}:
            raise ValidationError({"format": "format must be csv or jsonl."})
        selected = list(rows[:limit + 1])
        truncated = len(selected) > limit
        selected = selected[:limit]
        audit_identity(tenant=tenant, actor=request.user, action="ADMIN_AUDIT_EXPORT", resource_type="AuditLog", metadata={"format": fmt, "row_count": len(selected), "truncated": truncated, "filters": {"action": action or None, "outcome": outcome or None, "start": start, "end": end}})
        headers = ("id", "created_at", "actor_user_id", "actor_username", "action", "resource_type", "resource_id", "request_id", "outcome", "metadata")
        def rows_as_dict(row):
            return {"id": str(row.pk), "created_at": row.created_at.isoformat(), "actor_user_id": str(row.actor_user_id or ""), "actor_username": row.actor_user.get_username() if row.actor_user else "", "action": row.action, "resource_type": row.resource_type, "resource_id": str(row.resource_id or ""), "request_id": row.request_id or "", "outcome": row.outcome, "metadata": _redacted_metadata(row.metadata)}
        if fmt == "jsonl":
            def generate_jsonl():
                for row in selected:
                    yield json.dumps(rows_as_dict(row), ensure_ascii=False, separators=(",", ":")) + "\n"
            response = StreamingHttpResponse(generate_jsonl(), content_type="application/x-ndjson; charset=utf-8")
            filename = f"patentshield-audit-{tenant.slug}.jsonl"
        elif fmt == "csv":
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(headers)
            for row in selected:
                item = rows_as_dict(row)
                writer.writerow([_safe_csv_cell(json.dumps(item[h], ensure_ascii=False) if h == "metadata" else item[h]) for h in headers])
            response = HttpResponse(buffer.getvalue(), content_type="text/csv; charset=utf-8")
            filename = f"patentshield-audit-{tenant.slug}.csv"
        response["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
        response["Cache-Control"] = "private, no-store"
        response["X-Content-Type-Options"] = "nosniff"
        response["X-Audit-Export-Truncated"] = "true" if truncated else "false"
        return response


class ManagedSessionListView(EnterpriseAdminAPIView):
    def get(self, request: Request) -> Response:
        tenant = self.tenant(request)
        rows = ManagedSession.objects.filter(user__tenant_memberships__tenant=tenant, user__tenant_memberships__status=TenantMembership.Status.ACTIVE).select_related("user").distinct().order_by("-last_seen_at")[:1000]
        current_key = request._request.session.session_key
        # Registry uses keyed HMAC digests; raw browser session keys are never stored.
        from enterprise_identity.services import session_key_hash
        current_hash = session_key_hash(current_key) if current_key else ""
        data = ManagedSessionSerializer(rows, many=True, context={"current_session_hash": current_hash}).data
        return Response({"count": len(data), "results": data})


class ManagedSessionRevokeView(EnterpriseAdminAPIView):
    @transaction.atomic
    def post(self, request: Request, session_id) -> Response:
        tenant = self.tenant(request)
        record = get_object_or_404(ManagedSession.objects.filter(user__tenant_memberships__tenant=tenant, user__tenant_memberships__status=TenantMembership.Status.ACTIVE).distinct(), pk=session_id)
        removed = revoke_managed_session(record, actor=request.user)
        audit_identity(tenant=tenant, actor=request.user, action="USER_SESSION_REVOKED", resource_type="ManagedSession", resource_id=record.pk, metadata={"target_user_id": str(record.user_id), "server_session_found": removed})
        return Response({"id": str(record.pk), "revoked": True, "server_session_found": removed})


class UserSessionsRevokeAllView(EnterpriseAdminAPIView):
    @transaction.atomic
    def post(self, request: Request, user_id) -> Response:
        tenant = self.tenant(request)
        target = get_object_or_404(User.objects.filter(tenant_memberships__tenant=tenant, tenant_memberships__status=TenantMembership.Status.ACTIVE).distinct(), pk=user_id)
        removed = revoke_all_django_sessions_for_user(target, actor=request.user)
        audit_identity(tenant=tenant, actor=request.user, action="USER_ALL_SESSIONS_REVOKED", resource_type="User", metadata={"target_user_id": str(target.pk), "session_count": removed})
        return Response({"user_id": str(target.pk), "revoked_session_count": removed})


# ----- SCIM 2.0 discovery and Users -----------------------------------------
_SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
_SCIM_PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
_SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
_SCIM_SERVICE_PROVIDER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
_SCIM_RESOURCE_TYPE_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ResourceType"
_SCIM_SCHEMA_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Schema"


class SCIMJSONParser(JSONParser):
    """Accept the SCIM-defined JSON media type used by identity platforms."""
    media_type = "application/scim+json"


class SCIMServiceProviderConfigView(APIView):
    authentication_classes = [SCIMBearerAuthentication]
    permission_classes = [HasValidSCIMToken]

    def get(self, request: Request, tenant_slug: str) -> Response:
        return Response({
            "schemas": [_SCIM_SERVICE_PROVIDER_SCHEMA],
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": int(getattr(settings, "PATENTSHIELD_SCIM_MAX_PAGE_SIZE", 100))},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [{"type": "oauthbearertoken", "name": "OAuth Bearer Token", "description": "Tenant-scoped SCIM token", "specUri": "https://www.rfc-editor.org/rfc/rfc6750", "primary": True}],
        }, content_type="application/scim+json")


class SCIMResourceTypesView(APIView):
    authentication_classes = [SCIMBearerAuthentication]
    permission_classes = [HasValidSCIMToken]

    def get(self, request: Request, tenant_slug: str) -> Response:
        return Response({
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
            "totalResults": 1, "startIndex": 1, "itemsPerPage": 1,
            "Resources": [{"schemas": [_SCIM_RESOURCE_TYPE_SCHEMA], "id": "User", "name": "User", "endpoint": "/Users", "description": "Tenant-scoped users", "schema": _SCIM_USER_SCHEMA, "schemaExtensions": []}],
        }, content_type="application/scim+json")


class SCIMSchemasView(APIView):
    authentication_classes = [SCIMBearerAuthentication]
    permission_classes = [HasValidSCIMToken]

    def get(self, request: Request, tenant_slug: str) -> Response:
        user_attributes = [
            {"name": "userName", "type": "string", "multiValued": False, "required": True, "caseExact": False, "mutability": "readWrite", "returned": "always", "uniqueness": "global"},
            {"name": "name", "type": "complex", "multiValued": False, "required": False, "mutability": "readWrite", "returned": "default", "subAttributes": [{"name": "givenName", "type": "string", "multiValued": False, "required": False, "mutability": "readWrite", "returned": "default"}, {"name": "familyName", "type": "string", "multiValued": False, "required": False, "mutability": "readWrite", "returned": "default"}]},
            {"name": "emails", "type": "complex", "multiValued": True, "required": False, "mutability": "readWrite", "returned": "default", "subAttributes": [{"name": "value", "type": "string", "multiValued": False, "required": True, "mutability": "readWrite", "returned": "default"}]},
            {"name": "active", "type": "boolean", "multiValued": False, "required": False, "mutability": "readWrite", "returned": "default"},
        ]
        return Response({"schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"], "totalResults": 1, "startIndex": 1, "itemsPerPage": 1, "Resources": [{"id": _SCIM_USER_SCHEMA, "name": "User", "description": "SCIM core User resource subset", "attributes": user_attributes, "schemas": [_SCIM_SCHEMA_SCHEMA]}]}, content_type="application/scim+json")


def _scim_error(detail: str, code: int, status_code: int):
    return Response({"schemas": [_SCIM_ERROR_SCHEMA], "detail": detail, "status": str(status_code)}, status=status_code, content_type="application/scim+json")


def _binding_for_user(token: SCIMToken, user):
    return SCIMUserBinding.objects.filter(tenant=token.tenant, user=user).first()


def _scim_resource(request, token: SCIMToken, binding: SCIMUserBinding) -> dict:
    user = binding.user
    base = request.build_absolute_uri(f"/api/v1/enterprise-auth/scim/v2/{token.tenant.slug}/Users/{user.pk}/")
    return {
        "schemas": [_SCIM_USER_SCHEMA], "id": str(user.pk), "externalId": binding.external_id,
        "userName": user.get_username(), "displayName": (" ".join([user.first_name, user.last_name]).strip() or user.get_username()),
        "name": {"givenName": user.first_name or "", "familyName": user.last_name or ""},
        "emails": ([{"value": user.email, "primary": True, "type": "work"}] if user.email else []),
        "active": bool(user.is_active and TenantMembership.objects.filter(tenant=token.tenant, user=user, status=TenantMembership.Status.ACTIVE).exists()),
        "meta": {"resourceType": "User", "location": base, "created": binding.created_at.isoformat(), "lastModified": binding.updated_at.isoformat(), "version": f'W/"{binding.updated_at.timestamp():.6f}"'},
    }


def _scim_email(data: dict) -> str:
    emails = data.get("emails", [])
    if not isinstance(emails, list):
        raise ValidationError({"emails": "emails must be an array."})
    for item in emails:
        if isinstance(item, dict) and item.get("value"):
            value = str(item["value"]).strip().casefold()
            if len(value) > 254:
                raise ValidationError({"emails": "A valid email value is required."})
            try:
                validate_email(value)
            except DjangoValidationError as exc:
                raise ValidationError({"emails": "A valid email value is required."}) from exc
            return value
    return ""


def _apply_scim_user_values(*, request, token: SCIMToken, binding: SCIMUserBinding, values: dict, create: bool = False, role: str | None = None):
    user = binding.user
    new_username = str(values.get("userName", user.get_username())).strip()
    if not new_username or len(new_username) > 150:
        raise ValidationError({"userName": "userName is required and must be at most 150 characters."})
    if User.objects.exclude(pk=user.pk).filter(username__iexact=new_username).exists():
        raise IntegrityError("userName is already provisioned.")
    email = _scim_email(values) if "emails" in values else (user.email or "")
    name = values.get("name", {}) or {}
    if not isinstance(name, dict):
        raise ValidationError({"name": "name must be an object."})
    given_name = name.get("givenName", user.first_name)
    family_name = name.get("familyName", user.last_name)
    if not isinstance(given_name, str) or not isinstance(family_name, str):
        raise ValidationError({"name": "givenName and familyName must be strings."})
    first_name = given_name[:150]
    last_name = family_name[:150]
    # Django's configured user model is global across tenants. Prevent a SCIM
    # client for one tenant from mutating the shared profile of a user with other
    # active tenant memberships; membership active state remains tenant-specific.
    other_memberships = TenantMembership.objects.filter(
        user=user, status=TenantMembership.Status.ACTIVE, tenant__status=Tenant.Status.ACTIVE,
    ).exclude(tenant=token.tenant).exists()
    if other_memberships and (new_username != user.get_username() or email != (user.email or "") or first_name != user.first_name or last_name != user.last_name):
        raise ValidationError({"detail": "Global identity fields cannot be modified by one tenant while the user has other active tenant memberships."})
    active = values.get("active", user.is_active)
    if not isinstance(active, bool):
        raise ValidationError({"active": "active must be a boolean."})
    with transaction.atomic():
        user.username = new_username
        user.email = email
        user.first_name = first_name
        user.last_name = last_name
        user.save(update_fields=("username", "email", "first_name", "last_name"))
        from enterprise_identity.services import membership_for_scim
        binding.save(update_fields=("external_id", "updated_at"))
        membership_for_scim(token=token, user=user, active=active, role=role)
        audit_identity(tenant=token.tenant, actor=token.created_by, action="SCIM_USER_UPDATED" if not create else "SCIM_USER_CREATED", resource_type="User", metadata={"target_user_id": str(user.pk), "active": active, "scim_external_id": binding.external_id})
    return binding


class SCIMUsersView(APIView):
    authentication_classes = [SCIMBearerAuthentication]
    permission_classes = [HasValidSCIMToken]
    parser_classes = [JSONParser, SCIMJSONParser]

    def get(self, request: Request, tenant_slug: str) -> Response:
        token = request.auth
        try:
            start_index = max(1, int(request.query_params.get("startIndex", 1)))
            configured_page_size = int(getattr(settings, "PATENTSHIELD_SCIM_MAX_PAGE_SIZE", 100))
            count = min(max(1, int(request.query_params.get("count", configured_page_size))), configured_page_size)
        except (TypeError, ValueError):
            return _scim_error("startIndex and count must be integers.", "invalidValue", 400)
        rows = SCIMUserBinding.objects.for_tenant(token.tenant_id).select_related("user").order_by("created_at")
        filter_value = request.query_params.get("filter", "").strip()
        if filter_value:
            match = re.fullmatch(r'(userName|externalId)\s+eq\s+"([^"\\]{1,255})"', filter_value, re.IGNORECASE)
            if not match:
                return _scim_error("Only simple userName or externalId equality filters are supported.", "invalidFilter", 400)
            attr, value = match.group(1).casefold(), match.group(2)
            if attr == "username":
                rows = rows.filter(user__username__iexact=value)
            else:
                rows = rows.filter(external_id=value)
        total = rows.count()
        selected = rows[(start_index - 1):(start_index - 1 + count)]
        resources = [_scim_resource(request._request, token, binding) for binding in selected]
        return Response({"schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"], "totalResults": total, "startIndex": start_index, "itemsPerPage": len(resources), "Resources": resources}, content_type="application/scim+json")

    @transaction.atomic
    def post(self, request: Request, tenant_slug: str) -> Response:
        token = request.auth
        data = request.data
        if not isinstance(data, dict) or not isinstance(data.get("userName"), str) or not str(data.get("externalId", "")).strip():
            return _scim_error("userName and externalId are required.", "invalidValue", 400)
        username = str(data["userName"]).strip()
        if not username or len(username) > 150:
            return _scim_error("userName must contain 1 to 150 characters.", "invalidValue", 400)
        if not isinstance(data.get("schemas", [_SCIM_USER_SCHEMA]), list) or _SCIM_USER_SCHEMA not in data.get("schemas", [_SCIM_USER_SCHEMA]):
            return _scim_error("The SCIM User schema is required.", "invalidValue", 400)
        external_id = str(data["externalId"]).strip()
        if not external_id or len(external_id) > 255:
            return _scim_error("externalId must contain 1 to 255 characters.", "invalidValue", 400)
        if SCIMUserBinding.objects.for_tenant(token.tenant_id).filter(external_id=external_id).exists():
            return _scim_error("externalId already exists.", "uniqueness", 409)
        if User.objects.filter(username__iexact=username).exists():
            return _scim_error("userName already exists; automatic email-based account linking is not allowed.", "uniqueness", 409)
        try:
            email = _scim_email(data)
        except ValidationError as exc:
            return _scim_error(str(exc.detail), "invalidValue", 400)
        if not email:
            return _scim_error("At least one email value is required for enterprise provisioning.", "invalidValue", 400)
        name = data.get("name", {}) or {}
        if not isinstance(name, dict):
            return _scim_error("name must be an object.", "invalidValue", 400)
        given_name = name.get("givenName", "")
        family_name = name.get("familyName", "")
        if not isinstance(given_name, str) or not isinstance(family_name, str):
            return _scim_error("givenName and familyName must be strings.", "invalidValue", 400)
        active = data.get("active", True)
        if not isinstance(active, bool):
            return _scim_error("active must be a boolean.", "invalidValue", 400)
        try:
            with transaction.atomic():
                # Start active only inside this transaction so membership_for_scim
                # can mark a SCIM-requested inactive account as SCIM-disabled. The
                # account state is not visible until the atomic block commits.
                user = User(username=username, email=email, first_name=given_name[:150], last_name=family_name[:150], is_active=True)
                user.set_unusable_password()
                user.save()
                binding = SCIMUserBinding.objects.create(tenant=token.tenant, external_id=external_id, user=user)
                from enterprise_identity.services import membership_for_scim
                membership_for_scim(token=token, user=user, active=active, role=TenantMembership.Role.READ_ONLY)
                audit_identity(tenant=token.tenant, actor=token.created_by, action="SCIM_USER_CREATED", resource_type="User", metadata={"target_user_id": str(user.pk), "active": active, "scim_external_id": external_id})
        except IntegrityError:
            return _scim_error("A user with this identity already exists.", "uniqueness", 409)
        return Response(_scim_resource(request._request, token, binding), status=201, content_type="application/scim+json", headers={"Location": request.build_absolute_uri(f"/api/v1/enterprise-auth/scim/v2/{tenant_slug}/Users/{user.pk}/")})


class SCIMUserDetailView(APIView):
    authentication_classes = [SCIMBearerAuthentication]
    permission_classes = [HasValidSCIMToken]
    parser_classes = [JSONParser, SCIMJSONParser]

    def binding(self, request, user_id):
        token = request.auth
        return get_object_or_404(SCIMUserBinding.objects.for_tenant(token.tenant_id).select_related("user"), user_id=user_id)

    def get(self, request: Request, tenant_slug: str, user_id) -> Response:
        return Response(_scim_resource(request._request, request.auth, self.binding(request, user_id)), content_type="application/scim+json")

    @transaction.atomic
    def put(self, request: Request, tenant_slug: str, user_id) -> Response:
        token = request.auth
        binding = self.binding(request, user_id)
        if not isinstance(request.data, dict):
            return _scim_error("A SCIM User JSON object is required.", "invalidSyntax", 400)
        if request.data.get("externalId") and str(request.data["externalId"]) != binding.external_id:
            return _scim_error("externalId is immutable for a provisioned user.", "mutability", 400)
        scopes = token.scopes if isinstance(token.scopes, list) else []
        desired_role = request.data.get("role")
        if desired_role is not None:
            if not ("Roles:manage" in scopes or "*" in scopes):
                return _scim_error("This token cannot manage tenant roles.", "insufficientScope", 403)
            if desired_role not in TenantMembership.Role.values:
                return _scim_error("Unsupported role.", "invalidValue", 400)
            if desired_role == TenantMembership.Role.OWNER:
                return _scim_error("Tenant ownership must be assigned locally by a tenant owner.", "mutability", 400)
            if desired_role == TenantMembership.Role.ADMIN and not ({"Roles:admin", "*"} & set(scopes)):
                return _scim_error("This token cannot assign elevated tenant roles.", "insufficientScope", 403)
        try:
            _apply_scim_user_values(request=request._request, token=token, binding=binding, values=dict(request.data), role=desired_role)
        except (ValidationError, IntegrityError) as exc:
            return _scim_error("The SCIM user resource is invalid or conflicts with an existing account.", "invalidValue", 400)
        return Response(_scim_resource(request._request, token, binding), content_type="application/scim+json")

    @transaction.atomic
    def patch(self, request: Request, tenant_slug: str, user_id) -> Response:
        token = request.auth
        binding = self.binding(request, user_id)
        payload = request.data
        if not isinstance(payload, dict) or payload.get("schemas") != [_SCIM_PATCH_SCHEMA] or not isinstance(payload.get("Operations"), list) or not payload["Operations"]:
            return _scim_error("A valid SCIM PatchOp body is required.", "invalidSyntax", 400)
        values = {"userName": binding.user.get_username(), "emails": ([{"value": binding.user.email}] if binding.user.email else []), "name": {"givenName": binding.user.first_name, "familyName": binding.user.last_name}, "active": bool(binding.user.is_active)}
        allowed_paths = {"active", "userName", "name.givenName", "name.familyName", "emails"}
        for operation in payload["Operations"]:
            if not isinstance(operation, dict) or str(operation.get("op", "")).casefold() not in {"add", "replace", "remove"}:
                return _scim_error("Each patch operation must use add, replace, or remove.", "invalidSyntax", 400)
            path = str(operation.get("path", "")).strip()
            if path not in allowed_paths:
                return _scim_error("This SCIM attribute path is not mutable.", "mutability", 400)
            op = str(operation["op"]).casefold()
            value = operation.get("value")
            if op == "remove":
                if path == "active":
                    values["active"] = False
                elif path == "emails":
                    values["emails"] = []
                elif path == "name.givenName":
                    values["name"]["givenName"] = ""
                elif path == "name.familyName":
                    values["name"]["familyName"] = ""
                else:
                    return _scim_error("userName cannot be removed.", "mutability", 400)
            elif path == "active":
                if not isinstance(value, bool):
                    return _scim_error("active must be boolean.", "invalidValue", 400)
                values["active"] = value
            elif path == "userName":
                if not isinstance(value, str) or not value.strip():
                    return _scim_error("userName must be a non-empty string.", "invalidValue", 400)
                values["userName"] = value.strip()
            elif path == "emails":
                values["emails"] = value if isinstance(value, list) else ([value] if isinstance(value, dict) else [])
            elif path == "name.givenName":
                values["name"]["givenName"] = str(value or "")
            elif path == "name.familyName":
                values["name"]["familyName"] = str(value or "")
        try:
            _apply_scim_user_values(request=request._request, token=token, binding=binding, values=values)
        except (ValidationError, IntegrityError):
            return _scim_error("The SCIM patch could not be applied atomically.", "invalidValue", 400)
        return Response(_scim_resource(request._request, token, binding), content_type="application/scim+json")

    @transaction.atomic
    def delete(self, request: Request, tenant_slug: str, user_id) -> Response:
        token = request.auth
        binding = self.binding(request, user_id)
        from enterprise_identity.services import membership_for_scim
        membership_for_scim(token=token, user=binding.user, active=False)
        audit_identity(tenant=token.tenant, actor=token.created_by, action="SCIM_USER_DEPROVISIONED", resource_type="User", metadata={"target_user_id": str(binding.user_id), "scim_external_id": binding.external_id})
        return Response(status=status.HTTP_204_NO_CONTENT)
