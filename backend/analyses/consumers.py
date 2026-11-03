"""Tenant-authorized WebSocket consumer for analysis progress events."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer

from analyses.models import AnalysisReport
from enterprise_identity.services import matter_access_allowed
from tenants.models import Tenant, TenantMembership

logger = logging.getLogger(__name__)


class AnalysisProgressConsumer(AsyncJsonWebsocketConsumer):
    """Stream job events only to active members of the report's tenant."""

    analysis_id: str
    group_name: str
    tenant_id: str

    async def connect(self) -> None:
        """Authenticate and authorize before joining the analysis group."""
        user = self.scope.get("user")
        if user is None or not getattr(user, "is_authenticated", False):
            await self.close(code=4401)
            return

        raw_analysis_id = self.scope.get("url_route", {}).get(
            "kwargs", {}
        ).get("analysis_id")
        try:
            analysis_uuid = UUID(str(raw_analysis_id))
        except (AttributeError, TypeError, ValueError):
            await self.close(code=4400)
            return

        tenant_id = await self._resolve_authorized_tenant(
            user_id=user.pk,
            analysis_id=analysis_uuid,
        )
        if tenant_id is None:
            # Use the same denial for missing analyses and unauthorized users
            # to avoid exposing cross-tenant analysis existence.
            await self.close(code=4403)
            return

        self.analysis_id = str(analysis_uuid)
        self.tenant_id = tenant_id
        self.group_name = f"analysis_{self.analysis_id}"

        try:
            await self.channel_layer.group_add(
                self.group_name,
                self.channel_name,
            )
        except Exception:
            logger.exception(
                "Unable to join progress group for analysis %s.",
                self.analysis_id,
            )
            await self.close(code=1011)
            return

        await self.accept()

    async def disconnect(self, close_code: int) -> None:
        """Leave the group if this connection successfully joined it."""
        group_name = getattr(self, "group_name", None)
        if group_name:
            try:
                await self.channel_layer.group_discard(
                    group_name,
                    self.channel_name,
                )
            except Exception:
                logger.warning(
                    "Unable to leave progress group for analysis %s.",
                    getattr(self, "analysis_id", "unknown"),
                    exc_info=True,
                )

    async def job_progress(self, event: dict[str, Any]) -> None:
        """Send a structured progress update to the connected client."""
        await self.send_json(
            {
                "type": "job.progress",
                "data": event.get("data", {}),
            }
        )

    async def node_completed(self, event: dict[str, Any]) -> None:
        """Send a completed-node event to the connected client."""
        await self.send_json(
            {
                "type": "node.completed",
                "data": event.get("data", {}),
            }
        )

    async def job_completed(self, event: dict[str, Any]) -> None:
        """Send the terminal job event to the connected client."""
        await self.send_json(
            {
                "type": "job.completed",
                "data": event.get("data", {}),
            }
        )

    async def report_progress(self, event: dict[str, Any]) -> None:
        """Forward asynchronous JSON/PDF export progress to the client."""
        await self.send_json(
            {
                "type": "report.progress",
                "data": event.get("data", {}),
            }
        )

    async def report_completed(self, event: dict[str, Any]) -> None:
        """Forward terminal export state without closing the analysis socket."""
        await self.send_json(
            {
                "type": "report.completed",
                "data": event.get("data", {}),
            }
        )

    async def claim_chart_export_progress(self, event: dict[str, Any]) -> None:
        """Forward chart-export progress over the already-authorized analysis group."""
        await self.send_json(
            {
                "type": "claim_chart.export.progress",
                "data": event.get("data", {}),
            }
        )

    async def claim_chart_export_completed(self, event: dict[str, Any]) -> None:
        """Forward terminal claim-chart export status without closing the socket."""
        await self.send_json(
            {
                "type": "claim_chart.export.completed",
                "data": event.get("data", {}),
            }
        )

    @database_sync_to_async
    def _resolve_authorized_tenant(
        self,
        user_id: Any,
        analysis_id: UUID,
    ) -> str | None:
        """Return the tenant ID only when the analysis membership is active."""
        report = (
            AnalysisReport.objects.select_related(
                "patent",
                "target_document_version",
            )
            .filter(pk=analysis_id)
            .first()
        )
        if report is None:
            return None
        if (
            report.patent.tenant_id != report.tenant_id
            or report.target_document_version.tenant_id != report.tenant_id
            or report.target_document_version.patent_id != report.patent_id
        ):
            return None

        membership = (
            TenantMembership.objects.select_related("tenant", "user")
            .filter(
                tenant_id=report.tenant_id,
                tenant__status=Tenant.Status.ACTIVE,
                user_id=user_id,
                status=TenantMembership.Status.ACTIVE,
            )
            .first()
        )
        if membership is None:
            return None
        # Match the HTTP authorization boundary: tenant membership alone does
        # not grant access to a matter whose explicit ACL is enforced.
        if not matter_access_allowed(
            tenant=membership.tenant,
            user=membership.user,
            matter_type="analysis",
            matter_id=report.pk,
            required="READ",
        ):
            return None
        if not matter_access_allowed(
            tenant=membership.tenant,
            user=membership.user,
            matter_type="patent",
            matter_id=report.patent_id,
            required="READ",
        ):
            return None
        return str(report.tenant_id)
