"""Tenant discovery routes."""

from django.urls import path

from tenants.api_views import TenantMembershipListView

app_name = "tenants"

urlpatterns = [
    path("", TenantMembershipListView.as_view(), name="tenant-list"),
]
