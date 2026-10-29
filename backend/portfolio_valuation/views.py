"""Tenant-authorized portfolio diligence and scenario valuation APIs."""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import Count, F, Prefetch
from rest_framework import status
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from enterprise_identity.services import filter_visible_matter_queryset, require_matter_access
from landscapes.models import LandscapeStudy
from patents.intelligence_models import PatentLegalEvent, PatentPublication, PatentSourceSnapshot
from portfolio_valuation.models import (
    PortfolioAnnotation,
    PortfolioAssetSnapshot,
    PortfolioDiligenceSnapshot,
    PortfolioScenario,
    PortfolioScenarioRevision,
    PortfolioValuationStudy,
)
from portfolio_valuation.serializers import (
    AnnotationCreateSerializer,
    AssetSelectionSerializer,
    ScenarioCreateSerializer,
    ScenarioRevisionCreateSerializer,
    StudyCreateSerializer,
    StudyUpdateSerializer,
    annotation_payload,
    asset_payload,
    current_asset_manifest,
    diligence_payload,
    revision_payload,
    scenario_payload,
    study_payload,
)
from portfolio_valuation.services import (
    add_publication_to_study,
    audit_valuation_event,
    create_diligence_snapshot,
    create_scenario_revision,
)
from tenants.models import TenantMembership
from tenants.permissions import HasTenantAccess


_EDITOR_ROLES = frozenset({TenantMembership.Role.OWNER, TenantMembership.Role.ADMIN, TenantMembership.Role.ATTORNEY, TenantMembership.Role.ANALYST})
_REVIEW_ROLES = _EDITOR_ROLES | {TenantMembership.Role.REVIEWER}


class ValuationAPIView(APIView):
    permission_classes = [IsAuthenticated, HasTenantAccess]
    parser_classes = [JSONParser]

    def require_role(self, request: Request, roles: frozenset[str], message: str = "Your tenant role cannot perform this operation.") -> None:
        if request.tenant_membership.role not in roles:
            raise PermissionDenied(message)

    def get_study(self, request: Request, study_id: str, required: str = "READ") -> PortfolioValuationStudy:
        study = PortfolioValuationStudy.objects.for_tenant(request.tenant.pk).select_related("created_by", "source_landscape_study").filter(pk=study_id).first()
        if study is None:
            raise NotFound("Portfolio valuation study was not found in this tenant.")
        require_matter_access(request=request, matter_type="portfolio_valuation", matter_id=study.pk, required=required)
        return study


def _visible_studies(request):
    queryset = PortfolioValuationStudy.objects.for_tenant(request.tenant.pk).select_related("created_by", "source_landscape_study")
    return filter_visible_matter_queryset(queryset, tenant=request.tenant, user=request.user, matter_type="portfolio_valuation")


class StudyListCreateView(ValuationAPIView):
    def get(self, request: Request) -> Response:
        rows = _visible_studies(request).order_by("-updated_at")[:200]
        return Response({"count": _visible_studies(request).count(), "results": [study_payload(row) for row in rows]})

    def post(self, request: Request) -> Response:
        self.require_role(request, _EDITOR_ROLES)
        serializer = StudyCreateSerializer(data=request.data, context={"tenant": request.tenant})
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        landscape = None
        if values.get("source_landscape_study_id"):
            landscape = LandscapeStudy.objects.for_tenant(request.tenant.pk).filter(pk=values["source_landscape_study_id"]).first()
            if landscape is None:
                raise ValidationError({"source_landscape_study_id": "Source landscape study was not found in this tenant."})
            require_matter_access(request=request, matter_type="landscape_study", matter_id=landscape.pk, required="READ")
        study = PortfolioValuationStudy.objects.create(
            tenant=request.tenant, title=values["title"], description=values.get("description", ""),
            currency=values.get("currency", "USD"), source_landscape_study=landscape, created_by=request.user,
        )
        audit_valuation_event(tenant_id=study.tenant_id, actor_id=request.user.pk, action="PORTFOLIO_VALUATION_STUDY_CREATED", resource_type="PortfolioValuationStudy", resource_id=study.pk, metadata={"currency": study.currency, "source_landscape_linked": bool(landscape)})
        return Response(study_payload(study), status=status.HTTP_201_CREATED)


