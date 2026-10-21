"""Request correlation and best-effort audit logging middleware."""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from typing import Any
from uuid import UUID

from django.http import HttpRequest, HttpResponseBase

from audit.models import AuditLog
from tenants.models import Tenant

logger = logging.getLogger(__name__)

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,100}\Z")
_API_VERSION = re.compile(r"v[0-9]+\Z", re.IGNORECASE)

GetResponse = Callable[[HttpRequest], HttpResponseBase]


class AuditLoggingMiddleware:
    """Attach a request ID and persist mutating request outcomes best-effort.

    Audit writes intentionally never replace or delay a response because an
    audit database write failed. Since AuditLog is tenant-scoped, events for
    requests with no resolved tenant are sent to application logs instead of
    inventing or guessing a tenant association.
    """

    def __init__(self, get_response: GetResponse) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponseBase:
        request_id = self._resolve_request_id(
            request.headers.get("X-Request-ID")
        )
        request.request_id = request_id  # type: ignore[attr-defined]

        try:
            response = self.get_response(request)
        except Exception:
            if request.method.upper() in _MUTATING_METHODS:
                self._write_audit(
                    request=request,
                    status_code=500,
                    request_id=request_id,
                )
            raise

        response.headers["X-Request-ID"] = request_id

        if request.method.upper() in _MUTATING_METHODS:
            self._write_audit(
                request=request,
                status_code=response.status_code,
                request_id=request_id,
            )

        return response

    @staticmethod
    def _resolve_request_id(candidate: str | None) -> str:
        """Keep a safe bounded incoming ID or create a new UUID request ID."""
        if candidate is not None:
            candidate = candidate.strip()
            if _VALID_REQUEST_ID.fullmatch(candidate):
                return candidate
        return str(uuid.uuid4())

    @staticmethod
    def _get_resource_type(request: HttpRequest) -> str:
        """Use the resolved view class, falling back to a path resource name."""
        resolver_match = getattr(request, "resolver_match", None)
        view_func = getattr(resolver_match, "func", None)
        view_class = getattr(view_func, "cls", None)
        if view_class is not None:
            return view_class.__name__[:100]

        segments = [
            segment
            for segment in request.path.strip("/").split("/")
            if segment
        ]
        if segments and segments[0].lower() == "api":
            segments.pop(0)
        if segments and _API_VERSION.fullmatch(segments[0]):
            segments.pop(0)

        return (segments[0] if segments else "unknown")[:100]

    @staticmethod
    def _get_actor_user_id(request: HttpRequest) -> UUID | None:
        """Return the authenticated user's UUID primary key when available.

        AuditLog.actor_user_id is a UUIDField. For a configured user model with
        a non-UUID primary key, the event is still recorded but actor_user_id
        must remain null to respect that field's database type.
        """
        user = getattr(request, "user", None)
        if user is None or not getattr(user, "is_authenticated", False):
            return None

        user_id = getattr(user, "pk", None)
        if user_id is None:
            return None
        if isinstance(user_id, UUID):
            return user_id

        try:
            return UUID(str(user_id))
        except (AttributeError, TypeError, ValueError):
            return None

    def _write_audit(
        self,
        request: HttpRequest,
        status_code: int,
        request_id: str,
    ) -> None:
        """Persist one audit event without propagating any audit-side error."""
        try:
            self._persist_audit(request, status_code, request_id)
        except Exception:
            # Audit logging must never change the result of the business request.
            logger.exception(
                "Failed to write audit event (request_id=%s, method=%s, path=%s).",
                request_id,
                request.method,
                request.path,
            )

    def _persist_audit(
        self,
        request: HttpRequest,
        status_code: int,
        request_id: str,
    ) -> None:
        """Resolve event fields and persist the tenant-scoped audit record."""
        tenant_context: Any = getattr(request, "tenant", None)
        tenant_pk = getattr(tenant_context, "pk", tenant_context)
        if tenant_pk is None:
            logger.warning(
                "Skipping tenant-scoped audit event because tenant context is "
                "missing (request_id=%s, method=%s, path=%s).",
                request_id,
                request.method,
                request.path,
            )
            return

        try:
            tenant_id = UUID(str(tenant_pk))
        except (AttributeError, TypeError, ValueError):
            logger.warning(
                "Skipping tenant-scoped audit event because tenant context is "
                "invalid (request_id=%s).",
                request_id,
            )
            return

        if status_code < 400:
            outcome = AuditLog.Outcome.SUCCESS
        elif status_code in {401, 403}:
            outcome = AuditLog.Outcome.DENIED
        else:
            outcome = AuditLog.Outcome.FAILURE

        actor_user_id = self._get_actor_user_id(request)
        action = f"{request.method.upper()} {request.path}"[:100]
        resource_type = self._get_resource_type(request)

        AuditLog.objects.create(
            tenant_id=tenant_id,
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            request_id=request_id,
            outcome=outcome,
            metadata={
                "status_code": status_code,
                "method": request.method.upper(),
                "path": request.path,
            },
        )
