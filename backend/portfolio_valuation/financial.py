"""Pure-standard-library valuation calculations for deterministic unit testing."""
from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
from typing import Any
from urllib.parse import urlsplit


class FinancialAssumptionError(ValueError):
    def __init__(self, field: str, detail: str):
        super().__init__(detail)
        self.field = field
        self.detail = detail


MONEY_QUANTUM = Decimal("0.01")


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool):
        raise FinancialAssumptionError(field, "A numeric value is required.")
    raw = str(value).strip()
    if not raw or len(raw) > 80:
        raise FinancialAssumptionError(field, "Enter a number with at most 80 characters.")
    try:
        result = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError):
        raise FinancialAssumptionError(field, "Enter a valid decimal number.") from None
    if not result.is_finite():
        raise FinancialAssumptionError(field, "NaN and infinite values are not allowed.")
    exponent = result.as_tuple().exponent
    if exponent < -18 or exponent > 18:
        raise FinancialAssumptionError(field, "Use at most 18 fractional decimal places and a supported exponent.")
    return result


def _rate(value: Any, field: str, low: Decimal = Decimal("0"), high: Decimal = Decimal("1")) -> Decimal:
    result = _decimal(value, field)
    if not low <= result <= high:
        raise FinancialAssumptionError(field, f"Value must be between {low} and {high}, inclusive.")
    return result


def _nonnegative(value: Any, field: str) -> Decimal:
    result = _decimal(value, field)
    if result < 0:
        raise FinancialAssumptionError(field, "Value cannot be negative.")
    if result != 0 and result.adjusted() > 15:
        raise FinancialAssumptionError(field, "Value exceeds the supported numeric magnitude (10^15).")
    return result


def money(value: Decimal) -> str:
    return str(value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP))


