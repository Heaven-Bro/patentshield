"""Enforce revocation and maintain a low-frequency registry of Django sessions."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from django.contrib.auth import logout
from django.http import HttpRequest, HttpResponseBase
from django.utils import timezone

from enterprise_identity.models import ManagedSession
from enterprise_identity.services import session_key_hash


class ManagedSessionTrackingMiddleware:
    """Track sessions with keyed hashes and reject sessions revoked by admins.

    Existing sessions created before Phase 8 are registered on their first
    authenticated request. A known revoked digest is never re-registered.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponseBase]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        user = getattr(request, "user", None)
        session = getattr(request, "session", None)
        digest = None
        known_record = None
        if user is not None and getattr(user, "is_authenticated", False) and session is not None:
            key = getattr(session, "session_key", None)
            if key:
                digest = session_key_hash(key)
                try:
                    known_record = ManagedSession.objects.filter(session_key_hash=digest).first()
                    if known_record is not None and (known_record.revoked_at is not None or known_record.user_id != user.pk):
                        # Do this before view dispatch so a revoked cookie cannot
                        # access one more protected resource after revocation.
                        logout(request)
                        digest = None
                        known_record = None
                except Exception:
                    # Registry storage is required for revocation enforcement.
                    # Fail closed for the request when it cannot be consulted.
                    import logging
                    logging.getLogger(__name__).exception("Managed-session revocation check failed.")
                    logout(request)
                    digest = None
                    known_record = None
        response = self.get_response(request)
        user = getattr(request, "user", None)
        session = getattr(request, "session", None)
        if user is None or not getattr(user, "is_authenticated", False) or not user.is_active or session is None:
            return response
        key = getattr(session, "session_key", None)
        if not key:
            return response
        now = timezone.now()
        marker = session.get("_ps_managed_session_touch")
        try:
            marker_time = datetime.fromisoformat(marker) if marker else None
        except (TypeError, ValueError):
            marker_time = None
        if marker_time and timezone.is_aware(marker_time) and now - marker_time < timedelta(minutes=5):
            return response
        digest = session_key_hash(key)
        try:
            record = ManagedSession.objects.filter(session_key_hash=digest, revoked_at__isnull=True).first()
            if record is None:
                # A registry row must never transfer ownership of an existing
                # session hash to another user. If a revoked hash appears again,
                # do not recreate the registry entry from this request.
                existing = ManagedSession.objects.filter(session_key_hash=digest).first()
                if existing is None:
                    ManagedSession.objects.create(
                        user=user,
                        session_key_hash=digest,
                        auth_source="PASSWORD_OR_EXISTING",
                        user_agent=request.META.get("HTTP_USER_AGENT", "")[:300],
                        last_seen_at=now,
                    )
            elif record.user_id == user.pk:
                ManagedSession.objects.filter(pk=record.pk, revoked_at__isnull=True).update(last_seen_at=now)
            session["_ps_managed_session_touch"] = now.isoformat()
        except Exception:
            # Avoid masking application errors, but keep this failure observable.
            import logging
            logging.getLogger(__name__).exception("Managed-session tracking failed.")
        return response
