"""Editable DOCX/XLSX and archival PDF/JSON renderers for claim-chart snapshots."""

from __future__ import annotations

import io
import json
from html import escape
from typing import Any


DOCX_MIME_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
XLSX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF_MIME_TYPE = "application/pdf"
JSON_MIME_TYPE = "application/json"


def render_claim_chart(
    snapshot: dict[str, Any],
    export_format: str,
) -> tuple[bytes, str, str]:
    """Render the specified immutable chart snapshot and return bytes/MIME/extension."""
    normalized_format = export_format.upper()
    if normalized_format == "DOCX":
        return render_docx(snapshot), DOCX_MIME_TYPE, "docx"
    if normalized_format == "XLSX":
        return render_xlsx(snapshot), XLSX_MIME_TYPE, "xlsx"
    if normalized_format == "PDF":
        return render_pdf(snapshot), PDF_MIME_TYPE, "pdf"
    if normalized_format == "JSON":
        payload = json.dumps(
            snapshot,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ).encode("utf-8")
        return payload, JSON_MIME_TYPE, "json"
    raise ValueError("Unsupported claim-chart export format.")


def render_docx(snapshot: dict[str, Any]) -> bytes:
    """Create an editable Word chart with limitation, evidence, and review sections."""
    try:
        from docx import Document
        from docx.enum.section import WD_ORIENT
        from docx.shared import Inches, Pt
    except ImportError as exc:
        raise RuntimeError("DOCX export requires python-docx.") from exc

    document = Document()
    section = document.sections[0]
    section.orientation = WD_ORIENT.LANDSCAPE
    section.page_width, section.page_height = section.page_height, section.page_width
    section.left_margin = Inches(0.55)
    section.right_margin = Inches(0.55)
    section.top_margin = Inches(0.55)
    section.bottom_margin = Inches(0.55)

    chart = snapshot.get("chart", {})
    target = snapshot.get("target_patent", {})
    analysis = snapshot.get("analysis", {})
    document.core_properties.title = str(
        chart.get("title") or "PatentShield Claim Chart"
    )[:255]
    document.core_properties.subject = "Evidence-grounded patent claim chart"
    document.core_properties.author = "PatentShield"
    document.add_heading(str(chart.get("title") or "PatentShield Claim Chart"), 0)
    document.add_paragraph(
        f"Status: {chart.get('status', 'DRAFT')} | "
        f"Revision: {chart.get('revision', 1)} | "
        f"Analysis: {analysis.get('analysis_id', '—')}"
    )
    document.add_paragraph(
        "Target: "
        + " · ".join(
            str(value)
            for value in (
                target.get("publication_number") or "Unspecified publication",
                target.get("jurisdiction") or "Jurisdiction not specified",
                target.get("title") or "Untitled patent",
            )
            if value
        )
    )
    if chart.get("description"):
        document.add_paragraph(str(chart["description"]))
    document.add_paragraph(
        "AI-assisted research work product for professional review. "
        "This document is not a legal opinion or a determination of infringement, "
        "validity, novelty, or freedom to operate."
    )

    rows = snapshot.get("rows", [])
    if not rows:
        document.add_paragraph(
            "No limitation mappings are available in this chart revision."
        )
    for row in rows:
        index = row.get("limitation_index", "—")
        claim_number = row.get("claim_number", "—")
        document.add_heading(f"Limitation {index} · Claim {claim_number}", level=1)
        table = document.add_table(rows=0, cols=2)
        table.style = "Light Shading Accent 1"
        _docx_pair(table, "Limitation", row.get("limitation_text", ""))
        _docx_pair(table, "Assessment", row.get("mapping_status", "UNCERTAIN"))
        _docx_pair(
            table,
            "Evidence mapping / rationale",
            row.get("mapping_summary", ""),
        )
        _docx_pair(table, "Confidence", _format_confidence(row.get("confidence")))
        _docx_pair(
            table,
            "Attorney decision",
            row.get("attorney_decision", "UNREVIEWED"),
        )
        _docx_pair(table, "Attorney notes", row.get("attorney_notes", ""))
        evidence_items = row.get("evidence", [])
        document.add_heading("Supporting evidence", level=2)
        if not evidence_items:
            document.add_paragraph(
                "No evidence references are attached to this limitation."
            )
        for evidence_index, evidence in enumerate(evidence_items, start=1):
            source = " · ".join(
                str(value)
                for value in (
                    evidence.get("source_publication_number")
                    or evidence.get("source_patent_id")
                    or "Unidentified source",
                    evidence.get("source_jurisdiction"),
                    evidence.get("source_section"),
                    (
                        f"page {evidence['page_number']}"
                        if evidence.get("page_number")
                        else None
                    ),
                )
                if value
            )
            document.add_paragraph(
                f"Evidence {evidence_index}: {source}",
                style="Caption",
            )
            quote = evidence.get("exact_quote") or "[No quotation provided]"
            document.add_paragraph(f"“{quote}”")
            document.add_paragraph(
                "Canonical quote verification: "
                + (
                    "VERIFIED"
                    if evidence.get("quote_verified") is True
                    else "NOT VERIFIED"
                ),
                style="Caption",
            )
        document.add_paragraph("")

    normal_style = document.styles["Normal"]
    normal_style.font.name = "Arial"
    normal_style.font.size = Pt(9)
    footer = section.footer.paragraphs[0]
    footer.text = (
        "PatentShield · Evidence sources and mappings require qualified "
        "attorney review."
    )
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _docx_pair(table: Any, label: str, value: Any) -> None:
    cells = table.add_row().cells
    cells[0].text = label
    cells[1].text = str(value if value is not None else "—")


