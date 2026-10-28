"""Tenant-authorized REST endpoints for landscape studies and analytics."""
from __future__ import annotations

from collections import Counter
from datetime import date

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.parsers import JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from landscapes.models import (
    AssigneeAlias,
    AssigneeEntity,
    LandscapeAnnotation,
    LandscapeCluster,
    LandscapeStudy,
    LandscapeStudyPublication,
    TechnologyTaxonomy,
)
from landscapes.serializers import (
    AssigneeAliasCreateSerializer,
    AssigneeAliasSerializer,
    AssigneeCreateSerializer,
    AssigneeEntitySerializer,
    LandscapeAnnotationCreateSerializer,
    LandscapeAnnotationSerializer,
    LandscapeStudyCreateSerializer,
    LandscapeStudyPublicationSerializer,
    LandscapeStudyUpdateSerializer,
    PublicationSelectionSerializer,
    TechnologyTaxonomyCreateSerializer,
    TechnologyTaxonomySerializer,
)
from landscapes.services import (
    audit_landscape_event,
    create_study_publication_snapshot,
    normalize_assignee_name,
)
from landscapes.tasks import build_landscape
from patents.intelligence_models import PatentPublication
from tenants.models import TenantMembership
from tenants.permissions import HasTenantAccess


_EDITOR_ROLES = frozenset({
    TenantMembership.Role.OWNER,
    TenantMembership.Role.ADMIN,
    TenantMembership.Role.ATTORNEY,
    TenantMembership.Role.ANALYST,
})
_REVIEW_ROLES = frozenset({
    TenantMembership.Role.OWNER,
    TenantMembership.Role.ADMIN,
    TenantMembership.Role.ATTORNEY,
    TenantMembership.Role.REVIEWER,
})
_MAX_STUDY_PUBLICATIONS = 500


class LandscapeAPIView(APIView):
    permission_classes = [IsAuthenticated, HasTenantAccess]
    parser_classes = [JSONParser]

    @staticmethod
    def tenant(request: Request):
        return request.tenant

    @staticmethod
    def require_role(request: Request, roles: frozenset[str], message: str = "Your tenant role cannot perform this operation.") -> None:
        membership = request.tenant_membership
        if membership.role not in roles:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied(message)

    def get_study(self, request: Request, study_id: str) -> LandscapeStudy:
        study = LandscapeStudy.objects.for_tenant(request.tenant.pk).filter(pk=study_id).first()
        if study is None:
            raise NotFound("Landscape study was not found in this tenant.")
        return study


def _study_payload(study: LandscapeStudy) -> dict:
    return {
        "id": str(study.pk),
        "title": study.title,
        "description": study.description,
        "scope_filters": study.scope_filters,
        "analysis_parameters": study.analysis_parameters,
        "status": study.status,
        "algorithm_version": study.algorithm_version,
        "error_code": study.error_code,
        "publication_count": LandscapeStudyPublication.objects.filter(tenant_id=study.tenant_id, study=study).count(),
        "cluster_count": LandscapeCluster.objects.filter(tenant_id=study.tenant_id, study=study).count(),
        "created_by": study.created_by.get_username(),
        "started_at": study.started_at,
        "completed_at": study.completed_at,
        "created_at": study.created_at,
        "updated_at": study.updated_at,
    }


def _publication_payload(publication: PatentPublication) -> dict:
    return {
        "id": str(publication.pk),
        "provider": publication.provider,
        "provider_record_id": publication.provider_record_id,
        "publication_number": publication.publication_number or "",
        "application_number": publication.application_number or "",
        "jurisdiction": publication.jurisdiction,
        "title": publication.title,
        "abstract": publication.abstract or "",
        "filing_date": publication.filing_date,
        "publication_date": publication.publication_date,
        "grant_date": publication.grant_date,
        "legal_status": publication.legal_status,
        "applicant_names": publication.applicant_names if isinstance(publication.applicant_names, list) else [],
        "ipc_codes": publication.ipc_codes if isinstance(publication.ipc_codes, list) else [],
        "cpc_codes": publication.cpc_codes if isinstance(publication.cpc_codes, list) else [],
        "family_system": publication.family_system,
        "family_identifier": publication.family_identifier,
        "source_url": publication.source_url,
    }


