"""Regression tests for canonical USPTO/EPO ingestion and live search controls."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from audit.models import AuditLog
from patents.connectors.base import NormalizedPublication
from patents.connectors.epo import EPOOPSConnector
from patents.connectors.uspto import USPTOODPConnector
from patents.intelligence_models import (
    ConnectorSyncCursor,
    PatentFamily,
    PatentFamilyMember,
    PatentLegalEvent,
    PatentPublication,
    PatentSourceSnapshot,
)
from patents.intelligence_services import persist_publication
from tenants.models import Tenant, TenantMembership


class PatentConnectorParsingTests(TestCase):
    """Test normalized records from provider payloads without live network calls."""

    def test_uspto_record_mapping(self) -> None:
        record = USPTOODPConnector._map_record(
            {
                "applicationNumberText": "17999999",
                "applicationMetaData": {
                    "publicationNumber": "US20240123456A1",
                    "inventionTitle": "A safer connector",
                    "abstractText": "A connector with a protected interface.",
                    "filingDate": "2023-01-02",
                    "publicationDate": "2024-07-11",
                    "applicationStatusDescriptionText": "Granted",
                    "lastModifiedDateTime": "2025-01-02T10:00:00Z",
                    "applicantBag": [
                        {"nameLineOneText": "Example Applicant LLC"}
                    ],
                    "inventorBag": [
                        {"firstName": "Ada", "lastName": "Inventor"}
                    ],
                },
                "eventDataBag": [
                    {
                        "eventCode": "PG25",
                        "eventDate": "2024-07-12",
                        "eventDescriptionText": "Patent granted",
                        "applicationStatusDescriptionText": "Patented Case",
                        "eventIdentifier": "event-001",
                    }
                ],
            }
        )

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.provider, "USPTO")
        self.assertEqual(record.publication_number, "US20240123456A1")
        self.assertEqual(record.application_number, "17999999")
        self.assertEqual(record.provider_record_id, "17999999")
        self.assertEqual(record.publication_date, date(2024, 7, 11))
        self.assertEqual(record.legal_status, "GRANTED")
        self.assertEqual(record.applicant_names, ["Example Applicant LLC"])
        self.assertEqual(record.inventor_names, ["Ada Inventor"])
        self.assertEqual(len(record.legal_events), 1)
        self.assertEqual(record.legal_events[0].source_event_id, "event-001")
        self.assertEqual(record.legal_events[0].legal_status_after, "GRANTED")
        self.assertTrue(record.source_url.startswith("https://"))

    @override_settings(
        USPTO_ODP_API_KEY="unit-test-key",
        PATENTSHIELD_USPTO_API_URL=(
            "https://api.uspto.gov/api/v1/patent/applications/search"
        ),
    )
    def test_uspto_search_uses_documented_get_pagination(self) -> None:
        from patents.connectors.base import SharedProviderRateLimiter

        response = Mock(
            status_code=200,
            headers={},
        )
        response.json.return_value = {
            "count": 1,
            "patentFileWrapperDataBag": [
                {
                    "applicationNumberText": "17999999",
                    "applicationMetaData": {
                        "publicationNumber": "US20240123456A1",
                        "inventionTitle": "A safer connector",
                        "publicationDate": "2024-07-11",
                    },
                }
            ],
        }
        session = Mock()
        session.get.return_value = response
        connector = USPTOODPConnector(session=session)
        with patch.object(SharedProviderRateLimiter, "acquire"):
            records = connector.search("optical sensor", max_results=7)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].application_number, "17999999")
        kwargs = session.get.call_args.kwargs
        self.assertEqual(
            kwargs["params"],
            {"q": "optical sensor", "offset": 0, "limit": 7},
        )
        self.assertEqual(kwargs["headers"]["X-API-KEY"], "unit-test-key")
        self.assertEqual(kwargs["timeout"], 10)

    @override_settings(
        USPTO_ODP_API_KEY="unit-test-key",
        PATENTSHIELD_USPTO_API_URL=(
            "https://api.uspto.gov/api/v1/patent/applications/search"
        ),
    )
    def test_uspto_exact_application_lookup_uses_detail_endpoint(self) -> None:
        from patents.connectors.base import SharedProviderRateLimiter

        response = Mock(status_code=200, headers={})
        response.json.return_value = {
            "applicationNumberText": "17999999",
            "applicationMetaData": {
                "applicationNumberText": "17999999",
                "publicationNumber": "US20240123456A1",
                "inventionTitle": "A safer connector",
                "applicationStatusDescriptionText": "Patented Case",
            },
        }
        session = Mock()
        session.get.return_value = response
        connector = USPTOODPConnector(session=session)
        with patch.object(SharedProviderRateLimiter, "acquire"):
            record = connector.fetch_application("17/999,999")

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.application_number, "17999999")
        self.assertTrue(
            session.get.call_args.args[0].endswith("/applications/17999999")
        )

    @override_settings(
        USPTO_ODP_API_KEY="unit-test-key",
        PATENTSHIELD_USPTO_API_URL=(
            "https://api.uspto.gov/api/v1/patent/applications/search"
        ),
    )
    def test_uspto_exact_application_lookup_returns_none_for_404(self) -> None:
        from patents.connectors.base import SharedProviderRateLimiter

        session = Mock()
        session.get.return_value = Mock(status_code=404, headers={})
        connector = USPTOODPConnector(session=session)
        with patch.object(SharedProviderRateLimiter, "acquire"):
            self.assertIsNone(connector.fetch_application("17999999"))

    def test_epo_xml_search_and_legal_event_mapping(self) -> None:
        search_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <ops:world-patent-data
            xmlns:ops="http://ops.epo.org"
            xmlns="http://www.epo.org/exchange">
          <ops:exchange-documents>
            <exchange-document country="EP" doc-number="1234567" kind="A1">
              <bibliographic-data>
                <publication-reference>
                  <document-id document-id-type="docdb">
                    <country>EP</country><doc-number>1234567</doc-number>
                    <kind>A1</kind><date>20240612</date>
                  </document-id>
                </publication-reference>
                <application-reference>
                  <document-id document-id-type="docdb">
                    <country>EP</country><doc-number>22123456</doc-number>
                    <date>20220110</date>
                  </document-id>
                </application-reference>
                <invention-title lang="en">A novel optical sensor</invention-title>
                <abstract><p>An optical sensor with a receiving element.</p></abstract>
              </bibliographic-data>
            </exchange-document>
          </ops:exchange-documents>
        </ops:world-patent-data>"""
        records = EPOOPSConnector._parse_search_response(search_xml)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].provider, "EPO")
        self.assertEqual(records[0].publication_number, "EP1234567A1")
        self.assertEqual(records[0].title, "A novel optical sensor")
        self.assertEqual(records[0].publication_date, date(2024, 6, 12))

        legal_xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <ops:world-patent-data xmlns:ops="http://ops.epo.org">
          <ops:legal-event>
            <ops:event-code>GRANTED</ops:event-code>
            <ops:event-date>2025-03-01</ops:event-date>
            <ops:event-text>Patent granted</ops:event-text>
            <ops:status-after>granted</ops:status-after>
          </ops:legal-event>
        </ops:world-patent-data>"""
        events = EPOOPSConnector._parse_legal_events(legal_xml)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_code, "GRANTED")
        self.assertEqual(events[0].event_date, date(2025, 3, 1))
        self.assertEqual(events[0].legal_status_after, "GRANTED")


    def test_epo_exact_publication_fetch_uses_biblio_endpoint(self) -> None:
        from unittest.mock import patch

        from patents.connectors.base import SharedProviderRateLimiter

        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <ops:world-patent-data xmlns:ops="http://ops.epo.org"
            xmlns="http://www.epo.org/exchange">
          <ops:exchange-documents>
            <exchange-document country="EP" doc-number="7654321" kind="B1">
              <bibliographic-data>
                <publication-reference>
                  <document-id document-id-type="docdb">
                    <country>EP</country><doc-number>7654321</doc-number>
                    <kind>B1</kind><date>20250115</date>
                  </document-id>
                </publication-reference>
                <invention-title lang="en">A test publication</invention-title>
              </bibliographic-data>
            </exchange-document>
          </ops:exchange-documents>
        </ops:world-patent-data>"""
        response = Mock(status_code=200, content=xml, headers={})
        session = Mock()
        session.get.return_value = response
        connector = EPOOPSConnector(session=session)
        with (
            patch.object(connector, "_access_token", return_value="access-token"),
            patch.object(SharedProviderRateLimiter, "acquire"),
        ):
            record = connector.fetch_publication("EP7654321B1")

        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record.publication_number, "EP7654321B1")
        self.assertIn(
            "/published-data/publication/epodoc/EP7654321/biblio",
            session.get.call_args.args[0],
        )

    def test_epo_exact_publication_fetch_rejects_unrelated_first_result(self) -> None:
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <ops:world-patent-data xmlns:ops="http://ops.epo.org"
            xmlns="http://www.epo.org/exchange">
          <ops:exchange-documents>
            <exchange-document country="EP" doc-number="7654322" kind="B1">
              <bibliographic-data>
                <publication-reference>
                  <document-id document-id-type="docdb">
                    <country>EP</country><doc-number>7654322</doc-number>
                    <kind>B1</kind><date>20250115</date>
                  </document-id>
                </publication-reference>
                <invention-title lang="en">A different publication</invention-title>
              </bibliographic-data>
            </exchange-document>
          </ops:exchange-documents>
        </ops:world-patent-data>"""
        response = Mock(status_code=200, content=xml, headers={})
        session = Mock()
        session.get.return_value = response
        connector = EPOOPSConnector(session=session)
        from patents.connectors.base import SharedProviderRateLimiter
        with (
            patch.object(connector, "_access_token", return_value="access-token"),
            patch.object(SharedProviderRateLimiter, "acquire"),
        ):
            result = connector.fetch_publication("EP7654321B1")
        self.assertIsNone(result)


class CanonicalPatentPersistenceTests(TestCase):
    """Ensure source snapshots, families, and legal events are idempotent."""

    def _record(self) -> NormalizedPublication:
        from patents.connectors.base import NormalizedLegalEvent

        return NormalizedPublication(
            provider="EPO",
            provider_record_id="EP1234567A1",
            publication_number="EP1234567A1",
            application_number="EP22123456",
            jurisdiction="EP",
            kind_code="A1",
            title="A novel optical sensor",
            abstract="An optical sensor with a receiving element.",
            filing_date=date(2022, 1, 10),
            publication_date=date(2024, 6, 12),
            legal_status="PENDING",
            applicant_names=["Example Research Ltd."],
            inventor_names=["A. Inventor"],
            family_system="DOCDB",
            family_identifier="71234567",
            source_url="https://worldwide.espacenet.com/",
            source_payload={"example": "public source snapshot"},
            legal_events=[
                NormalizedLegalEvent(
                    source_event_id="event-1",
                    event_code="PUBLICATION",
                    event_date=date(2024, 6, 12),
                    title="Publication",
                    source_payload={"event": "publication", "sequence": 1},
                )
            ],
        )

    def test_persistence_is_idempotent_and_retains_provenance(self) -> None:
        record = self._record()
        first, created_first = persist_publication(record)
        second, created_second = persist_publication(record)

        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(PatentPublication.objects.count(), 1)
        self.assertEqual(
            PatentSourceSnapshot.objects.filter(publication=first).count(), 1
        )
        self.assertEqual(PatentLegalEvent.objects.filter(publication=first).count(), 1)
        self.assertEqual(PatentFamily.objects.count(), 1)
        self.assertEqual(
            PatentFamilyMember.objects.filter(publication=first).count(), 1
        )
        self.assertEqual(first.source_payload_hash, record.payload_sha256)
        self.assertEqual(str(first), "EP1234567A1")
        snapshot = PatentSourceSnapshot.objects.get(publication=first)
        snapshot.payload = {"tampered": True}
        with self.assertRaises(ValidationError):
            snapshot.save()

    def test_changed_legal_event_creates_new_snapshot_and_event_version(self) -> None:
        from dataclasses import replace

        record = self._record()
        original, _ = persist_publication(record)
        changed_event = replace(
            record.legal_events[0],
            title="Updated publication event",
            source_payload={"event": "publication", "sequence": 2},
        )
        updated_record = replace(record, legal_events=[changed_event])
        updated, created = persist_publication(updated_record)

        self.assertFalse(created)
        self.assertEqual(original.pk, updated.pk)
        snapshot_count = PatentSourceSnapshot.objects.filter(
            publication=updated
        ).count()
        event_count = PatentLegalEvent.objects.filter(publication=updated).count()
        self.assertEqual(snapshot_count, 2)
        self.assertEqual(event_count, 2)


class LivePatentSearchAPITests(TestCase):
    """Exercise tenant policy and audit masking at the public API boundary."""

    def setUp(self) -> None:
        user_model = get_user_model()
        self.user = user_model.objects.create_user(
            username="patent-connector-analyst",
            password="test-password-not-used",
        )
        self.tenant = Tenant.objects.create(
            name="Connector Tenant",
            slug="connector-tenant",
            status=Tenant.Status.ACTIVE,
        )
        TenantMembership.objects.create(
            tenant=self.tenant,
            user=self.user,
            role=TenantMembership.Role.ANALYST,
            status=TenantMembership.Status.ACTIVE,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_search_is_disabled_by_default(self) -> None:
        with override_settings(
            PATENTSHIELD_EXTERNAL_SEARCH_ENABLED=False,
            PATENTSHIELD_EXTERNAL_SEARCH_ALLOW_QUERY_DISCLOSURE=False,
        ):
            response = self.client.post(
                "/api/v1/patents/search-live/",
                {
                    "query": "confidential unpublished quantum interface",
                    "providers": ["USPTO"],
                    "max_results": 5,
                },
                format="json",
                HTTP_X_TENANT_ID=str(self.tenant.pk),
            )
        self.assertEqual(response.status_code, 403)
        self.assertFalse(AuditLog.objects.filter(action="PATENT_DATA_SEARCH").exists())

    @override_settings(
        PATENTSHIELD_EXTERNAL_SEARCH_ENABLED=True,
        PATENTSHIELD_EXTERNAL_SEARCH_ALLOW_QUERY_DISCLOSURE=True,
        PATENTSHIELD_EXTERNAL_SEARCH_ALLOW_ALL_TENANTS=True,
        PATENTSHIELD_EXTERNAL_QUERY_HASH_KEY="independent-query-hmac-key-" * 2,
    )
    def test_authorized_search_persists_canonical_data_without_raw_query_audit(
        self,
    ) -> None:
        record = NormalizedPublication(
            provider="USPTO",
            provider_record_id="US20240123456A1",
            publication_number="US20240123456A1",
            application_number="17999999",
            jurisdiction="US",
            kind_code="A1",
            title="A safer connector",
            abstract="A connector with a protected interface.",
            publication_date=date(2024, 7, 11),
            source_url="https://patentcenter.uspto.gov/applications/17999999",
            source_payload={"application_status": "Published"},
        )
        mock_connector = Mock()
        mock_connector.search.return_value = [record]
        with patch(
            "patents.intelligence_services.get_connector",
            return_value=mock_connector,
        ):
            response = self.client.post(
                "/api/v1/patents/search-live/",
                {
                    "query": "confidential unpublished quantum interface",
                    "providers": ["USPTO"],
                    "max_results": 5,
                },
                format="json",
                HTTP_X_TENANT_ID=str(self.tenant.pk),
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["result_count"], 1)
        self.assertEqual(PatentPublication.objects.count(), 1)
        self.assertEqual(PatentSourceSnapshot.objects.count(), 1)
        self.assertTrue(response.data["query_hmac"])
        audit = AuditLog.objects.get(action="PATENT_DATA_SEARCH")
        encoded_metadata = json.dumps(audit.metadata)
        self.assertNotIn("confidential unpublished quantum interface", encoded_metadata)
        self.assertEqual(audit.actor_user_id, self.user.pk)
        self.assertEqual(audit.metadata["stage"], "COMPLETED")

    @override_settings(
        PATENTSHIELD_EXTERNAL_SEARCH_ENABLED=True,
        PATENTSHIELD_EXTERNAL_SEARCH_ALLOW_QUERY_DISCLOSURE=True,
        PATENTSHIELD_EXTERNAL_SEARCH_ALLOW_ALL_TENANTS=True,
        PATENTSHIELD_EXTERNAL_QUERY_HASH_KEY="independent-query-hmac-key-" * 2,
    )
    def test_partial_persistence_returns_saved_records_and_partial_status(self) -> None:
        first = NormalizedPublication(
            provider="USPTO",
            provider_record_id="17999999",
            publication_number="US20240123456A1",
            application_number="17999999",
            jurisdiction="US",
            title="First connector result",
            source_url="https://patentcenter.uspto.gov/applications/17999999",
        )
        second = NormalizedPublication(
            provider="USPTO",
            provider_record_id="18222222",
            publication_number="US20240222222A1",
            application_number="18222222",
            jurisdiction="US",
            title="Second connector result",
            source_url="https://patentcenter.uspto.gov/applications/18222222",
        )
        connector = Mock()
        connector.search.return_value = [first, second]
        real_persist = persist_publication
        calls = {"count": 0}

        def persist_first_then_fail(
            record: NormalizedPublication,
        ) -> tuple[PatentPublication, bool]:
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("simulated persistence failure")
            return real_persist(record)

        with (
            patch(
                "patents.intelligence_services.get_connector",
                return_value=connector,
            ),
            patch(
                "patents.intelligence_services.persist_publication",
                side_effect=persist_first_then_fail,
            ),
        ):
            response = self.client.post(
                "/api/v1/patents/search-live/",
                {
                    "query": "optical sensor",
                    "providers": ["USPTO"],
                    "max_results": 5,
                },
                format="json",
                HTTP_X_TENANT_ID=str(self.tenant.pk),
            )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["result_count"], 1)
        self.assertEqual(response.data["providers"][0]["status"], "PARTIAL")

    def test_invalid_provider_is_rejected_before_provider_call(self) -> None:
        with override_settings(
            PATENTSHIELD_EXTERNAL_SEARCH_ENABLED=True,
            PATENTSHIELD_EXTERNAL_SEARCH_ALLOW_QUERY_DISCLOSURE=True,
            PATENTSHIELD_EXTERNAL_SEARCH_ALLOW_ALL_TENANTS=True,
            PATENTSHIELD_EXTERNAL_QUERY_HASH_KEY="independent-query-hmac-key-" * 2,
        ):
            response = self.client.post(
                "/api/v1/patents/search-live/",
                {"query": "optical sensor", "providers": ["GOOGLE"]},
                format="json",
                HTTP_X_TENANT_ID=str(self.tenant.pk),
            )
        self.assertEqual(response.status_code, 400)


class ConnectorSyncCursorTests(TestCase):
    """Verify the cursor can track provider-specific synchronization state."""

    def test_cursor_unique_scope_and_task_owner_field(self) -> None:
        cursor = ConnectorSyncCursor.objects.create(
            provider="USPTO",
            scope_key="publication:abc",
            status=ConnectorSyncCursor.Status.RUNNING,
            active_task_id="task-123",
        )
        self.assertEqual(cursor.active_task_id, "task-123")
        with self.assertRaises(Exception):
            ConnectorSyncCursor.objects.create(
                provider="USPTO",
                scope_key="publication:abc",
            )
