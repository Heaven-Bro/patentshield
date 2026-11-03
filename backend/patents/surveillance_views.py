"""Tenant-authorized API for watchlists, notifications, and alert lifecycle."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from patents.surveillance_models import (
    AlertDelivery,
    AlertDestination,
    AlertSubscription,
    SurveillanceAlert,
    SurveillanceWatchRule,
    SurveillanceWatchlist,
)
from patents.surveillance_serializers import (
    AlertDeliverySerializer,
    AlertDestinationReadSerializer,
    AlertDestinationWriteSerializer,
    AlertSubscriptionSerializer,
    SurveillanceAlertSerializer,
    SurveillanceWatchlistSerializer,
    SurveillanceWatchRuleSerializer,
)
from patents.surveillance_services import (
    SurveillanceConfigurationError,
    encrypt_webhook_secret,
    record_surveillance_audit,
)
from tenants.models import TenantMembership
from tenants.permissions import HasTenantAccess, RequireTenantRole

logger = logging.getLogger(__name__)

READ_ROLES = (
    TenantMembership.Role.OWNER,
    TenantMembership.Role.ADMIN,
    TenantMembership.Role.ATTORNEY,
    TenantMembership.Role.ANALYST,
    TenantMembership.Role.REVIEWER,
    TenantMembership.Role.READ_ONLY,
)
WRITE_ROLES = (
    TenantMembership.Role.OWNER,
    TenantMembership.Role.ADMIN,
    TenantMembership.Role.ATTORNEY,
    TenantMembership.Role.ANALYST,
)
REVIEW_ROLES = WRITE_ROLES + (TenantMembership.Role.REVIEWER,)


class SurveillanceConfigurationUnavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "Surveillance notification configuration is unavailable."
    default_code = "SURVEILLANCE_CONFIGURATION_UNAVAILABLE"


def _membership(request: Request):
    return getattr(request, "tenant_membership", None)


def _require_role(request: Request, allowed_roles: tuple[str, ...]) -> None:
    membership = _membership(request)
    if membership is None or membership.role not in allowed_roles:
        raise PermissionDenied("Your tenant role does not permit this surveillance operation.")


def _limit(request: Request, default: int = 50, maximum: int = 100) -> int:
    raw = request.query_params.get("limit", default)
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError({"limit": "limit must be an integer."}) from exc
    return max(1, min(maximum, parsed))


def _audit(request: Request, *, action: str, resource_type: str, resource_id, metadata=None) -> None:
    record_surveillance_audit(
        tenant=request.tenant,
        actor=request.user,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        metadata=metadata or {},
    )


def _base_queryset(request: Request):
    return SurveillanceWatchlist.objects.for_tenant(request.tenant).select_related(
        "tenant", "created_by", "updated_by"
    )


class SurveillanceWatchlistListCreateView(APIView):
    permission_classes = [IsAuthenticated, HasTenantAccess, RequireTenantRole(*READ_ROLES)]

    def get(self, request: Request) -> Response:
        items = _base_queryset(request).order_by("name", "created_at")[:_limit(request)]
        return Response({"results": SurveillanceWatchlistSerializer(items, many=True).data})

    def post(self, request: Request) -> Response:
        _require_role(request, WRITE_ROLES)
        serializer = SurveillanceWatchlistSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        now = timezone.now()
        try:
            with transaction.atomic():
                watchlist = serializer.save(
                    tenant=request.tenant,
                    created_by=request.user,
                    updated_by=request.user,
                    next_poll_at=now,
                )
                _audit(
                    request,
                    action="SURVEILLANCE_WATCHLIST_CREATED",
                    resource_type="SurveillanceWatchlist",
                    resource_id=watchlist.pk,
                    metadata={"watchlist_id": str(watchlist.pk)},
                )
        except IntegrityError as exc:
            raise ValidationError({"name": "A watchlist with this name already exists in this tenant."}) from exc
        return Response(
            SurveillanceWatchlistSerializer(watchlist).data,
            status=status.HTTP_201_CREATED,
        )


class SurveillanceWatchlistDetailView(APIView):
    permission_classes = [IsAuthenticated, HasTenantAccess, RequireTenantRole(*READ_ROLES)]

    def _get(self, request: Request, watchlist_id: str) -> SurveillanceWatchlist:
        return get_object_or_404(_base_queryset(request), pk=watchlist_id)

    def get(self, request: Request, watchlist_id: str) -> Response:
        watchlist = self._get(request, watchlist_id)
        return Response(SurveillanceWatchlistSerializer(watchlist).data)

    def patch(self, request: Request, watchlist_id: str) -> Response:
        _require_role(request, WRITE_ROLES)
        watchlist = self._get(request, watchlist_id)
        serializer = SurveillanceWatchlistSerializer(
            watchlist, data=request.data, partial=True
        )
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                updated = serializer.save(updated_by=request.user)
                if not updated.is_active:
                    updated.next_poll_at = None
                    updated.save(update_fields=("next_poll_at", "updated_at"))
                elif updated.next_poll_at is None:
                    updated.next_poll_at = timezone.now()
                    updated.save(update_fields=("next_poll_at", "updated_at"))
                _audit(
                    request,
                    action="SURVEILLANCE_WATCHLIST_UPDATED",
                    resource_type="SurveillanceWatchlist",
                    resource_id=updated.pk,
                    metadata={"changed_fields": sorted(request.data.keys())[:20]},
                )
        except IntegrityError as exc:
            raise ValidationError({"name": "A watchlist with this name already exists in this tenant."}) from exc
        return Response(SurveillanceWatchlistSerializer(updated).data)

    def delete(self, request: Request, watchlist_id: str) -> Response:
        _require_role(request, WRITE_ROLES)
        watchlist = self._get(request, watchlist_id)
        # A watchlist owns audit-worthy alert and observation history. Never let
        # a routine DELETE cascade that history; deactivate historical watchlists.
        if watchlist.alerts.exists() or watchlist.observations.exists():
            raise ValidationError({
                "detail": "This watchlist has surveillance history. Set is_active=false to preserve alerts and evidence."
            })
        with transaction.atomic():
            _audit(
                request,
                action="SURVEILLANCE_WATCHLIST_DELETED",
                resource_type="SurveillanceWatchlist",
                resource_id=watchlist.pk,
                metadata={"watchlist_id": str(watchlist.pk)},
            )
            watchlist.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SurveillanceWatchRuleListCreateView(APIView):
    permission_classes = [IsAuthenticated, HasTenantAccess, RequireTenantRole(*READ_ROLES)]

    def _watchlist(self, request: Request, watchlist_id: str) -> SurveillanceWatchlist:
        return get_object_or_404(_base_queryset(request), pk=watchlist_id)

    def get(self, request: Request, watchlist_id: str) -> Response:
        watchlist = self._watchlist(request, watchlist_id)
        rules = SurveillanceWatchRule.objects.for_tenant(request.tenant).filter(
            watchlist=watchlist
        ).order_by("created_at")[:_limit(request)]
        return Response({"results": SurveillanceWatchRuleSerializer(rules, many=True).data})

    def post(self, request: Request, watchlist_id: str) -> Response:
        _require_role(request, WRITE_ROLES)
        watchlist = self._watchlist(request, watchlist_id)
        serializer = SurveillanceWatchRuleSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                rule = serializer.save(tenant=request.tenant, watchlist=watchlist)
                if watchlist.is_active:
                    watchlist.next_poll_at = timezone.now()
                    watchlist.save(update_fields=("next_poll_at", "updated_at"))
                _audit(
                    request,
                    action="SURVEILLANCE_RULE_CREATED",
                    resource_type="SurveillanceWatchRule",
                    resource_id=rule.pk,
                    metadata={"watchlist_id": str(watchlist.pk), "rule_type": rule.rule_type},
                )
        except IntegrityError as exc:
            raise ValidationError({"value": "This rule already exists in the watchlist."}) from exc
        return Response(SurveillanceWatchRuleSerializer(rule).data, status=status.HTTP_201_CREATED)


class SurveillanceWatchRuleDetailView(APIView):
    permission_classes = [IsAuthenticated, HasTenantAccess, RequireTenantRole(*READ_ROLES)]

    def _get(self, request: Request, rule_id: str) -> SurveillanceWatchRule:
        return get_object_or_404(
            SurveillanceWatchRule.objects.for_tenant(request.tenant).select_related("watchlist"),
            pk=rule_id,
        )

    def patch(self, request: Request, rule_id: str) -> Response:
        _require_role(request, WRITE_ROLES)
        rule = self._get(request, rule_id)
        serializer = SurveillanceWatchRuleSerializer(rule, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                updated = serializer.save()
                SurveillanceWatchlist.objects.filter(
                    tenant_id=request.tenant.pk, pk=updated.watchlist_id, is_active=True
                ).update(next_poll_at=timezone.now())
                _audit(
                    request,
                    action="SURVEILLANCE_RULE_UPDATED",
                    resource_type="SurveillanceWatchRule",
                    resource_id=updated.pk,
                    metadata={"watchlist_id": str(updated.watchlist_id), "changed_fields": sorted(request.data.keys())[:20]},
                )
        except IntegrityError as exc:
            raise ValidationError({"value": "This rule already exists in the watchlist."}) from exc
        return Response(SurveillanceWatchRuleSerializer(updated).data)

    def delete(self, request: Request, rule_id: str) -> Response:
        _require_role(request, WRITE_ROLES)
        rule = self._get(request, rule_id)
        if rule.observations.exists() or rule.alerts.exists():
            raise ValidationError({
                "detail": "This rule has monitoring history. Set is_active=false to preserve its observations and alerts."
            })
        with transaction.atomic():
            _audit(
                request,
                action="SURVEILLANCE_RULE_DELETED",
                resource_type="SurveillanceWatchRule",
                resource_id=rule.pk,
                metadata={"watchlist_id": str(rule.watchlist_id), "rule_type": rule.rule_type},
            )
            rule.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AlertDestinationListCreateView(APIView):
    permission_classes = [IsAuthenticated, HasTenantAccess, RequireTenantRole(*READ_ROLES)]

    def get(self, request: Request) -> Response:
        destinations = AlertDestination.objects.for_tenant(request.tenant).order_by("name")[:_limit(request)]
        return Response({"results": AlertDestinationReadSerializer(destinations, many=True).data})

    def post(self, request: Request) -> Response:
        _require_role(request, WRITE_ROLES)
        serializer = AlertDestinationWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = dict(serializer.validated_data)
        webhook_url = values.pop("webhook_url", "")
        ciphertext = ""
        if values["channel"] != AlertDestination.Channel.EMAIL:
            try:
                ciphertext = encrypt_webhook_secret(webhook_url)
            except SurveillanceConfigurationError as exc:
                raise SurveillanceConfigurationUnavailable() from exc
        try:
            with transaction.atomic():
                destination = AlertDestination.objects.create(
                    tenant=request.tenant,
                    created_by=request.user,
                    webhook_secret_ciphertext=ciphertext,
                    **values,
                )
                _audit(
                    request,
                    action="SURVEILLANCE_DESTINATION_CREATED",
                    resource_type="AlertDestination",
                    resource_id=destination.pk,
                    metadata={"channel": destination.channel, "destination_id": str(destination.pk)},
                )
        except IntegrityError as exc:
            raise ValidationError({"name": "A destination with this name already exists in this tenant."}) from exc
        return Response(AlertDestinationReadSerializer(destination).data, status=status.HTTP_201_CREATED)


class AlertDestinationDetailView(APIView):
    permission_classes = [IsAuthenticated, HasTenantAccess, RequireTenantRole(*READ_ROLES)]

    def _get(self, request: Request, destination_id: str) -> AlertDestination:
        return get_object_or_404(AlertDestination.objects.for_tenant(request.tenant), pk=destination_id)

    def patch(self, request: Request, destination_id: str) -> Response:
        _require_role(request, WRITE_ROLES)
        destination = self._get(request, destination_id)
        merged = {
            "name": destination.name,
            "channel": destination.channel,
            "email_address": destination.email_address,
            "is_active": destination.is_active,
        }
        merged.update(request.data)
        serializer = AlertDestinationWriteSerializer(
            data=merged,
            context={"instance": destination},
        )
        serializer.is_valid(raise_exception=True)
        values = dict(serializer.validated_data)
        webhook_url = values.pop("webhook_url", "")
        channel_changed = values["channel"] != destination.channel
        ciphertext = destination.webhook_secret_ciphertext
        if values["channel"] == AlertDestination.Channel.EMAIL:
            ciphertext = ""
        elif webhook_url:
            try:
                ciphertext = encrypt_webhook_secret(webhook_url)
            except SurveillanceConfigurationError as exc:
                raise SurveillanceConfigurationUnavailable() from exc
        elif channel_changed:
            raise ValidationError({"webhook_url": "Provide a new webhook URL when changing webhook channels."})
        try:
            with transaction.atomic():
                for field, value in values.items():
                    setattr(destination, field, value)
                destination.webhook_secret_ciphertext = ciphertext
                destination.save()
                _audit(
                    request,
                    action="SURVEILLANCE_DESTINATION_UPDATED",
                    resource_type="AlertDestination",
                    resource_id=destination.pk,
                    metadata={"channel": destination.channel, "changed_fields": sorted(request.data.keys())[:20]},
                )
        except IntegrityError as exc:
            raise ValidationError({"name": "A destination with this name already exists in this tenant."}) from exc
        return Response(AlertDestinationReadSerializer(destination).data)

    def delete(self, request: Request, destination_id: str) -> Response:
        _require_role(request, WRITE_ROLES)
        destination = self._get(request, destination_id)
        if destination.deliveries.exists():
            raise ValidationError({"detail": "Destination has delivery history; deactivate it instead to preserve the audit trail."})
        with transaction.atomic():
            _audit(
                request,
                action="SURVEILLANCE_DESTINATION_DELETED",
                resource_type="AlertDestination",
                resource_id=destination.pk,
                metadata={"channel": destination.channel},
            )
            destination.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AlertSubscriptionListCreateView(APIView):
    permission_classes = [IsAuthenticated, HasTenantAccess, RequireTenantRole(*READ_ROLES)]

    def get(self, request: Request, watchlist_id: str) -> Response:
        watchlist = get_object_or_404(_base_queryset(request), pk=watchlist_id)
        rows = AlertSubscription.objects.for_tenant(request.tenant).filter(
            watchlist=watchlist
        ).select_related("destination").order_by("created_at")[:_limit(request)]
        return Response({"results": AlertSubscriptionSerializer(rows, many=True).data})

    def post(self, request: Request, watchlist_id: str) -> Response:
        _require_role(request, WRITE_ROLES)
        watchlist = get_object_or_404(_base_queryset(request), pk=watchlist_id)
        serializer = AlertSubscriptionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        destination_id = serializer.validated_data["destination_id"]
        destination = get_object_or_404(
            AlertDestination.objects.for_tenant(request.tenant),
            pk=destination_id,
        )
        if not destination.is_active:
            raise ValidationError({"destination_id": "Activate the destination before subscribing a watchlist to it."})
        try:
            with transaction.atomic():
                subscription = AlertSubscription.objects.create(
                    tenant=request.tenant,
                    watchlist=watchlist,
                    destination=destination,
                    alert_types=serializer.validated_data.get("alert_types", []),
                    is_active=serializer.validated_data.get("is_active", True),
                )
                _audit(
                    request,
                    action="SURVEILLANCE_SUBSCRIPTION_CREATED",
                    resource_type="AlertSubscription",
                    resource_id=subscription.pk,
                    metadata={"watchlist_id": str(watchlist.pk), "destination_id": str(destination.pk)},
                )
        except IntegrityError as exc:
            raise ValidationError({"destination_id": "This watchlist is already subscribed to this destination."}) from exc
        subscription = AlertSubscription.objects.select_related("destination").get(pk=subscription.pk)
        return Response(AlertSubscriptionSerializer(subscription).data, status=status.HTTP_201_CREATED)


class AlertSubscriptionDetailView(APIView):
    permission_classes = [IsAuthenticated, HasTenantAccess, RequireTenantRole(*READ_ROLES)]

    def delete(self, request: Request, subscription_id: str) -> Response:
        _require_role(request, WRITE_ROLES)
        row = get_object_or_404(AlertSubscription.objects.for_tenant(request.tenant), pk=subscription_id)
        with transaction.atomic():
            _audit(
                request,
                action="SURVEILLANCE_SUBSCRIPTION_DELETED",
                resource_type="AlertSubscription",
                resource_id=row.pk,
                metadata={"watchlist_id": str(row.watchlist_id), "destination_id": str(row.destination_id)},
            )
            row.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class SurveillanceAlertListView(APIView):
    permission_classes = [IsAuthenticated, HasTenantAccess, RequireTenantRole(*READ_ROLES)]

    def get(self, request: Request) -> Response:
        query = SurveillanceAlert.objects.for_tenant(request.tenant).select_related(
            "watchlist", "rule", "publication", "source_snapshot", "legal_event", "acknowledged_by"
        ).prefetch_related("deliveries")
        state_filter = request.query_params.get("state")
        if state_filter:
            if state_filter not in SurveillanceAlert.State.values:
                raise ValidationError({"state": "Unsupported alert state."})
            query = query.filter(state=state_filter)
        alert_type = request.query_params.get("alert_type")
        if alert_type:
            if alert_type not in SurveillanceAlert.AlertType.values:
                raise ValidationError({"alert_type": "Unsupported alert type."})
            query = query.filter(alert_type=alert_type)
        watchlist_id = request.query_params.get("watchlist_id")
        if watchlist_id:
            query = query.filter(watchlist_id=watchlist_id)
        return Response({"results": SurveillanceAlertSerializer(query.order_by("-created_at")[:_limit(request)], many=True).data})


class SurveillanceAlertTransitionView(APIView):
    permission_classes = [IsAuthenticated, HasTenantAccess, RequireTenantRole(*READ_ROLES)]

    def post(self, request: Request, alert_id: str, action: str) -> Response:
        _require_role(request, REVIEW_ROLES)
        if action not in {"acknowledge", "resolve"}:
            raise NotFound("Unknown alert action.")
        with transaction.atomic():
            alert = get_object_or_404(
                SurveillanceAlert.objects.for_tenant(request.tenant).select_for_update().select_related("watchlist", "publication"),
                pk=alert_id,
            )
            now = timezone.now()
            if action == "acknowledge":
                if alert.state == SurveillanceAlert.State.RESOLVED:
                    raise ValidationError({"detail": "A resolved alert cannot be acknowledged."})
                if alert.state == SurveillanceAlert.State.OPEN:
                    alert.state = SurveillanceAlert.State.ACKNOWLEDGED
                    alert.acknowledged_by = request.user
                    alert.acknowledged_at = now
                    alert.save(update_fields=("state", "acknowledged_by", "acknowledged_at", "updated_at"))
                    _audit(
                        request,
                        action="SURVEILLANCE_ALERT_ACKNOWLEDGED",
                        resource_type="SurveillanceAlert",
                        resource_id=alert.pk,
                        metadata={"alert_type": alert.alert_type, "watchlist_id": str(alert.watchlist_id)},
                    )
            else:
                if alert.state != SurveillanceAlert.State.ACKNOWLEDGED:
                    raise ValidationError({"detail": "Only acknowledged alerts can be resolved."})
                alert.state = SurveillanceAlert.State.RESOLVED
                alert.resolved_at = now
                alert.save(update_fields=("state", "resolved_at", "updated_at"))
                _audit(
                    request,
                    action="SURVEILLANCE_ALERT_RESOLVED",
                    resource_type="SurveillanceAlert",
                    resource_id=alert.pk,
                    metadata={"alert_type": alert.alert_type, "watchlist_id": str(alert.watchlist_id)},
                )
        return Response(SurveillanceAlertSerializer(alert).data)


class AlertDeliveryListView(APIView):
    permission_classes = [IsAuthenticated, HasTenantAccess, RequireTenantRole(*READ_ROLES)]

    def get(self, request: Request) -> Response:
        query = AlertDelivery.objects.for_tenant(request.tenant).select_related(
            "alert", "destination"
        ).prefetch_related("attempts")
        state_filter = request.query_params.get("status")
        if state_filter:
            if state_filter not in AlertDelivery.Status.values:
                raise ValidationError({"status": "Unsupported delivery status."})
            query = query.filter(status=state_filter)
        return Response({"results": AlertDeliverySerializer(query.order_by("-created_at")[:_limit(request)], many=True).data})


class AlertDeliveryRetryView(APIView):
    permission_classes = [IsAuthenticated, HasTenantAccess, RequireTenantRole(*READ_ROLES)]

    def post(self, request: Request, delivery_id: str) -> Response:
        _require_role(request, WRITE_ROLES)
        with transaction.atomic():
            delivery = get_object_or_404(
                AlertDelivery.objects.for_tenant(request.tenant).select_for_update().select_related("alert", "destination"),
                pk=delivery_id,
            )
            if delivery.status != AlertDelivery.Status.DEAD_LETTER:
                raise ValidationError({"detail": "Only dead-letter deliveries can be manually retried."})
            if not delivery.destination.is_active:
                raise ValidationError({"detail": "Activate the destination before retrying."})
            delivery.status = AlertDelivery.Status.QUEUED
            delivery.next_attempt_at = timezone.now()
            delivery.last_error_code = ""
            delivery.save(update_fields=("status", "next_attempt_at", "last_error_code", "updated_at"))
            _audit(
                request,
                action="SURVEILLANCE_DELIVERY_RETRY_REQUESTED",
                resource_type="AlertDelivery",
                resource_id=delivery.pk,
                metadata={"destination_id": str(delivery.destination_id)},
            )
            transaction.on_commit(
                lambda delivery_pk=str(delivery.pk): _enqueue_delivery(delivery_pk)
            )
        return Response(AlertDeliverySerializer(delivery).data, status=status.HTTP_202_ACCEPTED)


def _enqueue_delivery(delivery_id: str) -> None:
    try:
        from patents.surveillance_tasks import enqueue_alert_delivery

        enqueue_alert_delivery(delivery_id)
    except Exception as exc:  # recovery task will enqueue it later
        logger.warning("Manual delivery retry enqueue deferred (%s).", type(exc).__name__)


class SurveillanceWatchlistPollView(APIView):
    permission_classes = [IsAuthenticated, HasTenantAccess, RequireTenantRole(*READ_ROLES)]

    def post(self, request: Request, watchlist_id: str) -> Response:
        _require_role(request, WRITE_ROLES)
        from django.conf import settings

        if not bool(getattr(settings, "PATENTSHIELD_SURVEILLANCE_ENABLED", False)):
            raise SurveillanceConfigurationUnavailable(
                "Surveillance polling is disabled by deployment configuration."
            )

        now = timezone.now()
        active_statuses = {
            SurveillanceWatchlist.PollStatus.QUEUED,
            SurveillanceWatchlist.PollStatus.RUNNING,
        }
        try:
            from patents.surveillance_tasks import POLL_STALE_AFTER, poll_watchlist

            with transaction.atomic():
                watchlist = get_object_or_404(
                    _base_queryset(request).select_for_update(), pk=watchlist_id
                )
                if not watchlist.is_active:
                    raise ValidationError({"detail": "Activate this watchlist before polling."})

                # Return the existing task while a recent reservation is queued
                # or executing. This prevents repeated clicks from flooding the
                # external-search queue with duplicate work.
                if (
                    watchlist.poll_status in active_statuses
                    and watchlist.poll_started_at is not None
                    and watchlist.poll_started_at >= now - POLL_STALE_AFTER
                ):
                    return Response(
                        {
                            "status": watchlist.poll_status,
                            "task_id": watchlist.active_task_id,
                            "watchlist_id": str(watchlist.pk),
                            "already_active": True,
                        },
                        status=status.HTTP_202_ACCEPTED,
                    )

                task_id = str(uuid.uuid4())
                watchlist.poll_status = SurveillanceWatchlist.PollStatus.QUEUED
                watchlist.poll_started_at = now
                watchlist.active_task_id = task_id
                watchlist.next_poll_at = now + timedelta(
                    minutes=max(15, watchlist.poll_interval_minutes)
                )
                watchlist.last_error_code = ""
                watchlist.save(
                    update_fields=(
                        "poll_status",
                        "poll_started_at",
                        "active_task_id",
                        "next_poll_at",
                        "last_error_code",
                        "updated_at",
                    )
                )
                task = poll_watchlist.apply_async(
                    args=(str(watchlist.pk),),
                    queue="external_search",
                    task_id=task_id,
                )
                _audit(
                    request,
                    action="SURVEILLANCE_WATCHLIST_POLL_REQUESTED",
                    resource_type="SurveillanceWatchlist",
                    resource_id=watchlist.pk,
                    metadata={"task_id": str(task.id)},
                )
        except (ValidationError, Http404):
            raise
        except Exception as exc:
            logger.warning("Manual watchlist poll could not be queued (%s).", type(exc).__name__)
            return Response(
                {"detail": "The poll could not be queued.", "code": "POLL_ENQUEUE_FAILED"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        return Response(
            {"status": "QUEUED", "task_id": str(task.id), "watchlist_id": str(watchlist.pk)},
            status=status.HTTP_202_ACCEPTED,
        )
