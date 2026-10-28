"""Enterprise administration, federation callbacks, SCIM 2.0 and matter ACLs."""
from django.urls import path

from enterprise_identity.views import (
    AdministrativeAuditExportView,
    IdentityProviderDetailView,
    IdentityProviderListCreateView,
    ManagedSessionListView,
    ManagedSessionRevokeView,
    MatterGrantDeleteView,
    MatterGrantListCreateView,
    MatterPolicyDetailView,
    MatterPolicyListCreateView,
    OIDCCallbackView,
    OIDCLoginStartView,
    SAMLACSView,
    SAMLLoginStartView,
    SAMLMetadataView,
    SCIMTokenListCreateView,
    SCIMTokenRevokeView,
    SCIMServiceProviderConfigView,
    SCIMResourceTypesView,
    SCIMSchemasView,
    SCIMUserDetailView,
    SCIMUsersView,
    UserSessionsRevokeAllView,
)

app_name = "enterprise_identity"

urlpatterns = [
    path("providers/", IdentityProviderListCreateView.as_view(), name="provider-list-create"),
    path("providers/<uuid:provider_id>/", IdentityProviderDetailView.as_view(), name="provider-detail"),
    path("oidc/<slug:tenant_slug>/<slug:provider_slug>/login/", OIDCLoginStartView.as_view(), name="oidc-login-start"),
    path("oidc/callback/", OIDCCallbackView.as_view(), name="oidc-callback"),
    path("saml/<slug:tenant_slug>/<slug:provider_slug>/login/", SAMLLoginStartView.as_view(), name="saml-login-start"),
    path("saml/<slug:tenant_slug>/<slug:provider_slug>/acs/", SAMLACSView.as_view(), name="saml-acs"),
    path("saml/<slug:tenant_slug>/<slug:provider_slug>/metadata/", SAMLMetadataView.as_view(), name="saml-metadata"),
    path("scim-tokens/", SCIMTokenListCreateView.as_view(), name="scim-token-list-create"),
    path("scim-tokens/<uuid:token_id>/revoke/", SCIMTokenRevokeView.as_view(), name="scim-token-revoke"),
    path("matter-policies/", MatterPolicyListCreateView.as_view(), name="matter-policy-list-create"),
    path("matter-policies/<uuid:policy_id>/", MatterPolicyDetailView.as_view(), name="matter-policy-detail"),
    path("matter-grants/", MatterGrantListCreateView.as_view(), name="matter-grant-list-create"),
    path("matter-grants/<uuid:grant_id>/", MatterGrantDeleteView.as_view(), name="matter-grant-delete"),
    path("audit-export/", AdministrativeAuditExportView.as_view(), name="audit-export"),
    path("sessions/", ManagedSessionListView.as_view(), name="managed-session-list"),
    path("sessions/<uuid:session_id>/revoke/", ManagedSessionRevokeView.as_view(), name="managed-session-revoke"),
    path("users/<str:user_id>/sessions/revoke-all/", UserSessionsRevokeAllView.as_view(), name="user-sessions-revoke-all"),
    path("scim/v2/<slug:tenant_slug>/ServiceProviderConfig/", SCIMServiceProviderConfigView.as_view(), name="scim-service-provider-config"),
    path("scim/v2/<slug:tenant_slug>/ResourceTypes/", SCIMResourceTypesView.as_view(), name="scim-resource-types"),
    path("scim/v2/<slug:tenant_slug>/Schemas/", SCIMSchemasView.as_view(), name="scim-schemas"),
    path("scim/v2/<slug:tenant_slug>/Users/", SCIMUsersView.as_view(), name="scim-users"),
    path("scim/v2/<slug:tenant_slug>/Users/<str:user_id>/", SCIMUserDetailView.as_view(), name="scim-user-detail"),
]
