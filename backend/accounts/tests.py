"""Regression tests for the browser session and tenant discovery APIs."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from tenants.models import Tenant, TenantMembership


class SessionAuthenticationApiTests(TestCase):
    """Verify CSRF, login/logout, and active tenant discovery behavior."""

    def setUp(self) -> None:
        self.client = Client(enforce_csrf_checks=True)
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="analyst@example.test",
            email="analyst@example.test",
            password="Correct-Horse-Battery-9!",
            first_name="Avery",
            last_name="Analyst",
        )
        self.tenant = Tenant.objects.create(
            name="Northstar IP",
            slug="northstar-ip",
            status=Tenant.Status.ACTIVE,
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=self.user,
            role=TenantMembership.Role.ANALYST,
            status=TenantMembership.Status.ACTIVE,
        )
        self.suspended_tenant = Tenant.objects.create(
            name="Suspended Workspace",
            slug="suspended-workspace",
            status=Tenant.Status.SUSPENDED,
        )
        TenantMembership.objects.create(
            tenant=self.suspended_tenant,
            user=self.user,
            role=TenantMembership.Role.REVIEWER,
            status=TenantMembership.Status.ACTIVE,
        )
        self.revoked_tenant = Tenant.objects.create(
            name="Revoked Workspace",
            slug="revoked-workspace",
            status=Tenant.Status.ACTIVE,
        )
        TenantMembership.objects.create(
            tenant=self.revoked_tenant,
            user=self.user,
            role=TenantMembership.Role.READ_ONLY,
            status=TenantMembership.Status.REVOKED,
        )

    def _get_csrf_token(self) -> str:
        response = self.client.get(reverse("accounts:csrf-token"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("csrftoken", self.client.cookies)
        return response.json()["csrfToken"]

    def _login(self) -> dict[str, str]:
        csrf_token = self._get_csrf_token()
        response = self.client.post(
            reverse("accounts:login"),
            data={
                "username": self.user.get_username(),
                "password": "Correct-Horse-Battery-9!",
            },
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_csrf_endpoint_sets_cookie_and_returns_token(self) -> None:
        response = self.client.get(reverse("accounts:csrf-token"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["csrfToken"])
        self.assertIn("csrftoken", self.client.cookies)
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_login_rejects_missing_csrf_token(self) -> None:
        response = self.client.post(
            reverse("accounts:login"),
            data={
                "username": self.user.get_username(),
                "password": "Correct-Horse-Battery-9!",
            },
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_login_session_and_tenant_list_are_scoped_to_active_memberships(self) -> None:
        login_response = self._login()
        self.assertEqual(login_response["user"]["username"], self.user.get_username())
        self.assertTrue(login_response["csrfToken"])

        session_response = self.client.get(reverse("accounts:me"))
        self.assertEqual(session_response.status_code, 200)
        self.assertTrue(session_response.json()["authenticated"])
        self.assertEqual(session_response.json()["user"]["id"], str(self.user.pk))

        tenants_response = self.client.get(reverse("tenants:tenant-list"))
        self.assertEqual(tenants_response.status_code, 200)
        results = tenants_response.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], str(self.tenant.pk))
        self.assertEqual(results[0]["role"], TenantMembership.Role.ANALYST)

    def test_logout_requires_csrf_and_invalidates_session(self) -> None:
        login_payload = self._login()
        blocked_response = self.client.post(
            reverse("accounts:logout"),
            data={},
            content_type="application/json",
        )
        self.assertEqual(blocked_response.status_code, 403)

        response = self.client.post(
            reverse("accounts:logout"),
            data={},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=login_payload["csrfToken"],
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertFalse(response.json()["authenticated"])
        self.assertTrue(response.json()["csrfToken"])

        session_response = self.client.get(reverse("accounts:me"))
        self.assertEqual(session_response.status_code, 401)

    def test_authenticated_session_endpoint_returns_401_after_logout(self) -> None:
        response = self.client.get(reverse("accounts:me"))
        self.assertEqual(response.status_code, 401)

    def test_login_is_throttled_after_ten_attempts_per_minute(self) -> None:
        cache.clear()
        csrf_token = self._get_csrf_token()
        for _ in range(10):
            response = self.client.post(
                reverse("accounts:login"),
                data={"username": self.user.get_username(), "password": "incorrect"},
                content_type="application/json",
                HTTP_X_CSRFTOKEN=csrf_token,
                REMOTE_ADDR="198.51.100.45",
            )
            self.assertEqual(response.status_code, 401, response.content)
        limited = self.client.post(
            reverse("accounts:login"),
            data={"username": self.user.get_username(), "password": "incorrect"},
            content_type="application/json",
            HTTP_X_CSRFTOKEN=csrf_token,
            REMOTE_ADDR="198.51.100.45",
        )
        self.assertEqual(limited.status_code, 429, limited.content)
        cache.clear()


class LoginThrottleConfigurationTests(TestCase):
    def test_login_endpoint_declares_the_custom_rate_throttle(self):
        from accounts.throttles import LoginRateThrottle
        from accounts.views import SessionLoginView
        from django.conf import settings

        self.assertIn(LoginRateThrottle, SessionLoginView.throttle_classes)
        self.assertEqual(settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]["login"], "10/min")
