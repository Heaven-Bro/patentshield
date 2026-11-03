"""Security regression tests for enterprise federation and matter authorization."""
from __future__ import annotations

import hashlib
import uuid
from unittest.mock import patch

from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.http import HttpResponse
from django.contrib.sessions.backends.db import SessionStore
from django.utils import timezone
from rest_framework.exceptions import ValidationError as DRFValidationError

from enterprise_identity.models import (
    IdentityProvider,
    ManagedSession,
    MatterGrant,
    MatterPolicy,
    SCIMToken,
    SCIMUserBinding,
)
from enterprise_identity.services import (
    _https_url,
    _safe_external_endpoint,
    decrypt_client_secret,
    encrypt_client_secret,
    filter_visible_matter_queryset,
    matter_access_allowed,
    membership_for_scim,
    saml_auth,
    session_key_hash,
    EnterpriseAuthError,
)
from enterprise_identity.middleware import ManagedSessionTrackingMiddleware
from enterprise_identity.serializers import IdentityProviderSerializer
from enterprise_identity.views import _normalize_scim_scopes
from tenants.models import Tenant, TenantMembership

User = get_user_model()


class EnterpriseUrlSafetyTests(SimpleTestCase):
    def test_federation_urls_require_https_outside_local_development(self):
        self.assertTrue(_https_url("https://idp.example.com/.well-known/openid-configuration"))
        self.assertFalse(_https_url("http://idp.example.com/.well-known/openid-configuration"))
        self.assertFalse(_https_url("https://user:password@idp.example.com/metadata"))
        self.assertTrue(_https_url("https://idp.example.com:443/metadata", allow_localhost=False))

    def test_local_http_is_only_allowed_when_explicitly_enabled(self):
        self.assertTrue(_https_url("http://localhost:9000/metadata", allow_localhost=True))
        self.assertFalse(_https_url("http://localhost:9000/metadata", allow_localhost=False))
        self.assertFalse(_https_url("http://169.254.169.254/latest/meta-data", allow_localhost=True))

    @override_settings(PATENTSHIELD_IDP_ALLOWED_HOSTS="idp.example.com")
    @patch("enterprise_identity.services.socket.getaddrinfo")
    def test_oidc_endpoints_reject_private_network_addresses(self, getaddrinfo):
        getaddrinfo.return_value = [(None, None, None, None, ("10.20.30.40", 443))]
        self.assertFalse(_safe_external_endpoint("https://idp.example.com/.well-known/openid-configuration"))
        getaddrinfo.return_value = [(None, None, None, None, ("203.0.113.10", 443))]
        # TEST-NET addresses are intentionally not globally routable.
        self.assertFalse(_safe_external_endpoint("https://idp.example.com/.well-known/openid-configuration"))
        getaddrinfo.return_value = [(None, None, None, None, ("8.8.8.8", 443))]
        self.assertTrue(_safe_external_endpoint("https://idp.example.com/.well-known/openid-configuration"))

    @override_settings(PATENTSHIELD_IDP_ALLOWED_HOSTS="")
    @patch("enterprise_identity.services.socket.getaddrinfo")
    def test_oidc_endpoint_allowlist_must_not_be_empty(self, getaddrinfo):
        getaddrinfo.return_value = [(None, None, None, None, ("8.8.8.8", 443))]
        self.assertFalse(_safe_external_endpoint("https://idp.example.com/.well-known/openid-configuration"))
        getaddrinfo.assert_not_called()

    def test_saml_endpoints_require_the_same_explicit_host_allowlist(self):
        with override_settings(PATENTSHIELD_IDP_ALLOWED_HOSTS="login.example.com"):
            with patch("enterprise_identity.services.socket.getaddrinfo") as getaddrinfo:
                getaddrinfo.return_value = [(None, None, None, None, ("8.8.8.8", 443))]
                self.assertTrue(_safe_external_endpoint("https://login.example.com/saml/sso"))
                self.assertFalse(_safe_external_endpoint("https://unapproved.example.net/saml/sso"))
                getaddrinfo.assert_called_once()

    def test_saml_login_rejects_unapproved_endpoint_before_constructing_toolkit(self):
        import sys
        from types import ModuleType, SimpleNamespace
        onelogin = ModuleType("onelogin")
        saml_package = ModuleType("onelogin.saml2")
        auth_package = ModuleType("onelogin.saml2.auth")
        auth_package.OneLogin_Saml2_Auth = object
        onelogin.saml2 = saml_package
        saml_package.auth = auth_package
        provider = SimpleNamespace(
            protocol=IdentityProvider.Protocol.SAML, enabled=True,
            idp_entity_id="https://idp.example.com/entity", sso_url="https://unapproved.example.net/sso",
            idp_x509_certificate="-----BEGIN CERTIFICATE-----\nabc\n-----END CERTIFICATE-----",
        )
        with patch.dict(sys.modules, {
            "onelogin": onelogin,
            "onelogin.saml2": saml_package,
            "onelogin.saml2.auth": auth_package,
        }):
            with patch("enterprise_identity.services._safe_external_endpoint", return_value=False):
                with self.assertRaises(EnterpriseAuthError):
                    saml_auth(None, provider)

    def test_scim_scope_validation_rejects_unhashable_and_unknown_values(self):
        with self.assertRaises(DRFValidationError):
            _normalize_scim_scopes([{"scope": "Users:write"}])
        with self.assertRaises(DRFValidationError):
            _normalize_scim_scopes(["Users:read", "unexpected:admin"])
        with self.assertRaises(DRFValidationError):
            _normalize_scim_scopes(["Users:read", "*"])
        with self.assertRaises(DRFValidationError):
            _normalize_scim_scopes(["Roles:admin"])

    def test_scim_scope_validation_normalizes_duplicates_and_wildcard(self):
        self.assertEqual(_normalize_scim_scopes(["Users:read", "Users:read"]), ["Users:read"])
        self.assertEqual(_normalize_scim_scopes(["*"]), ["*"])

    def test_csv_formula_prefixes_are_neutralized(self):
        from enterprise_identity.views import _safe_csv_cell
        self.assertEqual(_safe_csv_cell("=1+1"), "'=1+1")
        self.assertEqual(_safe_csv_cell("@SUM(A1:A2)"), "'@SUM(A1:A2)")
        self.assertNotIn("\x00", _safe_csv_cell("test\x00value"))


class EnterpriseIdentitySecurityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tenant-admin", password="Test-password-321!", email="admin@example.com")
        self.member = User.objects.create_user(username="matter-analyst", password="Test-password-321!", email="analyst@example.com")
        self.tenant = Tenant.objects.create(name="Alpha IP", slug="alpha-ip")
        self.other_tenant = Tenant.objects.create(name="Beta IP", slug="beta-ip")
        TenantMembership.objects.create(tenant=self.tenant, user=self.user, role=TenantMembership.Role.ADMIN)
        TenantMembership.objects.create(tenant=self.tenant, user=self.member, role=TenantMembership.Role.ANALYST)
        self.matter_id = uuid.uuid4()

    def test_enabled_oidc_requires_configured_client_secret(self):
        serializer = IdentityProviderSerializer(data={
            "slug": "oidc-main", "name": "OIDC Main", "protocol": "OIDC", "enabled": True,
            "issuer": "https://idp.example.com", "discovery_url": "https://idp.example.com/.well-known/openid-configuration",
            "client_id": "patentshield",
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn("client_secret", serializer.errors)

    def test_federation_cannot_configure_tenant_owner_as_default_role(self):
        serializer = IdentityProviderSerializer(data={
            "slug": "oidc-owner", "name": "OIDC Owner", "protocol": "OIDC", "enabled": False,
            "default_role": TenantMembership.Role.OWNER,
        })
        self.assertFalse(serializer.is_valid())
        self.assertIn("default_role", serializer.errors)

    @override_settings(PATENTSHIELD_ENTERPRISE_ENCRYPTION_KEY="")
    def test_secret_encryption_fails_closed_without_key(self):
        from django.core.exceptions import ImproperlyConfigured
        with self.assertRaises(ImproperlyConfigured):
            encrypt_client_secret("a-client-secret")

    def test_provider_secret_is_encrypted_and_never_plaintext_in_storage(self):
        key = Fernet.generate_key().decode("ascii")
        with override_settings(PATENTSHIELD_ENTERPRISE_ENCRYPTION_KEY=key):
            ciphertext = encrypt_client_secret("do-not-store-plaintext")
            self.assertNotIn("do-not-store-plaintext", ciphertext)
            provider = IdentityProvider.objects.create(
                tenant=self.tenant, slug="oidc-main", name="OIDC Main", protocol=IdentityProvider.Protocol.OIDC,
                created_by=self.user, client_secret_ciphertext=ciphertext,
            )
            self.assertEqual(decrypt_client_secret(provider), "do-not-store-plaintext")

    def test_matter_access_preserves_role_defaults_until_acl_is_enforced(self):
        self.assertTrue(matter_access_allowed(tenant=self.tenant, user=self.member, matter_type="patent", matter_id=self.matter_id, required="READ"))
        MatterPolicy.objects.create(tenant=self.tenant, matter_type="patent", matter_id=self.matter_id, enforce_acl=True, created_by=self.user)
        self.assertFalse(matter_access_allowed(tenant=self.tenant, user=self.member, matter_type="patent", matter_id=self.matter_id, required="READ"))
        MatterGrant.objects.create(tenant=self.tenant, matter_type="patent", matter_id=self.matter_id, subject_user=self.member, level=MatterGrant.Level.READ, created_by=self.user)
        self.assertTrue(matter_access_allowed(tenant=self.tenant, user=self.member, matter_type="patent", matter_id=self.matter_id, required="READ"))
        self.assertFalse(matter_access_allowed(tenant=self.tenant, user=self.member, matter_type="patent", matter_id=self.matter_id, required="EDIT"))

    def test_cross_tenant_membership_is_required_even_with_grant(self):
        MatterPolicy.objects.create(tenant=self.other_tenant, matter_type="patent", matter_id=self.matter_id, enforce_acl=True, created_by=self.user)
        MatterGrant.objects.create(tenant=self.other_tenant, matter_type="patent", matter_id=self.matter_id, subject_user=self.member, level=MatterGrant.Level.ADMIN, created_by=self.user)
        self.assertFalse(matter_access_allowed(tenant=self.other_tenant, user=self.member, matter_type="patent", matter_id=self.matter_id, required="READ"))

    def test_inactive_global_account_is_denied_even_when_tenant_membership_remains_active(self):
        self.member.is_active = False
        self.member.save(update_fields=("is_active",))
        self.assertFalse(matter_access_allowed(tenant=self.tenant, user=self.member, matter_type="patent", matter_id=self.matter_id, required="READ"))
        qs = filter_visible_matter_queryset(
            IdentityProvider.objects.for_tenant(self.tenant.pk), tenant=self.tenant,
            user=self.member, matter_type="provider_test",
        )
        self.assertFalse(qs.exists())

    def test_collection_filter_hides_acl_protected_objects_without_read_grant(self):
        provider = IdentityProvider.objects.create(
            tenant=self.tenant, slug="public-test-provider", name="Test", protocol=IdentityProvider.Protocol.OIDC,
            created_by=self.user,
        )
        MatterPolicy.objects.create(tenant=self.tenant, matter_type="provider_test", matter_id=provider.pk, enforce_acl=True, created_by=self.user)
        visible = filter_visible_matter_queryset(
            IdentityProvider.objects.for_tenant(self.tenant.pk), tenant=self.tenant, user=self.member,
            matter_type="provider_test",
        )
        self.assertFalse(visible.filter(pk=provider.pk).exists())
        MatterGrant.objects.create(tenant=self.tenant, matter_type="provider_test", matter_id=provider.pk, subject_role=TenantMembership.Role.ANALYST, level=MatterGrant.Level.READ, created_by=self.user)
        visible = filter_visible_matter_queryset(
            IdentityProvider.objects.for_tenant(self.tenant.pk), tenant=self.tenant, user=self.member,
            matter_type="provider_test",
        )
        self.assertTrue(visible.filter(pk=provider.pk).exists())

    def test_scim_deprovision_only_revokes_target_tenant_membership(self):
        TenantMembership.objects.create(tenant=self.other_tenant, user=self.member, role=TenantMembership.Role.ANALYST)
        token = SCIMToken.objects.create(
            tenant=self.tenant, label="directory", token_hash=hashlib.sha256(b"private-token").hexdigest(),
            scopes=["Users:write"], created_by=self.user,
        )
        membership_for_scim(token=token, user=self.member, active=False)
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_active)
        self.assertEqual(TenantMembership.objects.get(tenant=self.tenant, user=self.member).status, TenantMembership.Status.REVOKED)
        self.assertEqual(TenantMembership.objects.get(tenant=self.other_tenant, user=self.member).status, TenantMembership.Status.ACTIVE)

    def test_scim_deprovision_disables_account_when_no_other_tenant_remains(self):
        token = SCIMToken.objects.create(
            tenant=self.tenant, label="directory", token_hash=hashlib.sha256(b"private-token-2").hexdigest(),
            scopes=["Users:write"], created_by=self.user,
        )
        membership_for_scim(token=token, user=self.member, active=False)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)

    def test_scim_membership_activation_does_not_override_global_account_disable(self):
        self.member.is_active = False
        self.member.save(update_fields=("is_active",))
        token = SCIMToken.objects.create(
            tenant=self.tenant, label="directory", token_hash=hashlib.sha256(b"private-token-3").hexdigest(),
            scopes=["Users:write"], created_by=self.user,
        )
        membership_for_scim(token=token, user=self.member, active=True)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)
        self.assertEqual(TenantMembership.objects.get(tenant=self.tenant, user=self.member).status, TenantMembership.Status.ACTIVE)

    def test_scim_cannot_assign_tenant_owner_even_with_elevated_role_scope(self):
        token = SCIMToken.objects.create(
            tenant=self.tenant, label="directory", token_hash=hashlib.sha256(b"private-token-owner").hexdigest(),
            scopes=["*"], created_by=self.user,
        )
        with self.assertRaises(DjangoValidationError):
            membership_for_scim(token=token, user=self.member, active=True, role=TenantMembership.Role.OWNER)

    def test_scim_reactivation_restores_only_an_account_disabled_by_scim(self):
        token = SCIMToken.objects.create(
            tenant=self.tenant, label="directory", token_hash=hashlib.sha256(b"private-token-scim-toggle").hexdigest(),
            scopes=["Users:write"], created_by=self.user,
        )
        binding = SCIMUserBinding.objects.create(tenant=self.tenant, external_id="analyst-01", user=self.member)
        membership_for_scim(token=token, user=self.member, active=False)
        self.member.refresh_from_db()
        binding.refresh_from_db()
        self.assertFalse(self.member.is_active)
        self.assertTrue(binding.globally_disabled_by_scim)

        membership_for_scim(token=token, user=self.member, active=True)
        self.member.refresh_from_db()
        binding.refresh_from_db()
        self.assertTrue(self.member.is_active)
        self.assertFalse(binding.globally_disabled_by_scim)

    def test_session_registry_stores_only_hmac_digest(self):
        raw_key = "sample-django-session-key-not-to-persist"
        digest = session_key_hash(raw_key)
        self.assertEqual(len(digest), 64)
        self.assertNotEqual(digest, raw_key)
        self.assertNotIn(raw_key, digest)
        ManagedSession.objects.create(user=self.user, session_key_hash=digest, auth_source="PASSWORD")
        stored = ManagedSession.objects.get(session_key_hash=digest)
        self.assertFalse(hasattr(stored, "session_key"))


class ManagedSessionMiddlewareTests(TestCase):
    def test_revoked_session_is_logged_out_before_view_dispatch(self):
        user = User.objects.create_user(username="session-user", password="Session-password-42!")
        session = SessionStore()
        session["_auth_user_id"] = str(user.pk)
        session["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
        session["_auth_user_hash"] = user.get_session_auth_hash()
        session.save()
        digest = session_key_hash(session.session_key)
        ManagedSession.objects.create(user=user, session_key_hash=digest, revoked_at=timezone.now())

        request = RequestFactory().get("/protected/")
        request.session = session
        request.user = user
        middleware = ManagedSessionTrackingMiddleware(
            lambda req: HttpResponse(b"authenticated" if req.user.is_authenticated else b"anonymous")
        )
        response = middleware(request)
        self.assertEqual(response.content, b"anonymous")
        self.assertIsNone(request.session.session_key)