"""Security-sensitive enterprise identity and matter authorization services.

Protocol parsing is delegated to Authlib (OIDC/JWT) and python3-saml (SAML XML
signatures). This module deliberately fails closed if an adapter is unavailable.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import socket
import ipaddress
import time
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

import requests
from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.contrib.sessions.models import Session
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q, Subquery
from django.http import HttpRequest
from django.utils import timezone

from audit.models import AuditLog
from tenants.models import Tenant, TenantMembership
from enterprise_identity.models import (
    FederatedIdentity,
    IdentityProvider,
    ManagedSession,
    MatterGrant,
    MatterPolicy,
    SCIMToken,
    SCIMUserBinding,
)

User = get_user_model()


class EnterpriseAuthError(Exception):
    """A safe, user-displayable federation/provisioning failure."""


def audit_identity(*, tenant: Tenant, actor, action: str, resource_type: str, resource_id=None, metadata=None, outcome=AuditLog.Outcome.SUCCESS) -> None:
    """Persist a security audit event; failures must abort the surrounding change.

    Callers that mutate identity, grant, or credential state wrap both the mutation
    and this write in one ``transaction.atomic()`` block. Never swallow audit
    persistence exceptions for security-sensitive state transitions.
    """
    with transaction.atomic():
        AuditLog.objects.create(
            tenant=tenant,
            actor_user_id=actor.pk if getattr(actor, "is_authenticated", False) else None,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            outcome=outcome,
            metadata=metadata or {},
        )


def _fernet() -> Fernet:
    key = str(getattr(settings, "PATENTSHIELD_ENTERPRISE_ENCRYPTION_KEY", "") or "").strip()
    if not key:
        raise ImproperlyConfigured("PATENTSHIELD_ENTERPRISE_ENCRYPTION_KEY is required to encrypt identity-provider secrets.")
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ImproperlyConfigured("PATENTSHIELD_ENTERPRISE_ENCRYPTION_KEY must be a valid Fernet key.") from exc


def encrypt_client_secret(value: str) -> str:
    if not value:
        return ""
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt_client_secret(provider: IdentityProvider) -> str:
    if not provider.client_secret_ciphertext:
        return ""
    try:
        return _fernet().decrypt(provider.client_secret_ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeEncodeError) as exc:
        raise ImproperlyConfigured("Identity-provider secret cannot be decrypted; check the configured encryption key.") from exc


def _https_url(value: str, *, allow_localhost: bool = False) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
        return True
    return bool(allow_localhost and parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})


def _safe_external_endpoint(url: str, *, allow_localhost: bool = False) -> bool:
    """Require HTTPS and reject hostnames resolving to non-public IP ranges.

    Deployment egress controls and an IdP host allowlist remain recommended: DNS
    checks reduce SSRF risk but cannot fully eliminate DNS-rebinding races.
    """
    if not _https_url(url, allow_localhost=allow_localhost):
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").rstrip(".").casefold()
    if allow_localhost and host in {"localhost", "127.0.0.1", "::1"}:
        return True
    allowed_hosts = {
        item.strip().rstrip(".").casefold()
        for item in str(getattr(settings, "PATENTSHIELD_IDP_ALLOWED_HOSTS", "") or "").split(",")
        if item.strip()
    }
    # Fail closed: an empty allowlist must never silently mean "any public host".
    if not allowed_hosts or host not in allowed_hosts:
        return False
    try:
        answers = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except (OSError, ValueError):
        return False
    addresses = {answer[4][0].split("%", 1)[0] for answer in answers}
    if not addresses:
        return False
    try:
        return all(ipaddress.ip_address(address).is_global for address in addresses)
    except ValueError:
        return False



def _read_bounded_response(response, *, max_bytes: int = 1_048_576) -> bytes:
    """Read a response with a strict body limit even when Content-Length is absent."""
    chunks = bytearray()
    for chunk in response.iter_content(chunk_size=65_536):
        if not chunk:
            continue
        chunks.extend(chunk)
        if len(chunks) > max_bytes:
            response.close()
            raise EnterpriseAuthError("The identity provider response exceeded the permitted size.")
    return bytes(chunks)

def _get_json(url: str) -> dict[str, Any]:
    allow_localhost = bool(getattr(settings, "DEBUG", False))
    if not _safe_external_endpoint(url, allow_localhost=allow_localhost):
        raise EnterpriseAuthError("Federation endpoints must use an approved HTTPS host with a public network address.")
    try:
        with requests.get(url, timeout=(3.05, 8), allow_redirects=False, stream=True, headers={"Accept": "application/json"}) as response:
            if response.status_code != 200:
                raise EnterpriseAuthError("The identity provider metadata request failed.")
            payload = _read_bounded_response(response)
        value = json.loads(payload.decode("utf-8"))
    except (requests.RequestException, ValueError, UnicodeDecodeError) as exc:
        raise EnterpriseAuthError("The identity provider is temporarily unavailable.") from exc
    if not isinstance(value, dict):
        raise EnterpriseAuthError("The identity provider returned invalid metadata.")
    return value


def oidc_discovery(provider: IdentityProvider) -> dict[str, Any]:
    discovery = _get_json(provider.discovery_url)
    if discovery.get("issuer") != provider.issuer:
        raise EnterpriseAuthError("The configured OIDC issuer does not match discovery metadata.")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not isinstance(discovery.get(key), str) or not _safe_external_endpoint(discovery[key], allow_localhost=bool(settings.DEBUG)):
            raise EnterpriseAuthError("The identity provider has an invalid endpoint configuration.")
    return discovery


def begin_oidc(request: HttpRequest, provider: IdentityProvider) -> str:
    if provider.protocol != IdentityProvider.Protocol.OIDC or not provider.enabled:
        raise EnterpriseAuthError("This identity provider is not enabled for OIDC login.")
    discovery = oidc_discovery(provider)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    import base64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode("ascii")
    transactions = request.session.get("enterprise_oidc_transactions", {})
    if not isinstance(transactions, dict):
        transactions = {}
    now = int(time.time())
    transactions = {k: v for k, v in transactions.items() if isinstance(v, dict) and now - int(v.get("created_at", 0)) < 600}
    if len(transactions) >= 4:
        transactions = dict(list(transactions.items())[-3:])
    transactions[state] = {"provider_id": str(provider.pk), "tenant_id": str(provider.tenant_id), "nonce": nonce, "verifier": verifier, "created_at": now}
    request.session["enterprise_oidc_transactions"] = transactions
    request.session.save()
    from urllib.parse import urlencode
    redirect_uri = str(getattr(settings, "PATENTSHIELD_OIDC_CALLBACK_URL", "")).strip()
    if not _https_url(redirect_uri, allow_localhost=bool(settings.DEBUG)):
        raise ImproperlyConfigured("PATENTSHIELD_OIDC_CALLBACK_URL must be an absolute HTTPS callback URL.")
    scopes = provider.scopes if isinstance(provider.scopes, list) and provider.scopes else ["openid", "email", "profile"]
    params = {
        "response_type": "code", "client_id": provider.client_id,
        "redirect_uri": redirect_uri, "scope": " ".join(str(x) for x in scopes),
        "state": state, "nonce": nonce, "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return discovery["authorization_endpoint"] + "?" + urlencode(params)


def _exchange_oidc_code(provider: IdentityProvider, code: str, verifier: str) -> tuple[dict[str, Any], dict[str, Any]]:
    discovery = oidc_discovery(provider)
    callback = str(getattr(settings, "PATENTSHIELD_OIDC_CALLBACK_URL", "")).strip()
    body = {"grant_type": "authorization_code", "code": code, "redirect_uri": callback, "client_id": provider.client_id, "code_verifier": verifier}
    headers = {"Accept": "application/json"}
    secret = decrypt_client_secret(provider)
    auth = None
    if secret:
        if provider.client_auth_method == IdentityProvider.ClientAuthMethod.BASIC:
            auth = (provider.client_id, secret)
        else:
            body["client_secret"] = secret
    try:
        with requests.post(discovery["token_endpoint"], data=body, auth=auth, headers=headers, timeout=(3.05, 10), allow_redirects=False, stream=True) as response:
            if response.status_code != 200:
                raise EnterpriseAuthError("The identity provider token exchange failed.")
            payload = _read_bounded_response(response)
        token_data = json.loads(payload.decode("utf-8"))
    except (requests.RequestException, ValueError, UnicodeDecodeError) as exc:
        raise EnterpriseAuthError("The identity provider token exchange failed.") from exc
    if not isinstance(token_data, dict) or not isinstance(token_data.get("id_token"), str):
        raise EnterpriseAuthError("The identity provider did not return a valid ID token.")
    try:
        from authlib.jose import JsonWebKey, jwt
    except ImportError as exc:
        raise EnterpriseAuthError("OIDC support is unavailable; install the pinned enterprise identity dependencies.") from exc
    jwks = _get_json(discovery["jwks_uri"])
    header = jwt.get_unverified_header(token_data["id_token"])
    if header.get("alg") not in {"RS256", "ES256", "PS256"} or not header.get("kid"):
        raise EnterpriseAuthError("The ID token uses an unsupported signing algorithm.")
    key_set = JsonWebKey.import_key_set(jwks)
    try:
        claims = jwt.decode(token_data["id_token"], key_set)
        claims.validate(leeway=60)
    except Exception as exc:
        raise EnterpriseAuthError("The identity provider ID token failed validation.") from exc
    return dict(claims), token_data


def _role_from_groups(provider: IdentityProvider, groups: list[str]) -> str:
    mapping = provider.group_role_mapping if isinstance(provider.group_role_mapping, dict) else {}
    # Tenant ownership is a bootstrap/control-plane privilege and must be granted
    # locally, not by an unreviewed identity-provider claim. ADMIN mapping remains
    # possible only when an administrator explicitly configures a trusted IdP.
    for group in groups:
        mapped = mapping.get(group)
        if mapped in TenantMembership.Role.values and mapped != TenantMembership.Role.OWNER:
            return mapped
    fallback = provider.default_role
    return fallback if fallback != TenantMembership.Role.OWNER else TenantMembership.Role.READ_ONLY


def _provision_or_resolve_identity(*, request: HttpRequest, provider: IdentityProvider, subject: str, email: str, email_verified: bool, groups: list[str]) -> User:
    if not subject or len(subject) > 512:
        raise EnterpriseAuthError("The identity provider subject is invalid.")
    tenant = provider.tenant
    email = (email or "").strip().casefold()
    if len(email) > 254:
        raise EnterpriseAuthError("The identity provider returned an invalid email address.")
    if email:
        try:
            validate_email(email)
        except ValidationError as exc:
            raise EnterpriseAuthError("The identity provider returned an invalid email address.") from exc
    if email and provider.allowed_email_domains:
        allowed = {str(d).strip().casefold().lstrip("@") for d in provider.allowed_email_domains if str(d).strip()}
        domain = email.rsplit("@", 1)[-1] if email.count("@") == 1 else ""
        if not domain or domain not in allowed:
            raise EnterpriseAuthError("This account's email domain is not permitted for this tenant.")
    with transaction.atomic():
        link = FederatedIdentity.objects.select_for_update().select_related("user").filter(tenant=tenant, provider=provider, subject_id=subject).first()
        if link:
            user = link.user
            if not user.is_active:
                raise EnterpriseAuthError("This account is disabled.")
            link.email_verified = email_verified
            link.groups = groups[:100]
            link.last_login_at = timezone.now()
            link.save(update_fields=("email_verified", "groups", "last_login_at"))
            membership = TenantMembership.objects.filter(tenant=tenant, user=user).first()
            if membership is None or membership.status != TenantMembership.Status.ACTIVE:
                raise EnterpriseAuthError("Your tenant membership is not active; contact the tenant administrator.")
            return user

        if not provider.jit_provisioning:
            raise EnterpriseAuthError("This identity is not provisioned for the tenant.")
        if not email or not email_verified or not provider.trust_email_from_idp:
            raise EnterpriseAuthError("Just-in-time provisioning requires a trusted, verified email claim.")
        existing = User.objects.filter(email__iexact=email).first()
        if existing and not provider.allow_existing_email_link:
            raise EnterpriseAuthError("An account with this email already exists; an administrator must link the identity explicitly.")
        user = existing
        if user is None:
            base = (email.split("@", 1)[0] or "enterprise-user").strip()[:120]
            username = base
            for _ in range(12):
                if not User.objects.filter(username=username).exists():
                    break
                username = f"{base[:100]}-{secrets.token_hex(4)}"
            else:
                raise EnterpriseAuthError("A unique local username could not be provisioned.")
            user = User(username=username, email=email, is_active=True)
            user.set_unusable_password()
            user.save()
        elif not user.is_active:
            raise EnterpriseAuthError("This local account is disabled.")

        membership, created = TenantMembership.objects.get_or_create(
            tenant=tenant, user=user,
            defaults={"role": _role_from_groups(provider, groups), "status": TenantMembership.Status.ACTIVE},
        )
        if not created and membership.status != TenantMembership.Status.ACTIVE:
            raise EnterpriseAuthError("The existing tenant membership is not active.")
        FederatedIdentity.objects.create(tenant=tenant, provider=provider, user=user, subject_id=subject, email_verified=email_verified, groups=groups[:100], last_login_at=timezone.now())
        return user


def finish_oidc(request: HttpRequest) -> User:
    state = request.GET.get("state", "")
    code = request.GET.get("code", "")
    transactions = request.session.get("enterprise_oidc_transactions", {})
    transaction_data = transactions.pop(state, None) if isinstance(transactions, dict) and state else None
    request.session["enterprise_oidc_transactions"] = transactions if isinstance(transactions, dict) else {}
    if not isinstance(transaction_data, dict) or not code or int(time.time()) - int(transaction_data.get("created_at", 0)) >= 600:
        raise EnterpriseAuthError("The OIDC login response is missing or expired.")
    provider = IdentityProvider.objects.select_related("tenant").filter(pk=transaction_data.get("provider_id"), tenant_id=transaction_data.get("tenant_id"), enabled=True, protocol=IdentityProvider.Protocol.OIDC, tenant__status=Tenant.Status.ACTIVE).first()
    if provider is None:
        raise EnterpriseAuthError("The configured identity provider is unavailable.")
    claims, _token_data = _exchange_oidc_code(provider, code, transaction_data["verifier"])
    aud = claims.get("aud")
    audiences = aud if isinstance(aud, list) else [aud]
    if claims.get("iss") != provider.issuer or provider.client_id not in audiences or claims.get("nonce") != transaction_data.get("nonce"):
        raise EnterpriseAuthError("The OIDC issuer, audience, or nonce is invalid.")
    if len(audiences) > 1 and claims.get("azp") != provider.client_id:
        raise EnterpriseAuthError("The OIDC authorized party is invalid.")
    email = claims.get("email", "")
    email_verified = claims.get("email_verified") is True
    group_values = claims.get(provider.group_claim, [])
    if isinstance(group_values, str):
        group_values = [group_values]
    groups = [str(g)[:250] for g in group_values[:100]] if isinstance(group_values, list) else []
    user = _provision_or_resolve_identity(request=request, provider=provider, subject=str(claims.get("sub", "")), email=str(email), email_verified=email_verified, groups=groups)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    register_managed_session(request, user, auth_source="OIDC")
    audit_identity(tenant=provider.tenant, actor=user, action="ENTERPRISE_OIDC_LOGIN", resource_type="IdentityProvider", resource_id=provider.pk, metadata={"provider_protocol": "OIDC"})
    return user


def saml_settings_for(provider: IdentityProvider, request: HttpRequest) -> dict[str, Any]:
    callback = str(getattr(settings, "PATENTSHIELD_SAML_ACS_BASE_URL", "")).strip().rstrip("/")
    metadata = str(getattr(settings, "PATENTSHIELD_SAML_METADATA_BASE_URL", "")).strip().rstrip("/")
    if not _https_url(callback, allow_localhost=bool(settings.DEBUG)) or not _https_url(metadata, allow_localhost=bool(settings.DEBUG)):
        raise ImproperlyConfigured("SAML ACS and metadata base URLs must be absolute HTTPS URLs.")
    acs = f"{callback}/{provider.tenant.slug}/{provider.slug}/acs/"
    sp_entity = provider.sp_entity_id or f"{metadata}/{provider.tenant.slug}/{provider.slug}/metadata/"
    cert = provider.idp_x509_certificate.strip()
    if "BEGIN CERTIFICATE" not in cert:
        cert = "-----BEGIN CERTIFICATE-----\n" + "\n".join(cert.split()) + "\n-----END CERTIFICATE-----"
    return {
        "strict": True,
        "debug": False,
        "sp": {"entityId": sp_entity, "assertionConsumerService": {"url": acs, "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"}, "x509cert": "", "privateKey": ""},
        "idp": {"entityId": provider.idp_entity_id, "singleSignOnService": {"url": provider.sso_url, "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"}, "x509cert": cert},
        "security": {"authnRequestsSigned": False, "logoutRequestSigned": False, "logoutResponseSigned": False, "signMetadata": False, "wantMessagesSigned": True, "wantAssertionsSigned": True, "wantAssertionsEncrypted": False, "wantNameId": True, "wantNameIdEncrypted": False, "requestedAuthnContext": False, "allowRepeatAttributeName": False, "wantXMLValidation": True, "rejectUnsolicitedResponsesWithInResponseTo": True, "signatureAlgorithm": "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256", "digestAlgorithm": "http://www.w3.org/2001/04/xmlenc#sha256"},
    }


def _saml_request_data(request: HttpRequest) -> dict[str, Any]:
    return {
        "https": "on" if request.is_secure() else "off",
        "http_host": request.get_host(),
        "server_port": str(request.get_port()),
        "script_name": request.path,
        "get_data": request.GET.copy(),
        "post_data": request.POST.copy(),
        "query_string": request.META.get("QUERY_STRING", ""),
    }


def saml_auth(request: HttpRequest, provider: IdentityProvider):
    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
    except ImportError as exc:
        raise EnterpriseAuthError("SAML support is unavailable; install the pinned enterprise identity dependencies and XML security libraries.") from exc
    if provider.protocol != IdentityProvider.Protocol.SAML or not provider.enabled:
        raise EnterpriseAuthError("This identity provider is not enabled for SAML login.")
    if not provider.idp_entity_id or not provider.sso_url or not provider.idp_x509_certificate:
        raise EnterpriseAuthError("The SAML identity provider configuration is incomplete.")
    if not _safe_external_endpoint(provider.sso_url, allow_localhost=bool(settings.DEBUG)):
        raise EnterpriseAuthError(
            "The SAML SSO endpoint must use an approved HTTPS host with a public network address."
        )
    try:
        return OneLogin_Saml2_Auth(_saml_request_data(request), settings=saml_settings_for(provider, request))
    except Exception as exc:
        raise EnterpriseAuthError("The SAML service provider configuration is invalid.") from exc


def finish_saml_login(request: HttpRequest, provider: IdentityProvider, auth) -> User:
    expected_request_id = request.session.pop(f"enterprise_saml_request_{provider.pk}", None)
    if not expected_request_id:
        raise EnterpriseAuthError("The SAML response is not associated with an active sign-in request.")
    auth.process_response(expected_request_id)
    errors = auth.get_errors()
    if errors or not auth.is_authenticated():
        # Never echo SAML toolkit diagnostics, assertion data, or raw XML to users.
        raise EnterpriseAuthError("The SAML response could not be validated.")
    subject = auth.get_nameid()
    attrs = auth.get_attributes()
    email_values = attrs.get(provider.saml_email_attribute, [])
    email = str(email_values[0]) if isinstance(email_values, list) and email_values else ""
    group_values = attrs.get(provider.saml_groups_attribute, [])
    if isinstance(group_values, str):
        group_values = [group_values]
    groups = [str(g)[:250] for g in group_values[:100]] if isinstance(group_values, list) else []
    user = _provision_or_resolve_identity(request=request, provider=provider, subject=str(subject or ""), email=email, email_verified=bool(provider.trust_email_from_idp), groups=groups)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    register_managed_session(request, user, auth_source="SAML")
    audit_identity(tenant=provider.tenant, actor=user, action="ENTERPRISE_SAML_LOGIN", resource_type="IdentityProvider", resource_id=provider.pk, metadata={"provider_protocol": "SAML"})
    return user


def session_key_hash(session_key: str) -> str:
    return hmac.new(str(settings.SECRET_KEY).encode(), session_key.encode(), hashlib.sha256).hexdigest()


def register_managed_session(request: HttpRequest, user, *, auth_source: str) -> None:
    try:
        request.session.save()
        session_key = request.session.session_key
        if not session_key:
            return
        ManagedSession.objects.update_or_create(
            session_key_hash=session_key_hash(session_key),
            defaults={"user": user, "auth_source": auth_source[:24], "user_agent": request.META.get("HTTP_USER_AGENT", "")[:300], "revoked_at": None, "revoked_by": None, "last_seen_at": timezone.now()},
        )
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Could not register managed session.")


def _revoke_session_row(session_row: Session) -> None:
    session_row.delete()


def revoke_managed_session(session_record: ManagedSession, *, actor=None) -> bool:
    now = timezone.now()
    found = False
    # The raw Django session key is not stored in our registry. Scan only active
    # server-side sessions and compare keyed digests to locate the target.
    for row in Session.objects.filter(expire_date__gt=now).iterator(chunk_size=500):
        digest = session_key_hash(row.session_key)
        if hmac.compare_digest(digest, session_record.session_key_hash):
            _revoke_session_row(row)
            found = True
            break
    session_record.revoked_at = now
    session_record.revoked_by = actor if getattr(actor, "is_authenticated", False) else None
    session_record.save(update_fields=("revoked_at", "revoked_by"))
    return found


def revoke_all_django_sessions_for_user(user, *, actor=None) -> int:
    now = timezone.now()
    removed = 0
    user_id = str(user.pk)
    for row in Session.objects.filter(expire_date__gt=now).iterator(chunk_size=500):
        try:
            decoded = row.get_decoded()
        except Exception:
            decoded = {}
        if str(decoded.get("_auth_user_id", "")) == user_id:
            digest = session_key_hash(row.session_key)
            ManagedSession.objects.filter(session_key_hash=digest, revoked_at__isnull=True).update(
                revoked_at=now,
                revoked_by=actor if getattr(actor, "is_authenticated", False) else None,
            )
            row.delete()
            removed += 1
    ManagedSession.objects.filter(user=user, revoked_at__isnull=True).update(revoked_at=now, revoked_by=actor if getattr(actor, "is_authenticated", False) else None)
    return removed


_MATTER_MODEL_PATHS = {
    "patent": ("patents.models", "Patent"),
    "analysis": ("analyses.models", "AnalysisReport"),
    "claim_chart": ("reports.models", "ClaimChart"),
    "office_action": ("office_actions.models", "OfficeAction"),
    "fto_study": ("fto.models", "FTOStudy"),
    "product_matter": ("fto.models", "ProductMatter"),
    "landscape_study": ("landscapes.models", "LandscapeStudy"),
    "portfolio_valuation": ("portfolio_valuation.models", "PortfolioValuationStudy"),
}


def resolve_matter(tenant: Tenant, matter_type: str, matter_id):
    path = _MATTER_MODEL_PATHS.get(matter_type)
    if not path:
        raise ValidationError({"matter_type": "Unsupported matter type."})
    module = __import__(path[0], fromlist=[path[1]])
    model = getattr(module, path[1])
    try:
        return model.objects.for_tenant(tenant.pk).get(pk=matter_id)
    except model.DoesNotExist as exc:
        raise ValidationError({"matter_id": "Matter was not found in this tenant."}) from exc


def _role_default_allows(role: str, required: str) -> bool:
    roles = {
        "READ": {"OWNER", "ADMIN", "ATTORNEY", "ANALYST", "REVIEWER", "READ_ONLY"},
        "EDIT": {"OWNER", "ADMIN", "ATTORNEY", "ANALYST"},
        "REVIEW": {"OWNER", "ADMIN", "ATTORNEY", "REVIEWER"},
        "ADMIN": {"OWNER", "ADMIN"},
    }
    return role in roles.get(required, set())


def _grant_allows(level: str, required: str) -> bool:
    levels = {
        "READ": {"READ", "EDIT", "REVIEW", "ADMIN"},
        "EDIT": {"EDIT", "ADMIN"},
        "REVIEW": {"REVIEW", "ADMIN"},
        "ADMIN": {"ADMIN"},
    }
    return level in levels.get(required, set())


def matter_access_allowed(*, tenant: Tenant, user, matter_type: str, matter_id, required: str) -> bool:
    # Worker tasks and AI context resolvers can hold a user instance after the
    # account is disabled. Do not rely only on request authentication middleware.
    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return False
    membership = TenantMembership.objects.filter(tenant=tenant, user=user, status=TenantMembership.Status.ACTIVE, tenant__status=Tenant.Status.ACTIVE).first()
    if membership is None:
        return False
    policy = MatterPolicy.objects.filter(tenant=tenant, matter_type=matter_type, matter_id=matter_id).first()
    if required == "ADMIN" and membership.role in {TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN}:
        return True
    if not policy or not policy.enforce_acl:
        return _role_default_allows(membership.role, required)
    grants = MatterGrant.objects.filter(tenant=tenant, matter_type=matter_type, matter_id=matter_id).filter(Q(subject_user=user) | Q(subject_role=membership.role))
    return grants.filter(level__in=[level for level in MatterGrant.Level.values if _grant_allows(level, required)]).exists()


def filter_visible_matter_queryset(queryset, *, tenant: Tenant, user, matter_type: str, id_field: str = "pk"):
    """Hide ACL-protected matters from collection endpoints unless READ is granted.

    Objects without an explicit enforced policy retain existing tenant-role access.
    The caller must already constrain ``queryset`` to the active tenant.
    """
    if not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return queryset.none()
    membership = TenantMembership.objects.filter(
        tenant=tenant, user=user, status=TenantMembership.Status.ACTIVE,
        tenant__status=Tenant.Status.ACTIVE,
    ).first()
    if membership is None:
        return queryset.none()
    # Keep authorization filtering in SQL. Materializing every protected matter
    # id in Python creates avoidable memory spikes for enterprise tenants.
    protected_ids = MatterPolicy.objects.filter(
        tenant=tenant, matter_type=matter_type, enforce_acl=True,
    ).values("matter_id")
    readable_levels = [level for level in MatterGrant.Level.values if _grant_allows(level, "READ")]
    allowed_ids = MatterGrant.objects.filter(
        tenant=tenant, matter_type=matter_type,
        level__in=readable_levels,
    ).filter(Q(subject_user=user) | Q(subject_role=membership.role)).values("matter_id")
    return queryset.filter(
        ~Q(**{f"{id_field}__in": Subquery(protected_ids)})
        | Q(**{f"{id_field}__in": Subquery(allowed_ids)})
    )


def require_matter_access(*, request, matter_type: str, matter_id, required: str = "READ") -> None:
    tenant = getattr(request, "tenant", None)
    if not isinstance(tenant, Tenant) or not matter_access_allowed(tenant=tenant, user=request.user, matter_type=matter_type, matter_id=matter_id, required=required):
        from rest_framework.exceptions import PermissionDenied
        raise PermissionDenied("You do not have the required permission for this matter.")


def membership_for_scim(*, token: SCIMToken, user, active: bool, role: str | None = None) -> None:
    """Update one tenant membership and safely track SCIM-caused global disablement.

    A globally disabled account is reactivated only when this SCIM binding was
    the reason it was disabled. All membership/user/session changes and their
    security audit record are part of one database transaction.
    """
    if role is not None and role not in TenantMembership.Role.values:
        raise ValidationError({"role": "Unsupported tenant role."})
    if role == TenantMembership.Role.OWNER:
        raise ValidationError({"role": "Tenant ownership must be granted locally, not through SCIM provisioning."})
    if not isinstance(active, bool):
        raise ValidationError({"active": "active must be a boolean."})
    with transaction.atomic():
        locked_user = User.objects.select_for_update().get(pk=user.pk)
        membership, created = TenantMembership.objects.select_for_update().get_or_create(
            tenant=token.tenant,
            user=locked_user,
            defaults={
                "role": role or TenantMembership.Role.READ_ONLY,
                "status": TenantMembership.Status.ACTIVE if active else TenantMembership.Status.REVOKED,
            },
        )
        if not created:
            if role in TenantMembership.Role.values:
                membership.role = role
            membership.status = TenantMembership.Status.ACTIVE if active else TenantMembership.Status.REVOKED
            membership.save(update_fields=("role", "status"))

        binding = (
            SCIMUserBinding.objects.select_for_update()
            .filter(tenant=token.tenant, user=locked_user)
            .first()
        )
        global_state_changed = False
        if not active:
            has_other_active = TenantMembership.objects.filter(
                user=locked_user,
                status=TenantMembership.Status.ACTIVE,
                tenant__status=Tenant.Status.ACTIVE,
            ).exclude(tenant=token.tenant).exists()
            if not has_other_active and locked_user.is_active:
                locked_user.is_active = False
                locked_user.save(update_fields=("is_active",))
                global_state_changed = True
                if binding is not None:
                    binding.globally_disabled_by_scim = True
                    binding.save(update_fields=("globally_disabled_by_scim", "updated_at"))
                revoke_all_django_sessions_for_user(locked_user, actor=token.created_by)
        elif binding is not None and binding.globally_disabled_by_scim:
            # The SCIM integration itself disabled this account. Restore it only
            # while the marker is present, then clear the marker transactionally.
            if not locked_user.is_active:
                locked_user.is_active = True
                locked_user.save(update_fields=("is_active",))
                global_state_changed = True
            binding.globally_disabled_by_scim = False
            binding.save(update_fields=("globally_disabled_by_scim", "updated_at"))

        user.is_active = locked_user.is_active
        audit_identity(
            tenant=token.tenant,
            actor=token.created_by,
            action="SCIM_MEMBERSHIP_ACTIVATED" if active else "SCIM_MEMBERSHIP_REVOKED",
            resource_type="TenantMembership",
            resource_id=membership.pk,
            metadata={
                "target_user_id": str(locked_user.pk),
                "membership_created": created,
                "active": active,
                "global_state_changed": global_state_changed,
                "scim_external_id": binding.external_id if binding else None,
            },
        )
