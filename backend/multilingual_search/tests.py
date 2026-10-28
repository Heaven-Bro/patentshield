"""Phase 5 tests for translation safety, provider behavior, and tenant isolation."""

import hashlib
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient

from indexing.models import TextChunk
from multilingual_search.models import TranslationRecord
from multilingual_search.services import (
    TranslationTenantNotAllowed,
    _tenant_translation_allowed,
    _translate_with_libretranslate,
)
from patents.models import Patent, PatentDocumentVersion
from tenants.models import Tenant, TenantMembership


class TranslationConfigurationTests(SimpleTestCase):
    @override_settings(
        PATENTSHIELD_TRANSLATION_ENABLED=True,
        PATENTSHIELD_TRANSLATION_ALLOWED_TENANTS=["tenant-approved"],
        PATENTSHIELD_TRANSLATION_URL="https://translate.example.test",
    )
    def test_tenant_must_be_explicitly_allowlisted(self):
        _tenant_translation_allowed("tenant-approved")
        with self.assertRaises(TranslationTenantNotAllowed):
            _tenant_translation_allowed("tenant-not-approved")

    @override_settings(
        PATENTSHIELD_TRANSLATION_ENABLED=False,
        PATENTSHIELD_TRANSLATION_ALLOWED_TENANTS=["tenant-approved"],
        PATENTSHIELD_TRANSLATION_URL="https://translate.example.test",
    )
    def test_translation_is_disabled_by_default_policy(self):
        from multilingual_search.services import TranslationDisabled

        with self.assertRaises(TranslationDisabled):
            _tenant_translation_allowed("tenant-approved")

    @override_settings(
        PATENTSHIELD_TRANSLATION_URL="https://translate.example.test/api",
        PATENTSHIELD_TRANSLATION_API_KEY="test-key-not-a-real-secret",
        PATENTSHIELD_TRANSLATION_TIMEOUT_SECONDS=5,
        PATENTSHIELD_TRANSLATION_MAX_OUTPUT_CHARS=30000,
    )
    @patch("multilingual_search.services.requests.post")
    def test_provider_uses_bounded_request_without_following_redirects(self, post_mock):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"translatedText": "无线通信系统"}
        post_mock.return_value = response

        translated = _translate_with_libretranslate("wireless communication system", "und", "zh")

        self.assertEqual(translated, "无线通信系统")
        args, kwargs = post_mock.call_args
        self.assertEqual(args[0], "https://translate.example.test/api/translate")
        self.assertEqual(kwargs["timeout"], 5)
        self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(kwargs["json"]["source"], "auto")
        self.assertEqual(kwargs["json"]["target"], "zh")
        self.assertEqual(kwargs["json"]["api_key"], "test-key-not-a-real-secret")

    @override_settings(
        PATENTSHIELD_TRANSLATION_URL="https://translate.example.test",
        PATENTSHIELD_TRANSLATION_TIMEOUT_SECONDS=5,
        PATENTSHIELD_TRANSLATION_MAX_OUTPUT_CHARS=30000,
    )
    @patch("multilingual_search.services.requests.post")
    def test_provider_rejects_http_redirects(self, post_mock):
        from multilingual_search.services import TranslationProviderError

        response = Mock()
        response.status_code = 302
        response.json.return_value = {"translatedText": "do not use"}
        post_mock.return_value = response
        with self.assertRaises(TranslationProviderError):
            _translate_with_libretranslate("patent text", "und", "ja")


class TranslationTenantIsolationTests(TestCase):
    def setUp(self):
        user_model = get_user_model()
        self.user = user_model.objects.create_user(username="phase5-owner", password="not-a-real-password")
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="phase5-tenant-a")
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="phase5-tenant-b")
        for tenant in (self.tenant_a, self.tenant_b):
            TenantMembership.objects.create(
                tenant=tenant,
                user=self.user,
                role=TenantMembership.Role.OWNER,
                status=TenantMembership.Status.ACTIVE,
            )
        patent = Patent.objects.create(
            tenant=self.tenant_a,
            title="CJK translation test patent",
            created_by=self.user,
        )
        version = PatentDocumentVersion.objects.create(
            tenant=self.tenant_a,
            patent=patent,
            version_number=1,
            storage_key="test/phase5.pdf",
            content_sha256="a" * 64,
        )
        self.chunk = TextChunk.objects.create(
            tenant=self.tenant_a,
            patent=patent,
            document_version=version,
            section_type=TextChunk.SectionType.ABSTRACT,
            text="一种用于无线通信的电路",
            language_code="zh",
        )
        self.translation = TranslationRecord.objects.create(
            tenant=self.tenant_a,
            source_chunk=self.chunk,
            source_text_sha256=hashlib.sha256(self.chunk.text.encode("utf-8")).hexdigest(),
            source_language="zh",
            target_language="en",
            translated_text="A circuit for wireless communication",
            translation_sha256="c" * 64,
            provider="libretranslate",
            provider_version="test-v1",
            created_by=self.user,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

    def test_translation_detail_cannot_cross_tenant_boundary(self):
        response = self.client.get(
            f"/api/v1/multilingual-search/translations/{self.translation.pk}/",
            HTTP_X_TENANT_ID=str(self.tenant_b.pk),
        )
        self.assertEqual(response.status_code, 404)

    def test_translation_detail_returns_source_and_translation_in_owner_tenant(self):
        response = self.client.get(
            f"/api/v1/multilingual-search/translations/{self.translation.pk}/",
            HTTP_X_TENANT_ID=str(self.tenant_a.pk),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["original_text"], self.chunk.text)
        self.assertEqual(response.data["translated_text"], self.translation.translated_text)
        self.assertTrue(response.data["source_preserved"])