class StudyDetailView(ValuationAPIView):
    def get(self, request: Request, study_id: str) -> Response:
        return Response(study_payload(self.get_study(request, study_id)))

    def patch(self, request: Request, study_id: str) -> Response:
        self.require_role(request, _EDITOR_ROLES)
        authorized_study = self.get_study(request, study_id, required="EDIT")
        with transaction.atomic():
            study = PortfolioValuationStudy.objects.for_tenant(request.tenant.pk).select_for_update().filter(pk=authorized_study.pk).first()
            if study is None:
                raise NotFound("Portfolio valuation study was not found in this tenant.")
            if study.status == PortfolioValuationStudy.Status.ARCHIVED:
                raise ValidationError("Archived studies cannot be edited.")
            serializer = StudyUpdateSerializer(data=request.data, context={"instance": study}, partial=True)
            serializer.is_valid(raise_exception=True)
            for key, value in serializer.validated_data.items():
                setattr(study, key, value)
            study.save()
        audit_valuation_event(tenant_id=study.tenant_id, actor_id=request.user.pk, action="PORTFOLIO_VALUATION_STUDY_UPDATED", resource_type="PortfolioValuationStudy", resource_id=study.pk, metadata={"changed_fields": sorted(serializer.validated_data.keys())})
        return Response(study_payload(study))


class StudyAssetsView(ValuationAPIView):
    def get(self, request: Request, study_id: str) -> Response:
        study = self.get_study(request, study_id)
        total = PortfolioAssetSnapshot.objects.filter(tenant_id=study.tenant_id, study=study).count()
        rows = PortfolioAssetSnapshot.objects.filter(tenant_id=study.tenant_id, study=study).order_by("publication_number", "pk")[:1000]
        return Response({"count": total, "limit": 1000, "truncated": total > 1000, "results": [asset_payload(row) for row in rows]})

    def post(self, request: Request, study_id: str) -> Response:
        self.require_role(request, _EDITOR_ROLES)
        study = self.get_study(request, study_id, required="EDIT")
        if study.status == PortfolioValuationStudy.Status.ARCHIVED:
            raise ValidationError("Archived studies cannot accept new assets.")
        serializer = AssetSelectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ids = serializer.validated_data["publication_ids"]
        created = 0
        with transaction.atomic():
            # Serialize additions per study so concurrent requests cannot exceed the asset cap.
            locked_study = PortfolioValuationStudy.objects.for_tenant(request.tenant.pk).select_for_update().filter(pk=study.pk).first()
            if locked_study is None:
                raise NotFound("Portfolio valuation study was not found in this tenant.")
            if locked_study.status == PortfolioValuationStudy.Status.ARCHIVED:
                raise ValidationError("Archived studies cannot accept new assets.")
            current_count = PortfolioAssetSnapshot.objects.filter(tenant_id=locked_study.tenant_id, study=locked_study).count()
            existing_ids = set(PortfolioAssetSnapshot.objects.filter(tenant_id=locked_study.tenant_id, study=locked_study, publication_id__in=ids).values_list("publication_id", flat=True))
            new_count = len(set(ids) - existing_ids)
            if current_count + new_count > 5000:
                raise ValidationError({"publication_ids": "A valuation study is limited to 5,000 distinct publication snapshots."})
            publications = list(PatentPublication.objects.filter(pk__in=ids).order_by("pk"))
            if len(publications) != len(ids):
                raise ValidationError({"publication_ids": "One or more publications became unavailable. Refresh and retry."})

            # Batch source snapshots and legal-event aggregates for this selection.
            # PostgreSQL DISTINCT ON makes the latest row per publication one query.
            publication_ids = [publication.pk for publication in publications]
            latest_sources = PatentSourceSnapshot.objects.filter(
                publication_id__in=publication_ids,
            ).order_by("publication_id", "-fetched_at").distinct("publication_id")
            source_by_publication = {row.publication_id: row for row in latest_sources}
            event_counts = dict(
                PatentLegalEvent.objects.filter(publication_id__in=publication_ids)
                .values("publication_id")
                .annotate(total=Count("pk"))
                .values_list("publication_id", "total")
            )
            latest_events = PatentLegalEvent.objects.filter(
                publication_id__in=publication_ids,
            ).order_by(
                "publication_id", F("event_date").desc(nulls_last=True), "-observed_at",
            ).distinct("publication_id")
            latest_event_by_publication = {row.publication_id: row for row in latest_events}
            for publication in publications:
                _row, was_created = add_publication_to_study(
                    locked_study,
                    publication,
                    source_snapshot=source_by_publication.get(publication.pk),
                    source_snapshot_prefetched=True,
                    legal_event_count=event_counts.get(publication.pk, 0),
                    prefetched_latest_event=latest_event_by_publication.get(publication.pk),
                )
                created += int(was_created)
            if created and locked_study.status == PortfolioValuationStudy.Status.READY:
                # Adding assets invalidates the ready state; it must be re-reviewed.
                locked_study.status = PortfolioValuationStudy.Status.DRAFT
                locked_study.save(update_fields=("status", "updated_at"))
        audit_valuation_event(tenant_id=study.tenant_id, actor_id=request.user.pk, action="PORTFOLIO_VALUATION_ASSETS_SELECTED", resource_type="PortfolioValuationStudy", resource_id=study.pk, metadata={"requested_count": len(ids), "new_snapshot_count": created})
        return Response({"study_id": str(study.pk), "requested_count": len(ids), "new_snapshot_count": created, "total_asset_count": PortfolioAssetSnapshot.objects.filter(tenant_id=study.tenant_id, study=study).count(), "detail": "Source data was pinned from the latest available provider snapshot. Missing citation data remains unknown, not zero."}, status=status.HTTP_201_CREATED)