class PublicationSearchView(LandscapeAPIView):
    """Search only the locally synchronized canonical public-record table."""

    def get(self, request: Request) -> Response:
        query = str(request.query_params.get("q", "")).strip()
        if len(query) > 200:
            raise ValidationError({"q": "Search text must be 200 characters or fewer."})
        queryset = PatentPublication.objects.all()
        if query:
            queryset = queryset.filter(
                Q(title__icontains=query)
                | Q(abstract__icontains=query)
                | Q(publication_number__icontains=query)
                | Q(application_number__icontains=query)
            )
        else:
            queryset = queryset.order_by("-last_seen_at")
        jurisdiction = str(request.query_params.get("jurisdiction", "")).strip().upper()
        if jurisdiction:
            if len(jurisdiction) > 10:
                raise ValidationError({"jurisdiction": "Jurisdiction code is too long."})
            queryset = queryset.filter(jurisdiction=jurisdiction)
        filing_from = request.query_params.get("filing_from")
        filing_to = request.query_params.get("filing_to")
        try:
            if filing_from:
                queryset = queryset.filter(filing_date__gte=date.fromisoformat(filing_from))
            if filing_to:
                queryset = queryset.filter(filing_date__lte=date.fromisoformat(filing_to))
        except (TypeError, ValueError):
            raise ValidationError({"filing_date": "Use ISO dates such as 2024-01-31."})
        queryset = queryset.order_by("-publication_date", "publication_number", "pk")
        total = queryset.count()
        try:
            limit = max(1, min(100, int(request.query_params.get("limit", "25"))))
        except ValueError:
            raise ValidationError({"limit": "Limit must be an integer between 1 and 100."})
        rows = queryset[:limit]
        return Response({"count": total, "limit": limit, "results": [_publication_payload(row) for row in rows]})


class LandscapeStudyListCreateView(LandscapeAPIView):
    def get(self, request: Request) -> Response:
        rows = LandscapeStudy.objects.for_tenant(self.tenant(request).pk).select_related("created_by")[:200]
        return Response({"count": LandscapeStudy.objects.for_tenant(self.tenant(request).pk).count(), "results": [_study_payload(row) for row in rows]})

    def post(self, request: Request) -> Response:
        tenant = self.tenant(request)
        self.require_role(request, _EDITOR_ROLES)
        serializer = LandscapeStudyCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        study = serializer.save(tenant=tenant, created_by=request.user)
        audit_landscape_event(tenant_id=tenant.pk, actor_id=request.user.pk, action="LANDSCAPE_STUDY_CREATED", resource_type="LandscapeStudy", resource_id=study.pk, metadata={"title_length": len(study.title)})
        return Response(_study_payload(study), status=status.HTTP_201_CREATED)


