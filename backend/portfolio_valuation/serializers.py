"""Input validation and stable response representations for portfolio valuation."""
from rest_framework import serializers

from landscapes.models import LandscapeStudy
from patents.intelligence_models import PatentPublication
from portfolio_valuation.models import (
    PortfolioAnnotation,
    PortfolioAssetSnapshot,
    PortfolioDiligenceSnapshot,
    PortfolioScenario,
    PortfolioScenarioRevision,
    PortfolioValuationStudy,
)
from portfolio_valuation.services import sha256_json, validate_assumptions


class StudyCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=240)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    currency = serializers.RegexField(regex=r"^[A-Z]{3}$", required=False, default="USD")
    source_landscape_study_id = serializers.UUIDField(required=False, allow_null=True)

    def validate_title(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Title cannot be empty.")
        return value

    def validate_source_landscape_study_id(self, value):
        if value is None:
            return value
        tenant = self.context["tenant"]
        if not LandscapeStudy.objects.for_tenant(tenant.pk).filter(pk=value).exists():
            raise serializers.ValidationError("Source landscape study was not found in this tenant.")
        return value


class StudyUpdateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=240, required=False)
    description = serializers.CharField(required=False, allow_blank=True)
    currency = serializers.RegexField(regex=r"^[A-Z]{3}$", required=False)
    status = serializers.ChoiceField(choices=PortfolioValuationStudy.Status.choices, required=False)

    def validate(self, attrs):
        instance = self.context["instance"]
        if "currency" in attrs and PortfolioScenarioRevision.objects.filter(tenant_id=instance.tenant_id, scenario__study=instance).exists() and attrs["currency"] != instance.currency:
            raise serializers.ValidationError({"currency": "Study currency cannot change after scenario revisions exist; create a new study to avoid mixing currencies."})
        if attrs.get("status") == PortfolioValuationStudy.Status.READY:
            has_assets = PortfolioAssetSnapshot.objects.filter(tenant_id=instance.tenant_id, study=instance).exists()
            latest = instance.diligence_snapshots.order_by("-created_at").first()
            if not has_assets or latest is None or latest.asset_manifest_sha256 != current_asset_manifest(instance):
                raise serializers.ValidationError({"status": "A study can be marked READY only after assets are selected and a current diligence snapshot is saved."})
        return attrs


class AssetSelectionSerializer(serializers.Serializer):
    publication_ids = serializers.ListField(child=serializers.UUIDField(), min_length=1, max_length=500, allow_empty=False)

    def validate_publication_ids(self, values):
        if len(set(values)) != len(values):
            raise serializers.ValidationError("Duplicate publication IDs are not allowed in one request.")
        found = set(PatentPublication.objects.filter(pk__in=values).values_list("pk", flat=True))
        missing = set(values) - found
        if missing:
            raise serializers.ValidationError({"missing_publication_ids": [str(value) for value in sorted(missing, key=str)]})
        return values


class ScenarioCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=160)
    description = serializers.CharField(required=False, allow_blank=True, default="")
    method = serializers.ChoiceField(choices=PortfolioScenario.Method.choices)
    currency = serializers.RegexField(regex=r"^[A-Z]{3}$", required=False, allow_null=True)
    assumptions = serializers.JSONField()

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Scenario name cannot be empty.")
        return value

    def validate(self, attrs):
        study = self.context["study"]
        currency = attrs.get("currency") or study.currency
        if currency != study.currency:
            # Currency comparisons across scenarios are too easy to misread. Use separate studies.
            raise serializers.ValidationError({"currency": "Scenario currency must match the study currency. Use a separate study for another currency."})
        attrs["assumptions"] = validate_assumptions(attrs["method"], attrs["assumptions"])
        attrs["currency"] = currency
        return attrs


class ScenarioRevisionCreateSerializer(serializers.Serializer):
    assumptions = serializers.JSONField()
    currency = serializers.RegexField(regex=r"^[A-Z]{3}$", required=False, allow_null=True)

    def validate(self, attrs):
        scenario = self.context["scenario"]
        currency = attrs.get("currency") or scenario.study.currency
        if currency != scenario.study.currency:
            raise serializers.ValidationError({"currency": "Revision currency must match the study currency."})
        attrs["assumptions"] = validate_assumptions(scenario.method, attrs["assumptions"])
        attrs["currency"] = currency
        return attrs


