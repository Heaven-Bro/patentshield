"""HTTP URL configuration for the PatentShield API."""

from __future__ import annotations

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/auth/", include("accounts.urls")),
    path("api/v1/tenants/", include("tenants.urls")),
    path("api/v1/patents/", include("patents.urls")),
    # analyses.urls declares paths prefixed with "analyses/" internally.
    path("api/v1/", include("analyses.urls")),
    path("api/v1/reports/", include("reports.urls")),
    path("api/v1/office-actions/", include("office_actions.urls")),
    path("api/v1/multilingual-search/", include("multilingual_search.urls")),
    path("api/v1/fto/", include("fto.urls")),
    path("api/v1/landscapes/", include("landscapes.urls")),
    path("api/v1/enterprise-auth/", include("enterprise_identity.urls")),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
