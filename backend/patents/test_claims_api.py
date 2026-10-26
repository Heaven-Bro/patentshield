"""Tests for the tenant-scoped claims endpoint consumed by analysis UI."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from patents.models import Claim, Patent, PatentDocumentVersion
from tenants.models import Tenant, TenantMembership


class PatentClaimsAPITests(TestCase):
    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="claims-endpoint-user",
            password="not-used-in-force-auth",
        )
        self.tenant = Tenant.objects.create(
            name="Claims Endpoint Tenant",
            slug="claims-endpoint-tenant",
            status=Tenant.Status.ACTIVE,
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=self.user,
            role=TenantMembership.Role.ANALYST,
            status=TenantMembership.Status.ACTIVE,
        )
        self.patent = Patent.objects.create(
            tenant=self.tenant,
            title="Claims API patent",
            status=Patent.Status.INDEXED,
            created_by=self.user,
        )
        self.version = PatentDocumentVersion.objects.create(
            tenant=self.tenant,
            patent=self.patent,
            version_number=1,
            storage_key="private/claims-api.pdf",
            content_sha256="b" * 64,
            extraction_status=PatentDocumentVersion.ExtractionStatus.EXTRACTED,
        )
        self.claim_two = Claim.objects.create(
            tenant=self.tenant,
            patent=self.patent,
            document_version=self.version,
            claim_number="2",
            claim_text="The device of claim 1, wherein the sensor is wireless.",
            claim_type="DEPENDENT",
            depends_on_claim_number="1",
        )
        self.claim_one = Claim.objects.create(
            tenant=self.tenant,
            patent=self.patent,
            document_version=self.version,
            claim_number="1",
            claim_text="A device comprising a sensor.",
            claim_type="INDEPENDENT",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_claims_are_returned_in_numeric_order(self) -> None:
        response = self.client.get(
            f"/api/v1/patents/{self.patent.pk}/claims/",
            {"document_version_id": str(self.version.pk)},
            HTTP_X_TENANT_ID=str(self.tenant.pk),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["claim_number"] for item in response.data["results"]],
            ["1", "2"],
        )
        self.assertEqual(response.data["results"][1]["id"], str(self.claim_two.pk))

    def test_missing_document_version_is_rejected(self) -> None:
        response = self.client.get(
            f"/api/v1/patents/{self.patent.pk}/claims/",
            HTTP_X_TENANT_ID=str(self.tenant.pk),
        )
        self.assertEqual(response.status_code, 400)
