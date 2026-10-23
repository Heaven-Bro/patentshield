"""Asynchronous JSON and PDF export task for analysis reports."""

from __future__ import annotations

import io
import json
import logging
from collections import defaultdict
from typing import Any

from celery import Task, shared_task
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone
from xml.sax.saxutils import escape

from analyses.models import AnalysisReport, ClaimAssessment, EvidenceReference

logger = logging.getLogger(__name__)


@shared_task(bind=True, queue="exports")
def generate_analysis_report(
    self: Task,
    analysis_id: str,
) -> dict[str, str | int]:
    """Create tenant-owned JSON and PDF snapshots for an analysis report.

    Report data is read through tenant-filtered querysets and linked objects are
    checked for tenant consistency before any document is generated. The task
    marks the report completed only after both artifacts have been persisted.
    """
    json_storage_key: str | None = None
    pdf_storage_key: str | None = None
    report: AnalysisReport | None = None

    try:
        report = (
            AnalysisReport.objects.select_related(
                "tenant",
                "patent",
                "target_document_version",
            )
            .get(pk=analysis_id)
        )
        _validate_report_relations(report)
        now = timezone.now()
        AnalysisReport.objects.filter(
            pk=report.pk,
            tenant_id=report.tenant_id,
        ).update(
            status=AnalysisReport.Status.EXPORTING,
            started_at=report.started_at or now,
            completed_at=None,
            error_code=None,
            updated_at=now,
        )

        assessments = list(
            ClaimAssessment.objects.for_tenant(report.tenant_id)
            .filter(analysis_id=report.pk)
            .select_related("claim", "claim__patent", "reviewed_by")
            .order_by("claim_id", "limitation_index")
        )
        _validate_assessment_relations(report, assessments)

        assessment_ids = [assessment.pk for assessment in assessments]
        evidence_rows = list(
            EvidenceReference.objects.for_tenant(report.tenant_id)
            .filter(assessment_id__in=assessment_ids)
            .select_related(
                "assessment",
                "source_patent",
                "source_document_version",
            )
            .order_by("source_page_index", "created_at")
        )
        _validate_evidence_relations(report, evidence_rows)

        evidence_by_assessment: dict[str, list[EvidenceReference]] = defaultdict(list)
        for evidence in evidence_rows:
            evidence_by_assessment[str(evidence.assessment_id)].append(evidence)

        generated_at = timezone.now()
        snapshot = _build_snapshot(
            report=report,
            assessments=assessments,
            evidence_by_assessment=evidence_by_assessment,
            generated_at=generated_at,
        )
        json_bytes = json.dumps(
            snapshot,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        pdf_bytes = _render_pdf(snapshot)

        output_dir = f"reports/{report.tenant_id}"
        json_target = f"{output_dir}/{report.pk}.json"
        pdf_target = f"{output_dir}/{report.pk}.pdf"
        json_storage_key = _save_at_key(json_target, json_bytes)
        pdf_storage_key = _save_at_key(pdf_target, pdf_bytes)

        with transaction.atomic():
            locked_report = (
                AnalysisReport.objects.for_tenant(report.tenant_id)
                .select_for_update()
                .get(pk=report.pk)
            )
            locked_report.report_json_storage_key = json_storage_key
            locked_report.report_storage_key = pdf_storage_key
            locked_report.status = AnalysisReport.Status.COMPLETED
            locked_report.completed_at = timezone.now()
            locked_report.error_code = None
            locked_report.save(
                update_fields=(
                    "report_json_storage_key",
                    "report_storage_key",
                    "status",
                    "completed_at",
                    "error_code",
                    "updated_at",
                )
            )

        logger.info(
            "Generated report artifacts for analysis %s (tenant=%s).",
            report.pk,
            report.tenant_id,
        )
        return {
            "analysis_id": str(report.pk),
            "tenant_id": str(report.tenant_id),
            "status": AnalysisReport.Status.COMPLETED,
            "assessment_count": len(assessments),
            "evidence_count": len(evidence_rows),
            "json_storage_key": json_storage_key,
            "pdf_storage_key": pdf_storage_key,
        }

    except Exception as exc:
        for storage_key in (pdf_storage_key, json_storage_key):
            if storage_key:
                try:
                    if default_storage.exists(storage_key):
                        default_storage.delete(storage_key)
                except Exception:
                    logger.warning(
                        "Unable to clean up incomplete report artifact %s.",
                        storage_key,
                        exc_info=True,
                    )
        if report is not None:
            try:
                AnalysisReport.objects.for_tenant(report.tenant_id).filter(
                    pk=report.pk
                ).update(
                    status=AnalysisReport.Status.FAILED,
                    error_code=type(exc).__name__[:100],
                    updated_at=timezone.now(),
                )
            except Exception:
                logger.exception(
                    "Unable to mark analysis report %s as failed.",
                    report.pk,
                )
        logger.exception(
            "Report generation failed for analysis ID %s.",
            analysis_id,
        )
        raise


def _validate_report_relations(report: AnalysisReport) -> None:
    """Reject inconsistent tenant relationships before reading report content."""
    if report.patent.tenant_id != report.tenant_id:
        raise ValueError("Analysis patent belongs to a different tenant.")
    if report.target_document_version.tenant_id != report.tenant_id:
        raise ValueError("Analysis document belongs to a different tenant.")
    if report.target_document_version.patent_id != report.patent_id:
        raise ValueError("Analysis document version belongs to a different patent.")


def _validate_assessment_relations(
    report: AnalysisReport,
    assessments: list[ClaimAssessment],
) -> None:
    """Reject an assessment attached to a claim from another tenant."""
    for assessment in assessments:
        if assessment.tenant_id != report.tenant_id:
            raise ValueError("Assessment belongs to a different tenant.")
        if assessment.analysis_id != report.pk:
            raise ValueError("Assessment belongs to a different analysis.")
        if assessment.claim.tenant_id != report.tenant_id:
            raise ValueError("Assessment claim belongs to a different tenant.")
        if assessment.claim.patent_id != report.patent_id:
            raise ValueError("Assessment claim belongs to a different patent.")
        if assessment.claim.patent.tenant_id != report.tenant_id:
            raise ValueError("Assessment claim's patent belongs to another tenant.")


def _validate_evidence_relations(
    report: AnalysisReport,
    evidence_rows: list[EvidenceReference],
) -> None:
    """Reject mismatched evidence ownership or assessment references."""
    for evidence in evidence_rows:
        if evidence.tenant_id != report.tenant_id:
            raise ValueError("Evidence reference belongs to a different tenant.")
        if evidence.assessment.tenant_id != report.tenant_id:
            raise ValueError("Evidence assessment belongs to a different tenant.")
        if evidence.assessment.analysis_id != report.pk:
            raise ValueError("Evidence reference belongs to another analysis.")
        if (
            evidence.source_patent is not None
            and evidence.source_patent.tenant_id != report.tenant_id
        ):
            raise ValueError("Evidence patent belongs to a different tenant.")
        if (
            evidence.source_document_version is not None
            and evidence.source_document_version.tenant_id != report.tenant_id
        ):
            raise ValueError(
                "Evidence document version belongs to a different tenant."
            )


def _build_snapshot(
    report: AnalysisReport,
    assessments: list[ClaimAssessment],
    evidence_by_assessment: dict[str, list[EvidenceReference]],
    generated_at: Any,
) -> dict[str, Any]:
    """Serialize report records into a stable, versioned JSON contract."""
    assessment_payloads: list[dict[str, Any]] = []
    for assessment in assessments:
        evidence_payloads = [
            {
                "evidence_id": str(evidence.pk),
                "source_patent_id": (
                    str(evidence.source_patent_id)
                    if evidence.source_patent_id
                    else None
                ),
                "source_document_version_id": (
                    str(evidence.source_document_version_id)
                    if evidence.source_document_version_id
                    else None
                ),
                "source_section": evidence.source_section,
                "page_index": evidence.source_page_index,
                "source_start_offset": evidence.source_start_offset,
                "source_end_offset": evidence.source_end_offset,
                "exact_quote": evidence.exact_quote,
                "quote_verified": evidence.quote_verified,
                "bbox": evidence.bbox,
                "retrieval_metadata": _redact_sensitive_values(
                    evidence.retrieval_metadata
                ),
            }
            for evidence in evidence_by_assessment.get(str(assessment.pk), [])
        ]
        assessment_payloads.append(
            {
                "assessment_id": str(assessment.pk),
                "claim_id": str(assessment.claim_id),
                "claim_number": assessment.claim.claim_number,
                "limitation_index": assessment.limitation_index,
                "limitation_text": assessment.limitation_text,
                "assessment_status": assessment.assessment_status,
                "explanation": assessment.explanation,
                "confidence": assessment.confidence,
                "metrics": assessment.metrics,
                "review_status": assessment.review_status,
                "reviewer_notes": assessment.reviewer_notes,
                "reviewed_by_id": (
                    str(assessment.reviewed_by_id)
                    if assessment.reviewed_by_id
                    else None
                ),
                "reviewed_at": _isoformat(assessment.reviewed_at),
                "evidence": evidence_payloads,
            }
        )

    summary = report.result_summary or {}
    if not isinstance(summary, dict):
        raise ValueError("Analysis result_summary must be a JSON object.")

    return {
        "schema_version": "patentshield-analysis-report-v1",
        "generated_at": generated_at.isoformat(),
        "tenant": {
            "tenant_id": str(report.tenant_id),
        },
        "analysis": {
            "analysis_id": str(report.pk),
            "analysis_type": report.analysis_type,
            "status": AnalysisReport.Status.COMPLETED,
            "created_at": _isoformat(report.created_at),
            "started_at": _isoformat(report.started_at),
            "completed_at": generated_at.isoformat(),
            "configuration": _redact_sensitive_values(report.configuration),
            "model_versions": _redact_sensitive_values(report.model_versions),
            "score_version": report.score_version,
        },
        "patent": {
            "patent_id": str(report.patent_id),
            "publication_number": report.patent.publication_number,
            "application_number": report.patent.application_number,
            "jurisdiction": report.patent.jurisdiction,
            "title": report.patent.title,
            "abstract": report.patent.abstract,
        },
        "target_document_version": {
            "document_version_id": str(report.target_document_version_id),
            "version_number": report.target_document_version.version_number,
            "content_sha256": report.target_document_version.content_sha256,
            "mime_type": report.target_document_version.mime_type,
            "page_count": report.target_document_version.page_count,
        },
        "metrics": summary.get("metrics", {}),
        "result_summary": _redact_sensitive_values(summary),
        "assessments": assessment_payloads,
        "counts": {
            "assessments": len(assessments),
            "evidence_references": sum(
                len(items) for items in evidence_by_assessment.values()
            ),
        },
    }


def _render_pdf(snapshot: dict[str, Any]) -> bytes:
    """Render a readable PDF with escaped user/source content."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import (
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Report PDF export requires the 'reportlab' package."
        ) from exc

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title="PatentShield Analysis Report",
        author="PatentShield",
    )
    styles = getSampleStyleSheet()
    font_name = "Helvetica"
    font_path = getattr(settings, "PATENTSHIELD_REPORT_FONT_PATH", None)
    if font_path:
        try:
            pdfmetrics.registerFont(TTFont("PatentShieldUnicode", font_path))
            font_name = "PatentShieldUnicode"
        except (OSError, ValueError):
            logger.exception(
                "Configured PatentShield report font could not be loaded."
            )
            raise RuntimeError(
                "PATENTSHIELD_REPORT_FONT_PATH must point to a valid TTF font."
            )
    font_style_names = (
        "Normal",
        "BodyText",
        "Title",
        "Heading1",
        "Heading2",
        "Heading3",
    )
    for style_name in font_style_names:
        if style_name in styles:
            styles[style_name].fontName = font_name
    styles.add(
        ParagraphStyle(
            name="ReportTitle",
            parent=styles["Title"],
            alignment=TA_CENTER,
            fontName=font_name,
            spaceAfter=12,
        )
    )
    styles.add(
        ParagraphStyle(
            name="SectionHeading",
            parent=styles["Heading2"],
            fontName=font_name,
            spaceBefore=10,
            spaceAfter=6,
        )
    )
    styles.add(
        ParagraphStyle(
            name="SmallBody",
            parent=styles["BodyText"],
            fontName=font_name,
            fontSize=8,
            leading=10,
            spaceAfter=4,
        )
    )

    def paragraph(value: Any, style: str = "BodyText") -> Paragraph:
        safe_text = escape("" if value is None else str(value))
        safe_text = safe_text.replace("\r\n", "\n").replace("\n", "<br/>")
        return Paragraph(safe_text or "—", styles[style])

    story: list[Any] = [
        paragraph("PatentShield Analysis Report", "ReportTitle"),
        paragraph(f"Analysis ID: {snapshot['analysis']['analysis_id']}"),
        paragraph(f"Generated: {snapshot['generated_at']}"),
        paragraph(f"Status: {snapshot['analysis']['status']}"),
        Spacer(1, 8),
        paragraph("Target Patent", "SectionHeading"),
        paragraph(f"Title: {snapshot['patent'].get('title') or 'Untitled'}"),
        paragraph(f"Patent ID: {snapshot['patent']['patent_id']}"),
        paragraph(
            "Publication number: "
            f"{snapshot['patent'].get('publication_number') or '—'}"
        ),
        paragraph(
            "Jurisdiction: "
            f"{snapshot['patent'].get('jurisdiction') or '—'}"
        ),
        paragraph(
            "Document version: "
            f"{snapshot['target_document_version']['version_number']}"
        ),
        Spacer(1, 8),
        paragraph("Screening Metrics", "SectionHeading"),
    ]

    metric_items = snapshot.get("metrics") or {}
    metric_rows: list[list[Any]] = [
        [paragraph("Metric", "SmallBody"), paragraph("Value", "SmallBody")]
    ]
    if isinstance(metric_items, dict):
        for key, value in sorted(metric_items.items()):
            metric_rows.append(
                [paragraph(key, "SmallBody"), paragraph(value, "SmallBody")]
            )
    if len(metric_rows) == 1:
        metric_rows.append(
            [
                paragraph("No metrics recorded", "SmallBody"),
                paragraph("—", "SmallBody"),
            ]
        )
    metric_table = Table(metric_rows, colWidths=(65 * mm, 95 * mm), repeatRows=1)
    metric_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EDF3")),
                ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#AAB4C0")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    story.extend(
        [
            metric_table,
            Spacer(1, 8),
            paragraph("Limitation Assessments", "SectionHeading"),
        ]
    )

    for index, assessment in enumerate(snapshot["assessments"], start=1):
        story.extend(
            [
                paragraph(
                    f"{index}. Claim {assessment['claim_number']} — "
                    f"Limitation {assessment['limitation_index']}: "
                    f"{assessment['assessment_status']}",
                    "Heading3",
                ),
                paragraph(f"Limitation: {assessment['limitation_text']}"),
                paragraph(f"Explanation: {assessment['explanation'] or '—'}"),
                paragraph(
                    "Confidence: "
                    f"{_display_confidence(assessment['confidence'])}"
                ),
                paragraph(f"Review status: {assessment['review_status']}"),
            ]
        )
        if assessment["reviewer_notes"]:
            story.append(paragraph(f"Reviewer notes: {assessment['reviewer_notes']}"))
        if not assessment["evidence"]:
            story.append(
                paragraph("Evidence: No evidence references recorded.", "SmallBody")
            )
        for evidence_index, evidence in enumerate(assessment["evidence"], start=1):
            page_label = (
                str(evidence["page_index"])
                if evidence["page_index"] is not None
                else "unknown"
            )
            story.extend(
                [
                    paragraph(
                        f"Evidence {evidence_index} "
                        f"| verified={evidence['quote_verified']} "
                        f"| source page index={page_label}",
                        "SmallBody",
                    ),
                    paragraph(evidence["exact_quote"], "SmallBody"),
                ]
            )
        story.append(Spacer(1, 5))

    doc.build(story)
    return buffer.getvalue()


def _save_at_key(target_key: str, content: bytes) -> str:
    """Write bytes at a stable key, replacing the previous report artifact."""
    if default_storage.exists(target_key):
        default_storage.delete(target_key)
    saved_key = default_storage.save(target_key, ContentFile(content))
    if not saved_key.startswith("reports/"):
        raise RuntimeError("Report storage returned an unexpected storage key.")
    return saved_key


def _isoformat(value: Any) -> str | None:
    """Serialize an optional datetime in ISO-8601 format."""
    return value.isoformat() if value is not None else None


def _display_confidence(value: Any) -> str:
    """Format a confidence value for human-readable report output."""
    return str(value) if value is not None else "—"


def _redact_sensitive_values(value: Any) -> Any:
    """Redact obvious secrets from metadata included in exported reports."""
    secret_markers = (
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "authorization",
        "password",
        "secret",
        "credential",
    )
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, nested_value in value.items():
            normalized_key = str(key).casefold().replace("-", "_")
            if any(marker in normalized_key for marker in secret_markers):
                sanitized[str(key)] = "[REDACTED]"
            else:
                sanitized[str(key)] = _redact_sensitive_values(nested_value)
        return sanitized
    if isinstance(value, list):
        return [_redact_sensitive_values(item) for item in value]
    return value