class LandscapeStudyDetailView(LandscapeAPIView):
    def get(self, request: Request, study_id: str) -> Response:
        return Response(_study_payload(self.get_study(request, study_id)))

    def patch(self, request: Request, study_id: str) -> Response:
        self.require_role(request, _EDITOR_ROLES)
        study = self.get_study(request, study_id)
        if study.status == LandscapeStudy.Status.ARCHIVED:
            raise ValidationError("Archived landscape studies cannot be edited.")
        serializer = LandscapeStudyUpdateSerializer(study, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        changed = sorted(serializer.validated_data.keys())
        # Title, description and saved scope metadata do not mutate the pinned
        # publication set. Preserve the READY result; adding/removing publications
        # explicitly invalidates computed projections through the selection API.
        serializer.save()
        audit_landscape_event(tenant_id=study.tenant_id, actor_id=request.user.pk, action="LANDSCAPE_STUDY_UPDATED", resource_type="LandscapeStudy", resource_id=study.pk, metadata={"fields": changed})
        return Response(_study_payload(study))


class LandscapeStudyPublicationsView(LandscapeAPIView):
    def get(self, request: Request, study_id: str) -> Response:
        study = self.get_study(request, study_id)
        queryset = LandscapeStudyPublication.objects.filter(tenant_id=study.tenant_id, study=study).select_related("cluster").order_by("filing_date", "publication_number")[:1000]
        return Response({"count": LandscapeStudyPublication.objects.filter(tenant_id=study.tenant_id, study=study).count(), "results": LandscapeStudyPublicationSerializer(queryset, many=True).data})

    def post(self, request: Request, study_id: str) -> Response:
        self.require_role(request, _EDITOR_ROLES)
        study = self.get_study(request, study_id)
        if study.status == LandscapeStudy.Status.ARCHIVED:
            raise ValidationError("Archived studies cannot accept new publications.")
        serializer = PublicationSelectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        ids = serializer.validated_data["publication_ids"]
        if len(ids) > _MAX_STUDY_PUBLICATIONS:
            raise ValidationError({"publication_ids": f"A single study may contain at most {_MAX_STUDY_PUBLICATIONS} publications."})
        publications = list(PatentPublication.objects.filter(pk__in=ids).order_by("pk"))
        if len(publications) != len(ids):
            raise ValidationError({"publication_ids": "One or more IDs do not match synchronized canonical publications."})
        created_count = 0
        with transaction.atomic():
            locked_study = LandscapeStudy.objects.select_for_update().get(pk=study.pk, tenant_id=study.tenant_id)
            for publication in publications:
                _row, created = create_study_publication_snapshot(locked_study, publication)
                created_count += int(created)
            if created_count:
                LandscapeStudyPublication.objects.filter(tenant_id=study.tenant_id, study=study).update(
                    cluster=None, projection_x=None, projection_y=None, projection_method=""
                )
                LandscapeCluster.objects.filter(tenant_id=study.tenant_id, study=study).delete()
                locked_study.status = LandscapeStudy.Status.DRAFT
                locked_study.error_code = ""
                locked_study.completed_at = None
                locked_study.save(update_fields=("status", "error_code", "completed_at", "updated_at"))
        audit_landscape_event(tenant_id=study.tenant_id, actor_id=request.user.pk, action="LANDSCAPE_PUBLICATIONS_ADDED", resource_type="LandscapeStudy", resource_id=study.pk, metadata={"requested_count": len(ids), "created_count": created_count})
        return Response({"study_id": str(study.pk), "requested_count": len(ids), "created_count": created_count, "publication_count": LandscapeStudyPublication.objects.filter(tenant_id=study.tenant_id, study=study).count(), "status": LandscapeStudy.Status.DRAFT if created_count else study.status}, status=status.HTTP_201_CREATED)


class LandscapeBuildView(LandscapeAPIView):
    def post(self, request: Request, study_id: str) -> Response:
        self.require_role(request, _EDITOR_ROLES)
        study = self.get_study(request, study_id)
        if study.status == LandscapeStudy.Status.ARCHIVED:
            raise ValidationError("Archived studies cannot be built.")
        count = LandscapeStudyPublication.objects.filter(tenant_id=study.tenant_id, study=study).count()
        if not count:
            raise ValidationError({"study": "Add at least one synchronized publication before building the landscape."})
        force = request.data.get("force", False) if isinstance(request.data, dict) else False
        if not isinstance(force, bool):
            raise ValidationError({"force": "Force must be a boolean."})
        if study.status == LandscapeStudy.Status.BUILDING:
            return Response({"study_id": str(study.pk), "status": "BUILDING", "detail": "A build is already in progress."}, status=status.HTTP_202_ACCEPTED)
        from rest_framework.exceptions import APIException
        with transaction.atomic():
            locked_study = LandscapeStudy.objects.select_for_update().get(pk=study.pk, tenant_id=study.tenant_id)
            if locked_study.status == LandscapeStudy.Status.BUILDING:
                return Response({"study_id": str(locked_study.pk), "status": "BUILDING", "detail": "A build is already in progress."}, status=status.HTTP_202_ACCEPTED)
            previous_status = locked_study.status
            locked_study.status = LandscapeStudy.Status.BUILDING
            locked_study.error_code = ""
            locked_study.started_at = timezone.now()
            locked_study.completed_at = None
            locked_study.save(update_fields=("status", "error_code", "started_at", "completed_at", "updated_at"))
            try:
                result = build_landscape.apply_async(args=[str(locked_study.pk), str(locked_study.tenant_id)], kwargs={"force": force}, queue="embedding")
            except Exception as exc:
                locked_study.status = previous_status if previous_status != LandscapeStudy.Status.BUILDING else LandscapeStudy.Status.DRAFT
                locked_study.error_code = "TASK_QUEUE_FAILED"
                locked_study.save(update_fields=("status", "error_code", "updated_at"))
                raise APIException("The landscape build task could not be queued. Check the embedding worker and queue configuration.") from exc
            locked_study.built_by_task_id = result.id
            locked_study.save(update_fields=("built_by_task_id", "updated_at"))
        audit_landscape_event(tenant_id=study.tenant_id, actor_id=request.user.pk, action="LANDSCAPE_BUILD_QUEUED", resource_type="LandscapeStudy", resource_id=study.pk, metadata={"publication_count": count})
        return Response({"study_id": str(study.pk), "status": "QUEUED", "task_id": result.id, "publication_count": count}, status=status.HTTP_202_ACCEPTED)


class LandscapeAnalyticsView(LandscapeAPIView):
    def get(self, request: Request, study_id: str) -> Response:
        study = self.get_study(request, study_id)
        selected_count = LandscapeStudyPublication.objects.filter(tenant_id=study.tenant_id, study=study).count()
        rows = list(LandscapeStudyPublication.objects.filter(tenant_id=study.tenant_id, study=study).select_related("cluster").prefetch_related("assignee_links__entity", "technology_links__taxonomy").order_by("pk")[:1000])
        timeline: Counter[str] = Counter()
        geography: Counter[str] = Counter()
        family_keys: set[tuple[str, str]] = set()
        family_groups: dict[tuple[str, str], dict[str, object]] = {}
        assignee_counts: Counter[str] = Counter()
        technology_counts: Counter[tuple[str, str, str]] = Counter()
        points = []
        for row in rows:
            year = str(row.filing_date.year) if row.filing_date else "undated"
            timeline[year] += 1
            jurisdiction = row.jurisdiction or "Unknown"
            geography[jurisdiction] += 1
            if row.family_system and row.family_identifier:
                family_key = (row.family_system, row.family_identifier)
                family_keys.add(family_key)
                family_group = family_groups.setdefault(family_key, {
                    "family_system": row.family_system,
                    "family_identifier": row.family_identifier,
                    "publication_count": 0,
                    "publication_numbers": [],
                    "jurisdictions": set(),
                    "filing_years": set(),
                })
                family_group["publication_count"] = int(family_group["publication_count"]) + 1
                family_group["jurisdictions"].add(jurisdiction)
                if row.filing_date:
                    family_group["filing_years"].add(row.filing_date.year)
                if len(family_group["publication_numbers"]) < 10 and row.publication_number:
                    family_group["publication_numbers"].append(row.publication_number)
            for link in row.assignee_links.all():
                assignee_counts[link.entity.canonical_name] += 1
            for link in row.technology_links.all():
                technology_counts[(link.taxonomy.source, link.taxonomy.code, link.taxonomy.label)] += 1
            points.append({
                "id": str(row.pk),
                "publication_id": str(row.publication_id),
                "publication_number": row.publication_number,
                "title": row.title,
                "jurisdiction": jurisdiction,
                "filing_year": year,
                "assignees": row.normalized_assignee_names,
                "cluster_number": row.cluster.cluster_number if row.cluster_id else None,
                "cluster_label": row.cluster.label if row.cluster_id else "Unclustered",
                "x": row.projection_x,
                "y": row.projection_y,
                "source_url": row.source_url,
                "snapshot_sha256": row.snapshot_sha256,
            })
        cluster_rows = LandscapeCluster.objects.filter(tenant_id=study.tenant_id, study=study).order_by("cluster_number")
        return Response({
            "study": _study_payload(study),
            "coverage": {
                "publication_count": selected_count,
                "analyzed_publication_count": len(rows),
                "analytics_truncated": selected_count > len(rows),
                "family_count": len(family_keys),
                "jurisdiction_count": len(geography),
                "assignee_count": len(assignee_counts),
                "technology_code_count": len(technology_counts),
            },
            "filing_timeline": [{"year": year, "count": count} for year, count in sorted(timeline.items(), key=lambda item: (item[0] == "undated", item[0]))],
            "family_aggregation": [
                {
                    "family_system": item["family_system"],
                    "family_identifier": item["family_identifier"],
                    "publication_count": item["publication_count"],
                    "publication_numbers": item["publication_numbers"],
                    "jurisdictions": sorted(item["jurisdictions"]),
                    "filing_years": sorted(item["filing_years"]),
                }
                for item in sorted(family_groups.values(), key=lambda item: (-int(item["publication_count"]), str(item["family_system"]), str(item["family_identifier"])))[:30]
            ],
            "geographic_coverage": [{"jurisdiction": name, "count": count} for name, count in geography.most_common()],
            "top_assignees": [{"name": name, "count": count} for name, count in assignee_counts.most_common(20)],
            "technology_distribution": [{"source": source, "code": code, "label": label, "count": count} for (source, code, label), count in technology_counts.most_common(30)],
            "clusters": [{"id": str(cluster.pk), "cluster_number": cluster.cluster_number, "label": cluster.label, "explanation": cluster.explanation, "top_terms": cluster.top_terms, "top_classification_codes": cluster.top_classification_codes, "publication_count": cluster.publication_count, "algorithm": cluster.algorithm} for cluster in cluster_rows],
            "points": points,
        })


class AssigneeListCreateView(LandscapeAPIView):
    def get(self, request: Request) -> Response:
        queryset = AssigneeEntity.objects.for_tenant(self.tenant(request).pk).prefetch_related("aliases")
        query = str(request.query_params.get("q", "")).strip()
        if query:
            queryset = queryset.filter(Q(canonical_name__icontains=query) | Q(normalized_key__icontains=normalize_assignee_name(query)))
        rows = queryset.order_by("canonical_name")[:200]
        return Response({"count": queryset.count(), "results": AssigneeEntitySerializer(rows, many=True).data})

    def post(self, request: Request) -> Response:
        self.require_role(request, _EDITOR_ROLES)
        serializer = AssigneeCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        tenant = self.tenant(request)
        name = serializer.validated_data["canonical_name"]
        key = normalize_assignee_name(name)
        entity, created = AssigneeEntity.objects.get_or_create(
            tenant=tenant,
            normalized_key=key,
            defaults={"canonical_name": name, "country_code": serializer.validated_data.get("country_code", "").upper(), "notes": serializer.validated_data.get("notes", ""), "status": AssigneeEntity.Status.CONFIRMED, "created_by": request.user, "reviewed_by": request.user, "reviewed_at": timezone.now()},
        )
        if not created:
            raise ValidationError({"canonical_name": "An assignee with this normalized name already exists in the tenant."})
        AssigneeAlias.objects.get_or_create(tenant=tenant, normalized_key=key, defaults={"entity": entity, "original_name": name, "source": AssigneeAlias.Source.ATTORNEY, "created_by": request.user})
        audit_landscape_event(tenant_id=tenant.pk, actor_id=request.user.pk, action="LANDSCAPE_ASSIGNEE_ENTITY_CREATED", resource_type="AssigneeEntity", resource_id=entity.pk, metadata={"name_length": len(name)})
        return Response(AssigneeEntitySerializer(entity).data, status=status.HTTP_201_CREATED)


class AssigneeEntityDetailView(LandscapeAPIView):
    def patch(self, request: Request, assignee_id: str) -> Response:
        tenant = self.tenant(request)
        entity = AssigneeEntity.objects.for_tenant(tenant.pk).filter(pk=assignee_id).first()
        if entity is None:
            raise NotFound("Assignee entity was not found in this tenant.")
        values = request.data if isinstance(request.data, dict) else {}
        allowed = {"status", "country_code", "notes"}
        if set(values) - allowed:
            raise ValidationError({"detail": "Only status, country_code and notes may be updated."})
        if {"country_code", "notes"} & set(values):
            self.require_role(request, _EDITOR_ROLES, "Only owner/admin/attorney/analyst roles can edit assignee metadata.")
        if "status" in values:
            self.require_role(request, _REVIEW_ROLES, "Only attorney/reviewer roles can confirm assignee identity decisions.")
            if values["status"] not in AssigneeEntity.Status.values:
                raise ValidationError({"status": "Status must be UNREVIEWED, CONFIRMED or DO_NOT_MERGE."})
            entity.status = values["status"]
            entity.reviewed_by = request.user if entity.status != AssigneeEntity.Status.UNREVIEWED else None
            entity.reviewed_at = timezone.now() if entity.status != AssigneeEntity.Status.UNREVIEWED else None
        if "country_code" in values:
            country_code = str(values["country_code"]).strip().upper()
            if country_code and (len(country_code) != 2 or not country_code.isalpha()):
                raise ValidationError({"country_code": "Use a two-letter country code or an empty value."})
            entity.country_code = country_code
        if "notes" in values:
            notes = str(values["notes"])
            if len(notes) > 5000:
                raise ValidationError({"notes": "Notes must be 5,000 characters or fewer."})
            entity.notes = notes
        entity.save(update_fields=("status", "reviewed_by", "reviewed_at", "country_code", "notes", "updated_at"))
        audit_landscape_event(tenant_id=tenant.pk, actor_id=request.user.pk, action="LANDSCAPE_ASSIGNEE_ENTITY_REVIEWED", resource_type="AssigneeEntity", resource_id=entity.pk, metadata={"status": entity.status})
        return Response(AssigneeEntitySerializer(entity).data)


class AssigneeAliasCreateView(LandscapeAPIView):
    def post(self, request: Request, assignee_id: str) -> Response:
        self.require_role(request, _EDITOR_ROLES)
        tenant = self.tenant(request)
        entity = AssigneeEntity.objects.for_tenant(tenant.pk).filter(pk=assignee_id).first()
        if entity is None:
            raise NotFound("Assignee entity was not found in this tenant.")
        serializer = AssigneeAliasCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        name = serializer.validated_data["original_name"]
        key = normalize_assignee_name(name)
        if entity.status == AssigneeEntity.Status.DO_NOT_MERGE and key != entity.normalized_key:
            raise ValidationError({"original_name": "This entity is marked Do not merge; no additional aliases can be attached."})
        if AssigneeEntity.objects.for_tenant(tenant.pk).filter(normalized_key=key).exclude(pk=entity.pk).exists():
            raise ValidationError({"original_name": "This normalized key belongs to another entity. Review the entities before attempting a merge."})
        existing = AssigneeAlias.objects.for_tenant(tenant.pk).filter(normalized_key=key).select_related("entity").first()
        if existing:
            if existing.entity_id != entity.pk:
                return Response({"detail": "This alias is already linked to a different entity; no automatic merge was performed."}, status=status.HTTP_409_CONFLICT)
            return Response(AssigneeAliasSerializer(existing).data)
        alias = AssigneeAlias.objects.create(tenant=tenant, entity=entity, original_name=name, normalized_key=key, source=AssigneeAlias.Source.ATTORNEY, created_by=request.user)
        audit_landscape_event(tenant_id=tenant.pk, actor_id=request.user.pk, action="LANDSCAPE_ASSIGNEE_ALIAS_CREATED", resource_type="AssigneeAlias", resource_id=alias.pk, metadata={"name_length": len(name)})
        return Response(AssigneeAliasSerializer(alias).data, status=status.HTTP_201_CREATED)


class TechnologyTaxonomyListCreateView(LandscapeAPIView):
    def get(self, request: Request) -> Response:
        queryset = TechnologyTaxonomy.objects.for_tenant(self.tenant(request).pk).select_related("parent").filter(is_active=True)
        source = str(request.query_params.get("source", "")).upper()
        if source in {choice for choice, _label in TechnologyTaxonomy.Source.choices}:
            queryset = queryset.filter(source=source)
        query = str(request.query_params.get("q", "")).strip()
        if query:
            queryset = queryset.filter(Q(code__icontains=query) | Q(label__icontains=query))
        return Response({"count": queryset.count(), "results": TechnologyTaxonomySerializer(queryset.order_by("source", "code")[:300], many=True).data})

    def post(self, request: Request) -> Response:
        self.require_role(request, _EDITOR_ROLES)
        tenant = self.tenant(request)
        serializer = TechnologyTaxonomyCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        parent = None
        if values.get("parent_id"):
            parent = TechnologyTaxonomy.objects.for_tenant(tenant.pk).filter(pk=values["parent_id"]).first()
            if parent is None:
                raise ValidationError({"parent_id": "Parent taxonomy node must belong to this tenant."})
        try:
            node = TechnologyTaxonomy.objects.create(tenant=tenant, source=TechnologyTaxonomy.Source.CUSTOM, code=values["code"], label=values["label"], description=values.get("description", ""), parent=parent)
        except IntegrityError as exc:
            raise ValidationError({"code": "A custom taxonomy node with this code already exists."}) from exc
        audit_landscape_event(tenant_id=tenant.pk, actor_id=request.user.pk, action="LANDSCAPE_TAXONOMY_NODE_CREATED", resource_type="TechnologyTaxonomy", resource_id=node.pk, metadata={"source": node.source})
        return Response(TechnologyTaxonomySerializer(node).data, status=status.HTTP_201_CREATED)


class LandscapeAnnotationListCreateView(LandscapeAPIView):
    def get(self, request: Request, study_id: str) -> Response:
        study = self.get_study(request, study_id)
        rows = LandscapeAnnotation.objects.filter(tenant_id=study.tenant_id, study=study).select_related("created_by", "resolved_by", "study_publication").order_by("-updated_at")[:500]
        return Response({"count": LandscapeAnnotation.objects.filter(tenant_id=study.tenant_id, study=study).count(), "results": LandscapeAnnotationSerializer(rows, many=True).data})

    def post(self, request: Request, study_id: str) -> Response:
        self.require_role(request, _EDITOR_ROLES)
        study = self.get_study(request, study_id)
        if study.status == LandscapeStudy.Status.ARCHIVED:
            raise ValidationError("Archived studies cannot receive new annotations.")
        serializer = LandscapeAnnotationCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        publication = None
        publication_id = values.get("study_publication_id")
        if publication_id:
            publication = LandscapeStudyPublication.objects.filter(tenant_id=study.tenant_id, study=study, pk=publication_id).first()
            if publication is None:
                raise ValidationError({"study_publication_id": "Annotation target must be a publication in this study."})
        annotation = LandscapeAnnotation.objects.create(tenant_id=study.tenant_id, study=study, study_publication=publication, kind=values["kind"], body=values["body"], created_by=request.user)
        audit_landscape_event(tenant_id=study.tenant_id, actor_id=request.user.pk, action="LANDSCAPE_ANNOTATION_CREATED", resource_type="LandscapeAnnotation", resource_id=annotation.pk, metadata={"kind": annotation.kind})
        return Response(LandscapeAnnotationSerializer(annotation).data, status=status.HTTP_201_CREATED)


class LandscapeAnnotationDetailView(LandscapeAPIView):
    def patch(self, request: Request, annotation_id: str) -> Response:
        tenant = self.tenant(request)
        annotation = LandscapeAnnotation.objects.filter(tenant_id=tenant.pk, pk=annotation_id).select_related("study", "study_publication").first()
        if annotation is None:
            raise NotFound("Landscape annotation was not found in this tenant.")
        values = request.data if isinstance(request.data, dict) else {}
        allowed = {"body", "status"}
        if set(values) - allowed:
            raise ValidationError({"detail": "Only body and status may be updated."})
        if "body" in values:
            self.require_role(request, _EDITOR_ROLES)
            body = str(values["body"]).strip()
            if not body or len(body) > 12000:
                raise ValidationError({"body": "Annotation body must contain 1 to 12,000 characters."})
            annotation.body = body
        if "status" in values:
            self.require_role(request, _REVIEW_ROLES)
            if values["status"] not in LandscapeAnnotation.Status.values:
                raise ValidationError({"status": "Status must be OPEN or RESOLVED."})
            annotation.status = values["status"]
            annotation.resolved_by = request.user if annotation.status == LandscapeAnnotation.Status.RESOLVED else None
            annotation.resolved_at = timezone.now() if annotation.status == LandscapeAnnotation.Status.RESOLVED else None
        annotation.save(update_fields=("body", "status", "resolved_by", "resolved_at", "updated_at"))
        audit_landscape_event(tenant_id=tenant.pk, actor_id=request.user.pk, action="LANDSCAPE_ANNOTATION_UPDATED", resource_type="LandscapeAnnotation", resource_id=annotation.pk, metadata={"status": annotation.status})
        return Response(LandscapeAnnotationSerializer(annotation).data)