class StudyDiligenceView(ValuationAPIView):
    def get(self, request: Request, study_id: str) -> Response:
        study = self.get_study(request, study_id)
        rows = PortfolioDiligenceSnapshot.objects.filter(tenant_id=study.tenant_id, study=study).select_related("created_by").order_by("-created_at")[:50]
        return Response({"count": PortfolioDiligenceSnapshot.objects.filter(tenant_id=study.tenant_id, study=study).count(), "results": [diligence_payload(row) for row in rows]})

    def post(self, request: Request, study_id: str) -> Response:
        self.require_role(request, _EDITOR_ROLES)
        study = self.get_study(request, study_id, required="EDIT")
        if study.status == PortfolioValuationStudy.Status.ARCHIVED:
            raise ValidationError("Archived studies cannot create diligence snapshots.")
        snapshot = create_diligence_snapshot(study, request.user)
        audit_valuation_event(tenant_id=study.tenant_id, actor_id=request.user.pk, action="PORTFOLIO_DILIGENCE_SNAPSHOT_CREATED", resource_type="PortfolioDiligenceSnapshot", resource_id=snapshot.pk, metadata={"asset_count": snapshot.asset_count, "methodology_version": snapshot.methodology_version, "manifest_sha256": snapshot.asset_manifest_sha256})
        return Response(diligence_payload(snapshot), status=status.HTTP_201_CREATED)


class StudyScenariosView(ValuationAPIView):
    def get(self, request: Request, study_id: str) -> Response:
        study = self.get_study(request, study_id)
        latest_revision = Prefetch(
            "revisions",
            queryset=PortfolioScenarioRevision.objects.filter(tenant_id=study.tenant_id).order_by("-revision_number")[:1],
            to_attr="prefetched_latest_revisions",
        )
        rows = PortfolioScenario.objects.filter(tenant_id=study.tenant_id, study=study).select_related("created_by", "study").prefetch_related(latest_revision).order_by("name")
        count = rows.count()
        current_manifest = current_asset_manifest(study)
        return Response({"count": count, "results": [scenario_payload(row, current_manifest=current_manifest) for row in rows]})

    def post(self, request: Request, study_id: str) -> Response:
        self.require_role(request, _EDITOR_ROLES)
        study = self.get_study(request, study_id, required="EDIT")
        if study.status == PortfolioValuationStudy.Status.ARCHIVED:
            raise ValidationError("Archived studies cannot accept valuation scenarios.")
        serializer = ScenarioCreateSerializer(data=request.data, context={"study": study})
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        try:
            with transaction.atomic():
                scenario = PortfolioScenario.objects.create(tenant=study.tenant, study=study, name=values["name"], description=values.get("description", ""), method=values["method"], created_by=request.user)
                revision = create_scenario_revision(scenario, request.user, values["assumptions"], values["currency"])
        except IntegrityError as exc:
            raise ValidationError({"name": "A scenario with this name already exists in this study."}) from exc
        audit_valuation_event(tenant_id=study.tenant_id, actor_id=request.user.pk, action="PORTFOLIO_VALUATION_SCENARIO_CREATED", resource_type="PortfolioScenario", resource_id=scenario.pk, metadata={"method": scenario.method, "revision": revision.revision_number, "input_sha256": revision.input_sha256, "asset_manifest_sha256": revision.asset_manifest_sha256})
        scenario = PortfolioScenario.objects.get(pk=scenario.pk, tenant_id=study.tenant_id)
        return Response(scenario_payload(scenario), status=status.HTTP_201_CREATED)


