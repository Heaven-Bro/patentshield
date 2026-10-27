"""Regression tests for native audit actor identity and best-effort writes."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import DatabaseError
from django.http import HttpResponse
from django.test import RequestFactory, TestCase

from audit.middleware import AuditLoggingMiddleware
from audit.models import AuditLog
from tenants.models import Tenant


class AuditActorForeignKeyTests(TestCase):
    """Ensure audit events retain the configured user model's native PK."""

    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="audit-test-user",
            password="test-only-password",
        )
        self.tenant = Tenant.objects.create(
            name="Audit Test Tenant",
            slug="audit-test-tenant",
        )
        self.factory = RequestFactory()

    def test_actor_user_id_accepts_native_user_primary_key(self) -> None:
        event = AuditLog.objects.create(
            tenant=self.tenant,
            actor_user_id=self.user.pk,
            action="REVIEW_CLAIM_ASSESSMENT",
            resource_type="ClaimAssessment",
            outcome=AuditLog.Outcome.SUCCESS,
        )

        event.refresh_from_db()
        self.assertEqual(event.actor_user, self.user)
        self.assertEqual(event.actor_user_id, self.user.pk)
        self.assertIsNone(event.legacy_actor_user_uuid)

    def test_middleware_persists_authenticated_actor_and_request_id(self) -> None:
        request = self.factory.post("/api/v1/patents/upload/")
        request.user = self.user
        request.tenant = self.tenant

        middleware = AuditLoggingMiddleware(
            lambda _request: HttpResponse("accepted", status=202)
        )
        response = middleware(request)

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.headers.get("X-Request-ID"))
        event = AuditLog.objects.get(request_id=response.headers["X-Request-ID"])
        self.assertEqual(event.actor_user, self.user)
        self.assertEqual(event.tenant, self.tenant)
        self.assertEqual(event.action, "POST /api/v1/patents/upload/")
        self.assertEqual(event.outcome, AuditLog.Outcome.SUCCESS)

    def test_audit_database_error_does_not_replace_business_response(self) -> None:
        request = self.factory.post("/api/v1/patents/upload/")
        request.user = self.user
        request.tenant = self.tenant

        with patch.object(
            AuditLog.objects,
            "create",
            side_effect=DatabaseError("simulated audit database outage"),
        ):
            response = AuditLoggingMiddleware(
                lambda _request: HttpResponse("accepted", status=202)
            )(request)

        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.headers.get("X-Request-ID"))

    def test_unauthenticated_request_has_no_actor_foreign_key(self) -> None:
        from django.contrib.auth.models import AnonymousUser

        request = self.factory.post("/api/v1/patents/upload/")
        request.user = AnonymousUser()
        request.tenant = self.tenant

        response = AuditLoggingMiddleware(
            lambda _request: HttpResponse("denied", status=403)
        )(request)

        event = AuditLog.objects.get(request_id=response.headers["X-Request-ID"])
        self.assertIsNone(event.actor_user)
        self.assertEqual(event.outcome, AuditLog.Outcome.DENIED)