def validate_assumptions(method: str, assumptions: Any) -> dict[str, Any]:
    if not isinstance(assumptions, dict):
        raise FinancialAssumptionError("assumptions", "Assumptions must be a JSON object.")
    if len(assumptions) > 40:
        raise FinancialAssumptionError("assumptions", "At most 40 assumption fields are permitted.")
    values = dict(assumptions)
    if method == "COST":
        allowed = {"replacement_cost", "obsolescence_rate", "documented_additional_costs", "basis_note", "source_reference"}
        if set(values) - allowed:
            raise FinancialAssumptionError("assumptions", f"Unsupported cost assumption(s): {', '.join(sorted(set(values) - allowed))}.")
        if "replacement_cost" not in values or "obsolescence_rate" not in values:
            raise FinancialAssumptionError("assumptions", "replacement_cost and obsolescence_rate are required.")
        values["replacement_cost"] = str(_nonnegative(values["replacement_cost"], "replacement_cost"))
        values["obsolescence_rate"] = str(_rate(values["obsolescence_rate"], "obsolescence_rate"))
        values["documented_additional_costs"] = str(_nonnegative(values.get("documented_additional_costs", 0), "documented_additional_costs"))
    elif method == "INCOME":
        allowed = {"annual_revenue", "royalty_rate", "annual_revenue_growth_rate", "discount_rate", "tax_rate", "projection_years", "terminal_growth_rate", "include_terminal_value", "basis_note", "source_reference"}
        if set(values) - allowed:
            raise FinancialAssumptionError("assumptions", f"Unsupported income assumption(s): {', '.join(sorted(set(values) - allowed))}.")
        required = {"annual_revenue", "royalty_rate", "annual_revenue_growth_rate", "discount_rate", "tax_rate", "projection_years"}
        missing = required - set(values)
        if missing:
            raise FinancialAssumptionError("assumptions", f"Missing income assumption(s): {', '.join(sorted(missing))}.")
        values["annual_revenue"] = str(_nonnegative(values["annual_revenue"], "annual_revenue"))
        values["royalty_rate"] = str(_rate(values["royalty_rate"], "royalty_rate"))
        values["annual_revenue_growth_rate"] = str(_rate(values["annual_revenue_growth_rate"], "annual_revenue_growth_rate", Decimal("-0.99"), Decimal("3")))
        values["discount_rate"] = str(_rate(values["discount_rate"], "discount_rate", Decimal("0.0001"), Decimal("1")))
        values["tax_rate"] = str(_rate(values["tax_rate"], "tax_rate"))
        raw_years = values["projection_years"]
        try:
            years = int(raw_years)
        except (TypeError, ValueError):
            raise FinancialAssumptionError("projection_years", "Use an integer between 1 and 20.") from None
        if isinstance(raw_years, bool) or str(years) != str(raw_years) or not 1 <= years <= 20:
            raise FinancialAssumptionError("projection_years", "Use an integer between 1 and 20.")
        values["projection_years"] = years
        include_terminal = values.get("include_terminal_value", False)
        if not isinstance(include_terminal, bool):
            raise FinancialAssumptionError("include_terminal_value", "Use true or false.")
        values["include_terminal_value"] = include_terminal
        if include_terminal:
            if "terminal_growth_rate" not in values:
                raise FinancialAssumptionError("terminal_growth_rate", "Required when include_terminal_value is true.")
            terminal_growth = _rate(values["terminal_growth_rate"], "terminal_growth_rate", Decimal("-0.99"), Decimal("0.99"))
            if terminal_growth >= Decimal(values["discount_rate"]):
                raise FinancialAssumptionError("terminal_growth_rate", "Terminal growth must be below the discount rate.")
            values["terminal_growth_rate"] = str(terminal_growth)
        else:
            values["terminal_growth_rate"] = str(_rate(values.get("terminal_growth_rate", 0), "terminal_growth_rate", Decimal("-0.99"), Decimal("0.99")))
    elif method == "MARKET":
        allowed = {"comparables", "basis_note", "source_reference"}
        if set(values) - allowed:
            raise FinancialAssumptionError("assumptions", f"Unsupported market assumption(s): {', '.join(sorted(set(values) - allowed))}.")
        comparables = values.get("comparables")
        if not isinstance(comparables, list) or not 1 <= len(comparables) <= 50:
            raise FinancialAssumptionError("comparables", "Provide between 1 and 50 explicitly sourced comparables.")
        cleaned = []
        for index, comparable in enumerate(comparables):
            field = f"comparables[{index}]"
            if not isinstance(comparable, dict):
                raise FinancialAssumptionError("comparables", f"Comparable {index + 1} must be an object.")
            allowed_fields = {"label", "transaction_value", "weight", "adjustment_rate", "evidence_reference", "source_url", "transaction_date", "notes"}
            if set(comparable) - allowed_fields:
                raise FinancialAssumptionError("comparables", f"Comparable {index + 1} has unsupported fields.")
            label = str(comparable.get("label", "")).strip()
            evidence = str(comparable.get("evidence_reference", "")).strip()
            source_url = str(comparable.get("source_url", "")).strip()
            if not label or (not evidence and not source_url):
                raise FinancialAssumptionError("comparables", f"Comparable {index + 1} requires a label and a source reference or URL.")
            if source_url:
                parsed_url = urlsplit(source_url)
                if parsed_url.scheme.lower() != "https" or not parsed_url.hostname or parsed_url.username or parsed_url.password:
                    raise FinancialAssumptionError("comparables", f"Comparable {index + 1} source_url must be a valid HTTPS URL without embedded credentials.")
            value = _nonnegative(comparable.get("transaction_value"), f"{field}.transaction_value")
            weight = _nonnegative(comparable.get("weight", 1), f"{field}.weight")
            if weight <= 0:
                raise FinancialAssumptionError("comparables", f"Comparable {index + 1} weight must be greater than zero.")
            adjustment = _rate(comparable.get("adjustment_rate", 0), f"{field}.adjustment_rate", Decimal("-0.90"), Decimal("3.00"))
            transaction_date = str(comparable.get("transaction_date", "")).strip()
            if transaction_date:
                try:
                    date.fromisoformat(transaction_date)
                except ValueError:
                    raise FinancialAssumptionError("comparables", f"Comparable {index + 1} transaction_date must use YYYY-MM-DD.") from None
            cleaned.append({
                "label": label, "transaction_value": str(value), "weight": str(weight), "adjustment_rate": str(adjustment),
                "evidence_reference": evidence, "source_url": source_url,
                "transaction_date": transaction_date, "notes": str(comparable.get("notes", ""))[:3000],
            })
        values["comparables"] = cleaned
    else:
        raise FinancialAssumptionError("method", "Method must be INCOME, MARKET or COST.")
    for field in ("basis_note", "source_reference"):
        if field in values:
            values[field] = str(values[field])[:3000]
    if method in {"COST", "INCOME"} and not str(values.get("basis_note", "")).strip() and not str(values.get("source_reference", "")).strip():
        raise FinancialAssumptionError("basis_note", "Provide a basis_note or source_reference so financial assumptions remain traceable.")
    return values


