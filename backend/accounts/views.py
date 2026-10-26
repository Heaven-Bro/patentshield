"""CSRF-protected Django session authentication endpoints."""

from __future__ import annotations

from typing import Any

from django.contrib.auth import authenticate, login as auth_login
from django.contrib.auth import logout as auth_logout
from django.http import HttpRequest, JsonResponse
from django.middleware.csrf import get_token
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_protect, ensure_csrf_cookie
from django.views.decorators.http import require_GET
from rest_framework import serializers, status
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView


def _serialize_user(user: Any) -> dict[str, Any]:
    """Return the minimal browser profile needed by the authenticated shell."""
    full_name = " ".join(
        part for part in (user.first_name, user.last_name) if part
    ).strip()
    return {
        "id": str(user.pk),
        "username": user.get_username(),
        "email": user.email or "",
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "full_name": full_name or user.get_username(),
        "is_staff": bool(user.is_staff),
    }


@require_GET
@ensure_csrf_cookie
def csrf_token_view(request: HttpRequest) -> JsonResponse:
    """Set the CSRF cookie and return a masked token for the SPA request header."""
    response = JsonResponse({"csrfToken": get_token(request)})
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response


class LoginRequestSerializer(serializers.Serializer):
    """Validate credentials without trimming or logging the password."""

    username = serializers.CharField(max_length=150, trim_whitespace=True)
    password = serializers.CharField(
        max_length=1024,
        trim_whitespace=False,
        write_only=True,
    )


@method_decorator(csrf_protect, name="dispatch")
class SessionLoginView(APIView):
    """Authenticate a user and establish a Django session."""

    authentication_classes: list[type] = []
    permission_classes = [AllowAny]

    def post(self, request: Request) -> Response:
        serializer = LoginRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        username = serializer.validated_data["username"]
        password = serializer.validated_data["password"]

        user = authenticate(
            request=request._request,
            username=username,
            password=password,
        )
        if user is None or not user.is_active:
            return Response(
                {"detail": "Invalid username or password."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        # Django rotates the CSRF secret when establishing a new session.
        auth_login(request._request, user)
        request._request.session.set_expiry(None)
        return Response(
            {
                "user": _serialize_user(user),
                "csrfToken": get_token(request._request),
            },
            status=status.HTTP_200_OK,
        )


class SessionView(APIView):
    """Return the current session identity, without exposing authentication data."""

    authentication_classes = [SessionAuthentication]
    permission_classes = [AllowAny]

    def get(self, request: Request) -> Response:
        if not request.user.is_authenticated:
            return Response(
                {"detail": "Authentication required."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        return Response(
            {
                "authenticated": True,
                "user": _serialize_user(request.user),
            },
            status=status.HTTP_200_OK,
        )


@method_decorator(csrf_protect, name="dispatch")
class SessionLogoutView(APIView):
    """Invalidate the current browser session and rotate the CSRF token."""

    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def post(self, request: Request) -> Response:
        auth_logout(request._request)
        return Response(
            {
                "authenticated": False,
                "csrfToken": get_token(request._request),
            },
            status=status.HTTP_200_OK,
        )