class AnnotationCreateSerializer(serializers.Serializer):
    asset_id = serializers.UUIDField(required=False, allow_null=True)
    scenario_id = serializers.UUIDField(required=False, allow_null=True)
    kind = serializers.ChoiceField(choices=PortfolioAnnotation.Kind.choices, default=PortfolioAnnotation.Kind.DILIGENCE)
    body = serializers.CharField(max_length=12000)

    def validate_body(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Annotation text cannot be empty.")
        return value

    def validate(self, attrs):
        study = self.context["study"]
        asset_id = attrs.get("asset_id")
        scenario_id = attrs.get("scenario_id")
        if asset_id and not PortfolioAssetSnapshot.objects.filter(tenant_id=study.tenant_id, study=study, pk=asset_id).exists():
            raise serializers.ValidationError({"asset_id": "Asset must belong to this study and tenant."})
        if scenario_id and not PortfolioScenario.objects.filter(tenant_id=study.tenant_id, study=study, pk=scenario_id).exists():
            raise serializers.ValidationError({"scenario_id": "Scenario must belong to this study and tenant."})
        if asset_id and scenario_id:
            raise serializers.ValidationError("An annotation can target either an asset or a scenario, not both.")
        attrs["asset_id"] = asset_id
        attrs["scenario_id"] = scenario_id
        return attrs


def current_asset_manifest(study):
    rows = list(PortfolioAssetSnapshot.objects.filter(tenant_id=study.tenant_id, study=study).order_by("pk").values("id", "snapshot_sha256"))
    return sha256_json([{"id": str(item["id"]), "snapshot_sha256": item["snapshot_sha256"]} for item in rows])


def study_payload(study):
    latest = study.diligence_snapshots.order_by("-created_at").first()
    current_manifest = current_asset_manifest(study)
    latest_diligence = diligence_payload(latest) if latest else None
    if latest_diligence is not None:
        latest_diligence["is_current_for_current_assets"] = latest.asset_manifest_sha256 == current_manifest
        latest_diligence["current_asset_manifest_sha256"] = current_manifest
    return {
        "id": str(study.pk), "title": study.title, "description": study.description,
        "currency": study.currency, "status": study.status,
        "source_landscape_study_id": str(study.source_landscape_study_id) if study.source_landscape_study_id else None,
        "methodology_version": study.methodology_version,
        "asset_count": PortfolioAssetSnapshot.objects.filter(tenant_id=study.tenant_id, study=study).count(),
        "scenario_count": study.scenarios.filter(tenant_id=study.tenant_id).count(),
        "current_asset_manifest_sha256": current_manifest,
        "latest_diligence_snapshot": latest_diligence,
        "created_by": study.created_by.get_username(), "created_at": study.created_at,
        "updated_at": study.updated_at,
    }


def asset_payload(asset):
    return {
        "id": str(asset.pk), "publication_id": str(asset.publication_id),
        "publication_number": asset.publication_number, "application_number": asset.application_number,
        "jurisdiction": asset.jurisdiction, "title": asset.title,
        "filing_date": asset.filing_date, "publication_date": asset.publication_date,
        "grant_date": asset.grant_date, "legal_status": asset.legal_status,
        "applicant_names": asset.applicant_names, "ipc_codes": asset.ipc_codes, "cpc_codes": asset.cpc_codes,
        "family_system": asset.family_system, "family_identifier": asset.family_identifier,
        "source_url": asset.source_url, "source_payload_hash": asset.source_payload_hash,
        "snapshot_sha256": asset.snapshot_sha256, "source_last_synced_at": asset.source_last_synced_at,
        "forward_citation_count": asset.forward_citation_count,
        "backward_citation_count": asset.backward_citation_count,
        "citation_provenance": asset.citation_provenance,
        "citation_availability": "provider-reported" if asset.forward_citation_count is not None or asset.backward_citation_count is not None else "unknown",
        "legal_event_count": asset.legal_event_count, "latest_legal_event": asset.latest_legal_event,
        "captured_at": asset.captured_at,
    }


def diligence_payload(snapshot):
    if snapshot is None:
        return None
    return {
        "id": str(snapshot.pk), "asset_manifest_sha256": snapshot.asset_manifest_sha256,
        "methodology_version": snapshot.methodology_version, "asset_count": snapshot.asset_count,
        "composition_score": str(snapshot.composition_score) if snapshot.composition_score is not None else None,
        "metrics": snapshot.metrics, "created_by": snapshot.created_by.get_username(), "created_at": snapshot.created_at,
    }


def revision_payload(revision):
    if revision is None:
        return None
    return {
        "id": str(revision.pk), "revision_number": revision.revision_number, "currency": revision.currency,
        "assumptions": revision.assumptions, "calculated_result": revision.calculated_result,
        "input_sha256": revision.input_sha256, "asset_manifest_sha256": revision.asset_manifest_sha256,
        "methodology_version": revision.methodology_version, "created_by": revision.created_by.get_username(),
        "created_at": revision.created_at,
    }


def scenario_payload(scenario, current_manifest=None):
    """Serialize a scenario while honoring an optional one-query latest-revision prefetch."""
    prefetched = getattr(scenario, "prefetched_latest_revisions", None)
    latest = prefetched[0] if prefetched else None
    if prefetched is None:
        latest = scenario.revisions.filter(tenant_id=scenario.tenant_id).order_by("-revision_number").first()
    payload = revision_payload(latest) if latest else None
    if payload is not None:
        if current_manifest is None:
            current_manifest = current_asset_manifest(scenario.study)
        payload["is_current_for_current_assets"] = latest.asset_manifest_sha256 == current_manifest
        payload["current_asset_manifest_sha256"] = current_manifest
    return {
        "id": str(scenario.pk), "name": scenario.name, "description": scenario.description,
        "method": scenario.method, "latest_revision": payload,
        "created_by": scenario.created_by.get_username(), "created_at": scenario.created_at,
        "updated_at": scenario.updated_at,
    }


def annotation_payload(annotation):
    return {
        "id": str(annotation.pk), "study_id": str(annotation.study_id),
        "asset_id": str(annotation.asset_id) if annotation.asset_id else None,
        "scenario_id": str(annotation.scenario_id) if annotation.scenario_id else None,
        "kind": annotation.kind, "body": annotation.body, "created_by": annotation.created_by.get_username(),
        "created_at": annotation.created_at, "updated_at": annotation.updated_at,
    }