class ScenarioRevisionsView(ValuationAPIView):
    def _get_scenario(self, request, scenario_id):
        scenario = PortfolioScenario.objects.for_tenant(request.tenant.pk).select_related("study").filter(pk=scenario_id).first()
        if scenario is None:
            raise NotFound("Valuation scenario was not found in this tenant.")
        require_matter_access(request=request, matter_type="portfolio_valuation", matter_id=scenario.study_id, required="READ")
        return scenario

    def get(self, request: Request, scenario_id: str) -> Response:
        scenario = self._get_scenario(request, scenario_id)
        rows = PortfolioScenarioRevision.objects.filter(tenant_id=scenario.tenant_id, scenario=scenario).select_related("created_by").order_by("-revision_number")[:100]
        return Response({"count": PortfolioScenarioRevision.objects.filter(tenant_id=scenario.tenant_id, scenario=scenario).count(), "results": [revision_payload(row) for row in rows]})

    def post(self, request: Request, scenario_id: str) -> Response:
        self.require_role(request, _EDITOR_ROLES)
        scenario = self._get_scenario(request, scenario_id)
        require_matter_access(request=request, matter_type="portfolio_valuation", matter_id=scenario.study_id, required="EDIT")
        if scenario.study.status == PortfolioValuationStudy.Status.ARCHIVED:
            raise ValidationError("Archived studies cannot accept valuation revisions.")
        serializer = ScenarioRevisionCreateSerializer(data=request.data, context={"scenario": scenario})
        serializer.is_valid(raise_exception=True)
        revision = create_scenario_revision(scenario, request.user, serializer.validated_data["assumptions"], serializer.validated_data["currency"])
        audit_valuation_event(tenant_id=scenario.tenant_id, actor_id=request.user.pk, action="PORTFOLIO_VALUATION_SCENARIO_REVISED", resource_type="PortfolioScenarioRevision", resource_id=revision.pk, metadata={"scenario_id": str(scenario.pk), "method": scenario.method, "revision": revision.revision_number, "input_sha256": revision.input_sha256, "asset_manifest_sha256": revision.asset_manifest_sha256})
        return Response(revision_payload(revision), status=status.HTTP_201_CREATED)


class StudyAnnotationsView(ValuationAPIView):
    def get(self, request: Request, study_id: str) -> Response:
        study = self.get_study(request, study_id)
        rows = PortfolioAnnotation.objects.filter(tenant_id=study.tenant_id, study=study).select_related("created_by").order_by("-created_at")[:500]
        return Response({"count": PortfolioAnnotation.objects.filter(tenant_id=study.tenant_id, study=study).count(), "results": [annotation_payload(row) for row in rows]})

    def post(self, request: Request, study_id: str) -> Response:
        self.require_role(request, _REVIEW_ROLES)
        study = self.get_study(request, study_id, required="EDIT")
        serializer = AnnotationCreateSerializer(data=request.data, context={"study": study})
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        asset = PortfolioAssetSnapshot.objects.filter(tenant_id=study.tenant_id, study=study, pk=values.get("asset_id")).first() if values.get("asset_id") else None
        scenario = PortfolioScenario.objects.filter(tenant_id=study.tenant_id, study=study, pk=values.get("scenario_id")).first() if values.get("scenario_id") else None
        note = PortfolioAnnotation.objects.create(tenant_id=study.tenant_id, study=study, asset=asset, scenario=scenario, kind=values["kind"], body=values["body"], created_by=request.user)
        audit_valuation_event(tenant_id=study.tenant_id, actor_id=request.user.pk, action="PORTFOLIO_VALUATION_ANNOTATION_CREATED", resource_type="PortfolioAnnotation", resource_id=note.pk, metadata={"kind": note.kind, "has_asset": bool(asset), "has_scenario": bool(scenario)})
        return Response(annotation_payload(note), status=status.HTTP_201_CREATED)


class StudyScenarioSummaryView(ValuationAPIView):
    """Summarize existing scenario outcomes without blending methods into one value."""
    def get(self, request: Request, study_id: str) -> Response:
        study = self.get_study(request, study_id)
        latest_revision = Prefetch(
            "revisions",
            queryset=PortfolioScenarioRevision.objects.filter(tenant_id=study.tenant_id).order_by("-revision_number")[:1],
            to_attr="prefetched_latest_revisions",
        )
        scenarios = list(
            PortfolioScenario.objects.filter(tenant_id=study.tenant_id, study=study)
            .prefetch_related(latest_revision).order_by("name")
        )
        result = []
        current_manifest = current_asset_manifest(study)
        for scenario in scenarios:
            prefetched = getattr(scenario, "prefetched_latest_revisions", None)
            revision = prefetched[0] if prefetched else None
            if revision:
                result.append({"scenario_id": str(scenario.pk), "scenario_name": scenario.name, "method": scenario.method, "currency": revision.currency, "indicated_value": revision.calculated_result.get("indicated_value"), "revision": revision.revision_number, "input_sha256": revision.input_sha256, "asset_manifest_sha256": revision.asset_manifest_sha256, "is_current_for_current_assets": revision.asset_manifest_sha256 == current_manifest})
        return Response({"study_id": str(study.pk), "currency": study.currency, "current_asset_manifest_sha256": current_manifest, "results": result, "interpretation": "Each outcome is a separate assumption-driven scenario. Do not average these methods or treat the spread as a statistical confidence interval."})
