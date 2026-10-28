"""Standard-library unit tests for Phase 10 financial calculations."""
import unittest
from decimal import Decimal

from portfolio_valuation.financial import (
    FinancialAssumptionError,
    calculate_scenario,
    validate_assumptions,
)


class CostApproachTests(unittest.TestCase):
    def test_replacement_cost_less_obsolescence(self):
        result = calculate_scenario("COST", "USD", {
            "replacement_cost": "100", "obsolescence_rate": "0.20",
            "documented_additional_costs": "5", "basis_note": "Engineering memo ENG-12",
        })
        self.assertEqual(result["indicated_value"], "85.00")
        self.assertEqual(result["components"]["remaining_replacement_cost"], "80.00")

    def test_nonfinite_negative_boolean_and_huge_values_rejected(self):
        candidates = ["NaN", "-1", True, "1e10000"]
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(FinancialAssumptionError):
                validate_assumptions("COST", {"replacement_cost": candidate, "obsolescence_rate": "0", "basis_note": "memo"})

    def test_cost_basis_reference_required(self):
        with self.assertRaises(FinancialAssumptionError):
            validate_assumptions("COST", {"replacement_cost": "10", "obsolescence_rate": "0"})


class IncomeApproachTests(unittest.TestCase):
    def test_present_value_of_one_year_royalty_savings(self):
        result = calculate_scenario("INCOME", "USD", {
            "annual_revenue": "100", "royalty_rate": "0.10", "annual_revenue_growth_rate": "0",
            "discount_rate": "0.10", "tax_rate": "0.25", "projection_years": 1,
            "basis_note": "Approved revenue forecast RF-1",
        })
        self.assertEqual(result["components"]["projected_cash_flows"][0]["after_tax_royalty_savings"], "7.50")
        self.assertEqual(result["indicated_value"], "6.82")

    def test_terminal_growth_must_be_below_discount_rate(self):
        with self.assertRaises(FinancialAssumptionError):
            validate_assumptions("INCOME", {
                "annual_revenue": "100", "royalty_rate": "0.1", "annual_revenue_growth_rate": "0",
                "discount_rate": "0.08", "tax_rate": "0.2", "projection_years": 5,
                "include_terminal_value": True, "terminal_growth_rate": "0.08", "basis_note": "Forecast RF-2",
            })

    def test_growth_rate_and_projection_horizon_are_bounded(self):
        values = {
            "annual_revenue": "100", "royalty_rate": "0.1", "annual_revenue_growth_rate": "0",
            "discount_rate": "0.08", "tax_rate": "0.2", "projection_years": 21, "basis_note": "Forecast RF-3",
        }
        with self.assertRaises(FinancialAssumptionError):
            validate_assumptions("INCOME", values)


    def test_high_magnitude_max_horizon_uses_safe_decimal_precision(self):
        result = calculate_scenario("INCOME", "USD", {
            "annual_revenue": "1000000000000000", "royalty_rate": "1",
            "annual_revenue_growth_rate": "3", "discount_rate": "0.0001",
            "tax_rate": "0", "projection_years": 20, "basis_note": "Stress test",
        })
        self.assertTrue(Decimal(result["indicated_value"]) > Decimal("1e25"))

    def test_excessive_decimal_scale_rejected(self):
        with self.assertRaises(FinancialAssumptionError):
            validate_assumptions("COST", {
                "replacement_cost": "0.0000000000000000001", "obsolescence_rate": "0", "basis_note": "test"
            })


class MarketApproachTests(unittest.TestCase):
    def test_weighted_mean_of_adjusted_comparables(self):
        result = calculate_scenario("MARKET", "USD", {"comparables": [
            {"label": "Comparable A", "transaction_value": "100", "weight": "1", "adjustment_rate": "0.10", "evidence_reference": "Licensed deal A"},
            {"label": "Comparable B", "transaction_value": "200", "weight": "3", "adjustment_rate": "0", "evidence_reference": "Licensed deal B"},
        ]})
        self.assertEqual(result["indicated_value"], "177.50")
        self.assertEqual(result["components"]["comparable_count"], 2)

    def test_comparable_requires_a_source_and_valid_https_or_reference(self):
        with self.assertRaises(FinancialAssumptionError):
            validate_assumptions("MARKET", {"comparables": [{"label": "No source", "transaction_value": "10"}]})
        with self.assertRaises(FinancialAssumptionError):
            validate_assumptions("MARKET", {"comparables": [{"label": "Bad URL", "transaction_value": "10", "source_url": "http://example.com"}]})
        with self.assertRaises(FinancialAssumptionError):
            validate_assumptions("MARKET", {"comparables": [{"label": "Credential URL", "transaction_value": "10", "source_url": "https://user:secret@example.com/deal"}]})


if __name__ == "__main__":
    unittest.main()
