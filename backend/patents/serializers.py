
    
  
"""Request serializers for tenant-scoped patent document APIs."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from rest_framework import serializers
from django.core.files.uploadedfile import UploadedFile


_DEFAULT_MAX_PDF_BYTES = 100 * 1024 * 1024
_ALLOWED_PDF_CONTENT_TYPES = {
    "application/pdf",
    "application/x-pdf",
    "application/octet-stream",
}


class PatentDocumentUploadSerializer(serializers.Serializer):
    """Validate metadata and a bounded PDF upload for a new patent record."""

    file = serializers.FileField(write_only=True, allow_empty_file=False)
    title = serializers.CharField(max_length=500, trim_whitespace=True)
    abstract = serializers.CharField(
        required=False,
        allow_blank=True,
        allow_null=True,
        trim_whitespace=True,
    )
    publication_number = serializers.CharField(
        max_length=100,
        required=False,
        allow_blank=True,
        allow_null=True,
        trim_whitespace=True,
    )
    application_number = serializers.CharField(
        max_length=100,
        required=False,
        allow_blank=True,
        allow_null=True,
        trim_whitespace=True,
    )
    jurisdiction = serializers.CharField(
        max_length=10,
        required=False,
        allow_blank=True,
        allow_null=True,
        trim_whitespace=True,
    )

    def validate_title(self, value: str) -> str:
        """Reject a title that is empty after trimming."""
        if not value.strip():
            raise serializers.ValidationError("A patent title is required.")
        return value.strip()

    def validate_file(self, uploaded_file: UploadedFile) -> UploadedFile:
        """Check size, type hint, and the PDF header without trusting filenames."""
        maximum_bytes = int(
            getattr(
                settings,
                "PATENTSHIELD_MAX_PDF_UPLOAD_BYTES",
                _DEFAULT_MAX_PDF_BYTES,
            )
        )
        if maximum_bytes <= 0:
            raise serializers.ValidationError(
                "PDF uploads are disabled by server configuration."
            )

        file_size = getattr(uploaded_file, "size", None)
        if file_size is None:
            raise serializers.ValidationError("The uploaded file size is unknown.")
        if file_size <= 0:
            raise serializers.ValidationError("The uploaded PDF is empty.")
        if file_size > maximum_bytes:
            raise serializers.ValidationError(
                f"The PDF exceeds the maximum upload size of {maximum_bytes} bytes."
            )

        content_type = (getattr(uploaded_file, "content_type", "") or "")
        if content_type and content_type.casefold() not in _ALLOWED_PDF_CONTENT_TYPES:
            raise serializers.ValidationError(
                "Only PDF documents are accepted."
            )

        try:
            original_position = uploaded_file.tell()
            uploaded_file.seek(0)
            header = uploaded_file.read(min(1024, file_size))
            uploaded_file.seek(original_position)
        except (AttributeError, OSError, ValueError) as exc:
            raise serializers.ValidationError(
                "The uploaded file could not be read."
            ) from exc

        if b"%PDF-" not in header:
            raise serializers.ValidationError(
                "The file does not contain a valid PDF header."
            )

        # The view hashes and stores the file from its start, regardless of the
        # position a multipart parser left it at.
        uploaded_file.seek(0)
        return uploaded_file

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Normalize optional metadata consistently before model creation."""
        for field_name in (
            "abstract",
            "publication_number",
            "application_number",
            "jurisdiction",
        ):
            value = attrs.get(field_name)
            if isinstance(value, str):
                value = value.strip()
                attrs[field_name] = value or None
        return attrs

