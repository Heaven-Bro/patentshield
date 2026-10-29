"""Phase 10 deterministic finance, evidence-provenance, and tenant isolation tests."""
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APIClient

from patents.intelligence_models import PatentPublication
from portfolio_valuation.financial import FinancialAssumptionError, calculate_scenario, validate_assumptions
from portfolio_valuation.models import (
    PortfolioAssetSnapshot,
    PortfolioScenario,
    PortfolioScenarioRevision,
    PortfolioValuationStudy,
)
from portfolio_valuation.services import add_publication_to_study
from tenants.models import Tenant, TenantMembership


class FinancialCalculationTests(SimpleTestCase):
    def test_cost_approach_is_explicit_and_deterministic(self):
        result = calculate_scenario("COST", "USD", {
            "replacement_cost": "100", "obsolescence_rate": "0.20", "documented_additional_costs": "5", "basis_note": "test cost memo"
        })
        self.assertEqual(result["indicated_value"], "85.00")
        self.assertEqual(result["components"]["remaining_replacement_cost"], "80.00")
        self.assertIn("not an objective market price", result["disclaimer"])

    def test_income_approach_discounting(self):
        result = calculate_scenario("INCOME", "USD", {
            "annual_revenue": "100", "royalty_rate": "0.10", "annual_revenue_growth_rate": "0",
            "discount_rate": "0.10", "tax_rate": "0.25", "projection_years": 1, "basis_note": "test revenue forecast",
        })
        self.assertEqual(result["components"]["projected_cash_flows"][0]["after_tax_royalty_savings"], "7.50")
        self.assertEqual(result["indicated_value"], "6.82")

    def test_market_approach_weighted_adjusted_comparables(self):
        result = calculate_scenario("MARKET", "USD", {"comparables": [
            {"label": "Comparable A", "transaction_value": "100", "weight": "1", "adjustment_rate": "0.10", "evidence_reference": "deal-a"},
            {"label": "Comparable B", "transaction_value": "200", "weight": "3", "adjustment_rate": "0", "evidence_reference": "deal-b"},
        ]})
        self.assertEqual(result["indicated_value"], "177.50")
        self.assertEqual(result["components"]["comparable_count"], 2)
        self.assertEqual(result["components"]["comparables"][0]["adjusted_value"], "110.00")

    def test_market_requires_comparable_source_reference(self):
        with self.assertRaises(FinancialAssumptionError):
            validate_assumptions("MARKET", {"comparables": [{"label": "Unverified", "transaction_value": "10"}]})

    def test_terminal_growth_must_be_below_discount_rate(self):
        assumptions = {
            "annual_revenue": "100", "royalty_rate": "0.1", "annual_revenue_growth_rate": "0",
            "discount_rate": "0.08", "tax_rate": "0.2", "projection_years": 5, "basis_note": "test forecast",
            "include_terminal_value": True, "terminal_growth_rate": "0.08",
        }
        with self.assertRaises(FinancialAssumptionError):
            validate_assumptions("INCOME", assumptions)

    def test_financial_inputs_reject_nan_negative_and_bool(self):
        for assumptions in (
            {"replacement_cost": "NaN", "obsolescence_rate": "0"},
            {"replacement_cost": "-1", "obsolescence_rate": "0"},
            {"replacement_cost": True, "obsolescence_rate": "0"},
        ):
            with self.subTest(assumptions=assumptions), self.assertRaises(FinancialAssumptionError):
                validate_assumptions("COST", assumptions)


class PortfolioValuationTenantTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username="valuator", password="not-used")
        self.tenant_a = Tenant.objects.create(name="Tenant A", slug="valuation-a")
        self.tenant_b = Tenant.objects.create(name="Tenant B", slug="valuation-b")
        TenantMembership.objects.create(tenant=self.tenant_a, user=self.user, role=TenantMembership.Role.OWNER, status=TenantMembership.Status.ACTIVE)
        TenantMembership.objects.create(tenant=self.tenant_b, user=self.user, role=TenantMembership.Role.OWNER, status=TenantMembership.Status.ACTIVE)
        self.study = PortfolioValuationStudy.objects.create(tenant=self.tenant_a, title="A portfolio", currency="USD", created_by=self.user)

    def test_cross_tenant_study_is_not_found(self):
        client = APIClient()
        client.force_authenticate(self.user)
        response = client.get(f"/api/v1/valuation/studies/{self.study.pk}/", HTTP_X_TENANT_ID=str(self.tenant_b.pk))
        self.assertEqual(response.status_code, 404)

    def test_portfolio_valuation_study_is_registered_for_matter_acl(self):
        from enterprise_identity.services import resolve_matter

        resolved = resolve_matter(self.tenant_a, "portfolio_valuation", self.study.pk)
        self.assertEqual(resolved.pk, self.study.pk)
        self.assertEqual(resolved.__class__, PortfolioValuationStudy)

    def test_asset_selection_batches_source_snapshot_and_legal_event_queries(self):
        from patents.intelligence_models import PatentLegalEvent, PatentSourceSnapshot

        publications = [
            PatentPublication.objects.create(
                provider=PatentPublication.Provider.USPTO,
                provider_record_id=f"valuation-batch-{index}",
                publication_number=f"US-BATCH-{index}",
                jurisdiction="US",
                title=f"Batch publication {index}",
                legal_status=PatentPublication.LegalStatus.UNKNOWN,
            )
            for index in range(3)
        ]
        client = APIClient()
        client.force_authenticate(self.user)
        with CaptureQueriesContext(connection) as captured:
            response = client.post(
                f"/api/v1/valuation/studies/{self.study.pk}/assets/",
                {"publication_ids": [str(item.pk) for item in publications]},
                format="json",
                HTTP_X_TENANT_ID=str(self.tenant_a.pk),
            )
        self.assertEqual(response.status_code, 201, response.data)
        sql = [item["sql"].casefold() for item in captured.captured_queries]
        snapshot_table = PatentSourceSnapshot._meta.db_table.casefold()
        event_table = PatentLegalEvent._meta.db_table.casefold()
        self.assertEqual(sum(snapshot_table in statement for statement in sql), 1)
        self.assertEqual(sum(event_table in statement for statement in sql), 2)

    def test_provider_omission_does_not_become_zero_citations(self):
        publication = PatentPublication.objects.create(
            provider=PatentPublication.Provider.USPTO, provider_record_id="valuation-test-1",
            publication_number="US-TEST-1", jurisdiction="US", title="Example publication",
            legal_status=PatentPublication.LegalStatus.UNKNOWN, raw_metadata={},
        )
        asset, created = add_publication_to_study(self.study, publication)
        self.assertTrue(created)
        self.assertIsNone(asset.forward_citation_count)
        self.assertIsNone(asset.backward_citation_count)
        self.assertEqual(asset.citation_provenance["availability"], "unknown")

    def test_asset_snapshots_are_immutable(self):
        publication = PatentPublication.objects.create(
            provider=PatentPublication.Provider.USPTO, provider_record_id="valuation-test-2",
            publication_number="US-TEST-2", jurisdiction="US", title="Immutable publication",
            legal_status=PatentPublication.LegalStatus.PENDING,
        )
        asset, _created = add_publication_to_study(self.study, publication)
        asset.title = "Mutated title"
        with self.assertRaises(DjangoValidationError):
            asset.save()

    def test_scenario_revision_cannot_be_overwritten(self):
        scenario = PortfolioScenario.objects.create(
            tenant=self.tenant_a, study=self.study, name="Cost base",
            method=PortfolioScenario.Method.COST, created_by=self.user,
        )
        from portfolio_valuation.services import create_scenario_revision
        revision = create_scenario_revision(scenario, self.user, {"replacement_cost": "100", "obsolescence_rate": "0.2", "basis_note": "Test cost assumptions"})
        revision.assumptions = {"replacement_cost": "1", "obsolescence_rate": "0"}
        with self.assertRaises(DjangoValidationError):
            revision.save()
