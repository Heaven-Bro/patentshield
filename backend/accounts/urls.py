"""URL routes for browser session authentication."""

from django.urls import path

from accounts.views import (
    SessionLoginView,
    SessionLogoutView,
    SessionView,
    csrf_token_view,
)

app_name = "accounts"

urlpatterns = [
    path("csrf/", csrf_token_view, name="csrf-token"),
    path("login/", SessionLoginView.as_view(), name="login"),
    path("me/", SessionView.as_view(), name="me"),
    path("logout/", SessionLogoutView.as_view(), name="logout"),
]
