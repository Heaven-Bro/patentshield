"""Safe serializers for enterprise identity administration and matter ACLs."""
from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from rest_framework import serializers

from enterprise_identity.models import IdentityProvider, ManagedSession, MatterGrant, MatterPolicy, SCIMToken
from enterprise_identity.services import _https_url, encrypt_client_secret
from tenants.models import TenantMembership


class IdentityProviderSerializer(serializers.ModelSerializer):
    client_secret = serializers.CharField(write_only=True, required=False, allow_blank=True, trim_whitespace=False)
    has_client_secret = serializers.SerializerMethodField()

    class Meta:
        model = IdentityProvider
        fields = (
            "id", "slug", "name", "protocol", "enabled", "jit_provisioning",
            "allow_existing_email_link", "trust_email_from_idp", "default_role",
            "allowed_email_domains", "group_claim", "group_role_mapping", "issuer",
            "discovery_url", "client_id", "client_secret", "has_client_secret",
            "client_auth_method", "scopes", "sp_entity_id", "idp_entity_id", "sso_url",
            "idp_x509_certificate", "saml_email_attribute", "saml_groups_attribute",
            "created_at", "updated_at",
        )
        read_only_fields = ("id", "has_client_secret", "created_at", "updated_at")

    def get_has_client_secret(self, obj):
        return bool(obj.client_secret_ciphertext)

    def validate_allowed_email_domains(self, value):
        if not isinstance(value, list) or len(value) > 100:
            raise serializers.ValidationError("Use a list of at most 100 email domains.")
        result = []
        for domain in value:
            domain = str(domain).strip().casefold().lstrip("@")
            if not domain or "/" in domain or "@" in domain or len(domain) > 253:
                raise serializers.ValidationError("Each allowed email domain must be a valid domain name.")
            result.append(domain)
        return sorted(set(result))

    def validate_group_role_mapping(self, value):
        if not isinstance(value, dict) or len(value) > 200:
            raise serializers.ValidationError("Group role mapping must be an object with at most 200 entries.")
        allowed = set(TenantMembership.Role.values)
        if any(role not in allowed for role in value.values()):
            raise serializers.ValidationError("Group role mappings must use recognized tenant roles.")
        if TenantMembership.Role.OWNER in value.values():
            raise serializers.ValidationError("Tenant ownership must be granted locally, not through identity-provider group claims.")
        return value

    def validate(self, attrs):
        protocol = attrs.get("protocol", getattr(self.instance, "protocol", None))
        enabled = attrs.get("enabled", getattr(self.instance, "enabled", False))
        if enabled and protocol == IdentityProvider.Protocol.OIDC:
            issuer = attrs.get("issuer", getattr(self.instance, "issuer", ""))
            discovery = attrs.get("discovery_url", getattr(self.instance, "discovery_url", ""))
            client_id = attrs.get("client_id", getattr(self.instance, "client_id", ""))
            if not issuer or not discovery or not client_id:
                raise serializers.ValidationError("Enabled OIDC providers require issuer, discovery URL, and client ID.")
            if not _https_url(str(issuer), allow_localhost=bool(settings.DEBUG)) or not _https_url(str(discovery), allow_localhost=bool(settings.DEBUG)):
                raise serializers.ValidationError("Enabled OIDC issuer and discovery URLs must use HTTPS (localhost HTTP is allowed only in DEBUG).")
            client_secret = attrs.get("client_secret", None)
            has_stored_secret = bool(getattr(self.instance, "client_secret_ciphertext", ""))
            if "client_secret" in attrs and not str(client_secret or "").strip():
                raise serializers.ValidationError({"client_secret": "An enabled OIDC client secret cannot be cleared. Disable the provider first if secret rotation is required."})
            if not (client_secret and str(client_secret).strip()) and not has_stored_secret:
                raise serializers.ValidationError({"client_secret": "A client secret is required for the configured client authentication methods before enabling OIDC."})
            if client_secret:
                try:
                    encrypt_client_secret(str(client_secret))
                except ImproperlyConfigured as exc:
                    raise serializers.ValidationError({"client_secret": str(exc)}) from exc
        if enabled and protocol == IdentityProvider.Protocol.SAML:
            required = {
                "idp_entity_id": attrs.get("idp_entity_id", getattr(self.instance, "idp_entity_id", "")),
                "sso_url": attrs.get("sso_url", getattr(self.instance, "sso_url", "")),
                "idp_x509_certificate": attrs.get("idp_x509_certificate", getattr(self.instance, "idp_x509_certificate", "")),
            }
            if any(not v for v in required.values()):
                raise serializers.ValidationError("Enabled SAML providers require IdP entity ID, SSO URL, and IdP X.509 certificate.")
            if not _https_url(str(required["sso_url"]), allow_localhost=bool(settings.DEBUG)):
                raise serializers.ValidationError({"sso_url": "Enabled SAML providers must use an HTTPS SSO URL (localhost HTTP is allowed only in DEBUG)."})
        role = attrs.get("default_role", getattr(self.instance, "default_role", TenantMembership.Role.READ_ONLY))
        if role not in TenantMembership.Role.values:
            raise serializers.ValidationError({"default_role": "Unsupported tenant role."})
        if role == TenantMembership.Role.OWNER:
            raise serializers.ValidationError({"default_role": "Tenant ownership must be granted locally, not by federation provisioning."})
        return attrs

    def create(self, validated_data):
        secret = validated_data.pop("client_secret", "")
        provider = IdentityProvider.objects.create(**validated_data)
        if secret:
            provider.client_secret_ciphertext = encrypt_client_secret(secret)
            provider.save(update_fields=("client_secret_ciphertext", "updated_at"))
        return provider

    def update(self, instance, validated_data):
        secret = validated_data.pop("client_secret", None)
        for key, value in validated_data.items():
            setattr(instance, key, value)
        if secret is not None:
            instance.client_secret_ciphertext = encrypt_client_secret(secret) if secret else ""
        instance.save()
        return instance


class MatterPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = MatterPolicy
        fields = ("id", "matter_type", "matter_id", "enforce_acl", "created_at", "updated_at")
        read_only_fields = ("id", "created_at", "updated_at")


class MatterGrantSerializer(serializers.ModelSerializer):
    subject_username = serializers.CharField(source="subject_user.username", read_only=True)

    class Meta:
        model = MatterGrant
        fields = ("id", "matter_type", "matter_id", "subject_user", "subject_username", "subject_role", "level", "created_at")
        read_only_fields = ("id", "subject_username", "created_at")

    def validate(self, attrs):
        subject_user = attrs.get("subject_user", getattr(self.instance, "subject_user", None))
        subject_role = attrs.get("subject_role", getattr(self.instance, "subject_role", None))
        if bool(subject_user) == bool(subject_role):
            raise serializers.ValidationError("Specify exactly one of subject_user or subject_role.")
        return attrs


class SCIMTokenSerializer(serializers.ModelSerializer):
    class Meta:
        model = SCIMToken
        fields = ("id", "label", "scopes", "expires_at", "last_used_at", "revoked_at", "created_at")
        read_only_fields = fields


class ManagedSessionSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.get_username", read_only=True)
    email = serializers.EmailField(source="user.email", read_only=True)
    is_current = serializers.SerializerMethodField()

    class Meta:
        model = ManagedSession
        fields = ("id", "user_id", "username", "email", "auth_source", "user_agent", "created_at", "last_seen_at", "revoked_at", "is_current")

    def get_is_current(self, obj):
        current_id = self.context.get("current_session_hash")
        return bool(current_id and obj.session_key_hash == current_id)