def _calculate_scenario_inner(method: str, currency: str, assumptions: dict[str, Any]) -> dict[str, Any]:
    values = validate_assumptions(method, assumptions)
    common = {
        "method": method, "currency": currency, "methodology_version": "valuation-methods-v1",
        "disclaimer": "A scenario output driven by user-supplied assumptions, not an objective market price, legal opinion, or guaranteed realizable value.",
        "assumption_source_note": values.get("basis_note") or values.get("source_reference", ""),
    }
    if method == "COST":
        replacement = Decimal(values["replacement_cost"])
        obsolescence = Decimal(values["obsolescence_rate"])
        additional = Decimal(values["documented_additional_costs"])
        remaining = max(Decimal("0"), replacement * (Decimal("1") - obsolescence))
        common.update({
            "indicated_value": money(remaining + additional),
            "calculation": "max(0, replacement_cost × (1 − obsolescence_rate)) + documented_additional_costs",
            "components": {"replacement_cost": money(replacement), "obsolescence_rate": str(obsolescence), "documented_additional_costs": money(additional), "remaining_replacement_cost": money(remaining)},
            "limitations": ["Replacement cost and obsolescence are supplied by the user; neither is independently verified."],
        })
        return common
    if method == "INCOME":
        revenue = Decimal(values["annual_revenue"])
        royalty = Decimal(values["royalty_rate"])
        growth = Decimal(values["annual_revenue_growth_rate"])
        discount = Decimal(values["discount_rate"])
        tax = Decimal(values["tax_rate"])
        years = int(values["projection_years"])
        cashflows = []
        present_value = Decimal("0")
        last_savings = Decimal("0")
        for year in range(1, years + 1):
            projected_revenue = revenue * ((Decimal("1") + growth) ** (year - 1))
            savings = projected_revenue * royalty * (Decimal("1") - tax)
            last_savings = savings
            pv = savings / ((Decimal("1") + discount) ** year)
            cashflows.append({"year": year, "projected_revenue": money(projected_revenue), "after_tax_royalty_savings": money(savings), "present_value": money(pv)})
            present_value += pv
        terminal_value = Decimal("0")
        terminal_pv = Decimal("0")
        if values["include_terminal_value"]:
            growth_terminal = Decimal(values["terminal_growth_rate"])
            terminal_value = last_savings * (Decimal("1") + growth_terminal) / (discount - growth_terminal)
            terminal_pv = terminal_value / ((Decimal("1") + discount) ** years)
        common.update({
            "indicated_value": money(present_value + terminal_pv),
            "calculation": "PV of projected after-tax royalty savings" + (" + discounted terminal value" if values["include_terminal_value"] else ""),
            "components": {"projected_cash_flows": cashflows, "present_value_of_projection": money(present_value), "terminal_value_at_horizon": money(terminal_value), "present_value_of_terminal_value": money(terminal_pv)},
            "limitations": ["Revenue, growth, royalty, tax and discount assumptions are user-supplied.", "The relief-from-royalty framing may be unsuitable for some asset portfolios; professional review is required."],
        })
        return common
    comparables = values["comparables"]
    weights = sum((Decimal(row["weight"]) for row in comparables), Decimal("0"))
    weighted_total = Decimal("0")
    details = []
    for row in comparables:
        transacted = Decimal(row["transaction_value"])
        adjustment = Decimal(row["adjustment_rate"])
        weight = Decimal(row["weight"])
        adjusted = transacted * (Decimal("1") + adjustment)
        normalized = weight / weights
        weighted_total += adjusted * normalized
        details.append({"label": row["label"], "transaction_value": money(transacted), "adjustment_rate": str(adjustment), "adjusted_value": money(adjusted), "weight": str(weight), "normalized_weight": str(normalized.quantize(Decimal("0.0001"))), "evidence_reference": row["evidence_reference"], "source_url": row["source_url"], "transaction_date": row["transaction_date"]})
    common.update({
        "indicated_value": money(weighted_total),
        "calculation": "weighted mean of user-supplied comparable transaction values adjusted by user-supplied adjustment rates",
        "components": {"comparables": details, "comparable_count": len(details), "weight_sum": str(weights)},
        "limitations": ["Comparable transactions and adjustments are user-supplied and have not been independently verified.", "Comparability, deal structure, jurisdiction, and transaction date may materially affect results."],
    })
    return common


def calculate_scenario(method: str, currency: str, assumptions: dict[str, Any]) -> dict[str, Any]:
    """Run bounded Decimal calculations under sufficient precision for full projections."""
    with localcontext() as context:
        context.prec = 80
        return _calculate_scenario_inner(method, currency, assumptions)