def render_xlsx(snapshot: dict[str, Any]) -> bytes:
    """Create an editable spreadsheet with one row per limitation/evidence pair."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError as exc:
        raise RuntimeError("XLSX export requires openpyxl.") from exc

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Claim Chart"
    chart = snapshot.get("chart", {})
    target = snapshot.get("target_patent", {})
    _append_safe_excel_row(sheet, ["PatentShield Claim Chart", chart.get("title", "")])
    _append_safe_excel_row(
        sheet,
        ["Target publication", target.get("publication_number") or ""],
    )
    _append_safe_excel_row(
        sheet,
        ["Target jurisdiction", target.get("jurisdiction") or ""],
    )
    _append_safe_excel_row(sheet, ["Revision", chart.get("revision", 1)])
    _append_safe_excel_row(sheet, ["Status", chart.get("status", "DRAFT")])
    _append_safe_excel_row(sheet, ["Disclaimer", snapshot.get("disclaimer", "")])
    sheet.append([])
    headers = [
        "Limitation Index", "Claim Number", "Limitation Text", "Assessment Status",
        "AI Mapping Summary", "Confidence", "Attorney Decision", "Attorney Notes",
        "Source Publication",
        "Source Patent ID",
        "Source Jurisdiction",
        "Source Section",
        "Page Number",
        "Exact Evidence Quote",
        "Quote Verified",
        "Source Document Version ID",
        "Source Start Offset", "Source End Offset",
    ]
    header_row = sheet.max_row + 1
    sheet.append(headers)
    for row in snapshot.get("rows", []):
        evidence_items = row.get("evidence") or [None]
        for evidence in evidence_items:
            evidence = evidence or {}
            _append_safe_excel_row(
                sheet,
                [
                    row.get("limitation_index"),
                    row.get("claim_number"),
                    row.get("limitation_text"),
                    row.get("mapping_status"),
                    row.get("mapping_summary"),
                    row.get("confidence"),
                    row.get("attorney_decision"),
                    row.get("attorney_notes"),
                    evidence.get("source_publication_number"),
                    evidence.get("source_patent_id"),
                    evidence.get("source_jurisdiction"),
                    evidence.get("source_section"),
                    evidence.get("page_number"),
                    evidence.get("exact_quote"),
                    "YES" if evidence.get("quote_verified") is True else "NO",
                    evidence.get("source_document_version_id"),
                    evidence.get("source_start_offset"),
                    evidence.get("source_end_offset"),
                ],
            )
    header_fill = PatternFill("solid", fgColor="1E293B")
    for cell in sheet[header_row]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(wrap_text=True, vertical="top")
    for row_cells in sheet.iter_rows(min_row=header_row + 1):
        for cell in row_cells:
            cell.alignment = Alignment(wrap_text=True, vertical="top")
    widths = [13, 13, 45, 22, 50, 12, 18, 45, 22, 38, 17, 18, 13, 60, 16, 38, 18, 18]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.freeze_panes = f"A{header_row + 1}"
    if sheet.max_row >= header_row:
        last_column = get_column_letter(len(headers))
        sheet.auto_filter.ref = f"A{header_row}:{last_column}{sheet.max_row}"
    sheet.sheet_view.showGridLines = False
    sheet.row_dimensions[header_row].height = 36

    metadata = workbook.create_sheet("Snapshot Metadata")
    _append_safe_excel_row(metadata, ["Field", "Value"])
    _append_safe_excel_row(
        metadata,
        ["Schema Version", snapshot.get("schema_version", "")],
    )
    _append_safe_excel_row(metadata, ["Chart ID", chart.get("chart_id", "")])
    _append_safe_excel_row(
        metadata, ["Analysis ID", snapshot.get("analysis", {}).get("analysis_id", "")]
    )
    _append_safe_excel_row(
        metadata, ["Snapshot Generated At", snapshot.get("generated_at", "")]
    )
    _append_safe_excel_row(
        metadata,
        [
            "Snapshot SHA-256 is stored with revision",
            "See the revision API for the canonical snapshot hash.",
        ],
    )
    _append_safe_excel_row(metadata, ["Disclaimer", snapshot.get("disclaimer", "")])
    for cell in metadata[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
    metadata.column_dimensions["A"].width = 42
    metadata.column_dimensions["B"].width = 110
    for row_cells in metadata.iter_rows():
        for cell in row_cells:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def render_pdf(snapshot: dict[str, Any]) -> bytes:
    """Render a printable landscape PDF with readable sections and verified quotes."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as exc:
        raise RuntimeError("PDF export requires reportlab.") from exc

    chart = snapshot.get("chart", {})
    target = snapshot.get("target_patent", {})
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=14 * mm,
        title=str(chart.get("title") or "PatentShield Claim Chart"),
        author="PatentShield",
    )
    styles = getSampleStyleSheet()
    body = ParagraphStyle(
        "ClaimChartBody",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=7.5,
        leading=9,
        alignment=TA_LEFT,
        spaceAfter=2,
    )
    small = ParagraphStyle(
        "ClaimChartSmall",
        parent=body,
        fontSize=6.5,
        leading=8,
    )
    title_style = styles["Title"]
    story: list[Any] = [
        Paragraph(
            _paragraph_text(chart.get("title") or "PatentShield Claim Chart"),
            title_style,
        ),
        Paragraph(
            _paragraph_text(
                "Target: "
                f"{target.get('publication_number') or 'Unspecified publication'} "
                "· "
                f"{target.get('jurisdiction') or 'Jurisdiction unspecified'} · "
                f"{target.get('title') or 'Untitled patent'}"
            ),
            styles["Normal"],
        ),
        Paragraph(
            _paragraph_text(
                f"Chart revision {chart.get('revision', 1)} · "
                f"Status {chart.get('status', 'DRAFT')} · "
                f"Analysis {snapshot.get('analysis', {}).get('analysis_id', '—')}"
            ),
            styles["Normal"],
        ),
        Spacer(1, 4 * mm),
    ]

    for row in snapshot.get("rows", []):
        heading = (
            f"Limitation {row.get('limitation_index', '—')} · "
            f"Claim {row.get('claim_number', '—')}"
        )
        story.append(
            Paragraph(_paragraph_text(heading), styles["Heading2"])
        )
        detail_data = [
            [
                Paragraph("<b>Limitation</b>", body),
                Paragraph(_paragraph_text(row.get("limitation_text")), body),
            ],
            [
                Paragraph("<b>Assessment</b>", body),
                Paragraph(_paragraph_text(row.get("mapping_status")), body),
            ],
            [
                Paragraph("<b>Mapping / rationale</b>", body),
                Paragraph(_paragraph_text(row.get("mapping_summary")), body),
            ],
            [
                Paragraph("<b>Attorney decision</b>", body),
                Paragraph(_paragraph_text(row.get("attorney_decision")), body),
            ],
            [
                Paragraph("<b>Attorney notes</b>", body),
                Paragraph(_paragraph_text(row.get("attorney_notes")), body),
            ],
        ]
        details = Table(detail_data, colWidths=[36 * mm, 218 * mm], repeatRows=0)
        details.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef2ff")),
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cbd5e1")),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.extend([details, Spacer(1, 2 * mm)])
        evidence_items = row.get("evidence", [])
        if not evidence_items:
            story.append(
                Paragraph(
                    "No evidence references are attached to this limitation.",
                    small,
                )
            )
        for number, evidence in enumerate(evidence_items, start=1):
            source = " · ".join(
                str(value)
                for value in (
                    evidence.get("source_publication_number")
                    or evidence.get("source_patent_id")
                    or "Unidentified source",
                    evidence.get("source_jurisdiction"),
                    evidence.get("source_section"),
                    (
                        f"page {evidence['page_number']}"
                        if evidence.get("page_number")
                        else None
                    ),
                )
                if value
            )
            verified_label = (
                "VERIFIED"
                if evidence.get("quote_verified") is True
                else "NOT VERIFIED"
            )
            evidence_heading = f"Evidence {number}: {source} · Quote {verified_label}"
            story.append(
                Paragraph(_paragraph_text(evidence_heading), small)
            )
            quote = f"“{evidence.get('exact_quote') or '[No quotation provided]'}”"
            story.append(Paragraph(_paragraph_text(quote), body))
        story.append(Spacer(1, 5 * mm))

    story.append(Spacer(1, 2 * mm))
    disclaimer = snapshot.get("disclaimer") or (
        "AI-assisted research work product; professional review required."
    )
    story.append(Paragraph(_paragraph_text(disclaimer), small))
    document.build(story)
    return buffer.getvalue()


def _paragraph_text(value: Any) -> str:
    """Escape source text before passing it to ReportLab's paragraph parser."""
    text = "—" if value is None or value == "" else str(value)
    return escape(text).replace("\r\n", "\n").replace("\n", "<br/>")


def _format_confidence(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "—"
    return f"{max(0.0, min(1.0, float(value))) * 100:.1f}%"


def _append_safe_excel_row(sheet: Any, values: list[Any]) -> None:
    """Append cells while neutralizing spreadsheet formula injection.

    External patent content and attorney-provided notes are untrusted text. Excel
    interprets cells beginning with formula sigils as executable expressions.
    Prefix such strings with an apostrophe while leaving numeric cells numeric.
    """
    sheet.append([_safe_excel_cell(value) for value in values])


def _safe_excel_cell(value: Any) -> Any:
    """Make untrusted string values inert in Excel-compatible spreadsheet files."""
    if not isinstance(value, str):
        return value
    first_non_whitespace = value.lstrip(" \t\r\n")[:1]
    if first_non_whitespace in {"=", "+", "-", "@", "\t", "\r"}:
        return "'" + value
    return value
