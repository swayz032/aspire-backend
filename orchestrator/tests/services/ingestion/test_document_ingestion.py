"""Unit tests for DocumentIngestionAdapter — Pass 14 expansion."""

from __future__ import annotations

from uuid import UUID

import pytest

from aspire_orchestrator.services.ingestion.base import IngestionError
from aspire_orchestrator.services.ingestion.document_ingestion import DocumentIngestionAdapter
from aspire_orchestrator.schemas.memory_v1 import ScopedIdentity

TENANT_A = UUID("dd000000-0000-0000-0000-000000000001")
SUITE_A = UUID("dd000000-0000-0000-0000-000000000002")
OFFICE_A = UUID("dd000000-0000-0000-0000-000000000003")

SCOPE_A = ScopedIdentity(tenant_id=TENANT_A, suite_id=SUITE_A, office_id=OFFICE_A)

_BASE_PAYLOAD = {
    "tenant_id": str(TENANT_A),
    "suite_id": str(SUITE_A),
    "office_id": str(OFFICE_A),
    "file_name": "service_agreement.pdf",
    "file_size_bytes": 512000,
    "mime_type": "application/pdf",
    "s3_url": "https://s3.amazonaws.com/aspire-uploads/service_agreement.pdf",
    "uploaded_by_user_id": "user_abc123",
    "uploaded_at": "2026-04-29T10:00:00Z",
    "sha256": "abc123deadbeef",
    "page_count": 5,
    "version_no": 1,
    "original_filename": "ServiceAgreement_v3.pdf",
}


# ---------------------------------------------------------------------------
# Signature (trust boundary is route layer)
# ---------------------------------------------------------------------------

class TestDocumentVerifySignature:

    @pytest.mark.asyncio
    async def test_verify_signature_always_true(self) -> None:
        """Internal route — signature always passes; security is the route auth."""
        adapter = DocumentIngestionAdapter()
        result = await adapter.verify_signature(body=b"anything", headers={})
        assert result is True

    @pytest.mark.asyncio
    async def test_verify_signature_true_with_any_headers(self) -> None:
        adapter = DocumentIngestionAdapter()
        result = await adapter.verify_signature(
            body=b"", headers={"Authorization": "Bearer garbage"}
        )
        assert result is True


# ---------------------------------------------------------------------------
# Scope resolution
# ---------------------------------------------------------------------------

class TestDocumentResolveScope:

    @pytest.mark.asyncio
    async def test_valid_payload_returns_scope(self) -> None:
        adapter = DocumentIngestionAdapter()
        scope = await adapter.resolve_scope(_BASE_PAYLOAD)
        assert scope.tenant_id == TENANT_A
        assert scope.suite_id == SUITE_A
        assert scope.office_id == OFFICE_A

    @pytest.mark.asyncio
    async def test_missing_tenant_id_raises_422(self) -> None:
        payload = {**_BASE_PAYLOAD}
        del payload["tenant_id"]
        adapter = DocumentIngestionAdapter()
        with pytest.raises(IngestionError) as exc_info:
            await adapter.resolve_scope(payload)
        assert exc_info.value.status_code == 422
        assert exc_info.value.code == "MISSING_SCOPE_FIELDS"

    @pytest.mark.asyncio
    async def test_missing_suite_id_raises_422(self) -> None:
        payload = {**_BASE_PAYLOAD}
        del payload["suite_id"]
        adapter = DocumentIngestionAdapter()
        with pytest.raises(IngestionError) as exc_info:
            await adapter.resolve_scope(payload)
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_invalid_uuid_raises_422(self) -> None:
        payload = {**_BASE_PAYLOAD, "tenant_id": "not-a-uuid"}
        adapter = DocumentIngestionAdapter()
        with pytest.raises(IngestionError) as exc_info:
            await adapter.resolve_scope(payload)
        assert exc_info.value.status_code == 422
        assert exc_info.value.code == "INVALID_SCOPE_UUID"


# ---------------------------------------------------------------------------
# Envelope building
# ---------------------------------------------------------------------------

class TestDocumentBuildEnvelope:

    @pytest.mark.asyncio
    async def test_happy_path_fields(self) -> None:
        adapter = DocumentIngestionAdapter()
        env = await adapter.build_envelope(_BASE_PAYLOAD, scope=SCOPE_A, thread=None)
        assert env.memory_type == "document"
        assert env.status == "executed"
        assert env.title == "service_agreement.pdf"
        assert "PDF" in env.summary
        assert "0.5MB" in env.summary or "MB" in env.summary

    @pytest.mark.asyncio
    async def test_missing_file_name_raises_422(self) -> None:
        payload = {**_BASE_PAYLOAD, "file_name": ""}
        adapter = DocumentIngestionAdapter()
        with pytest.raises(IngestionError) as exc_info:
            await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        assert exc_info.value.status_code == 422
        assert exc_info.value.code == "MISSING_FILE_NAME"

    @pytest.mark.asyncio
    async def test_missing_s3_url_raises_422(self) -> None:
        payload = {**_BASE_PAYLOAD, "s3_url": ""}
        adapter = DocumentIngestionAdapter()
        with pytest.raises(IngestionError) as exc_info:
            await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        assert exc_info.value.status_code == 422
        assert exc_info.value.code == "MISSING_S3_URL"

    @pytest.mark.asyncio
    async def test_idempotency_key_uses_sha256(self) -> None:
        adapter = DocumentIngestionAdapter()
        env = await adapter.build_envelope(_BASE_PAYLOAD, scope=SCOPE_A, thread=None)
        # sha256 + file_size in key
        assert "abc123deadbeef" in env.idempotency_key
        assert "512000" in env.idempotency_key

    @pytest.mark.asyncio
    async def test_idempotency_same_file_deduplicates(self) -> None:
        """Same SHA-256 + size produces same idempotency_key regardless of filename."""
        adapter = DocumentIngestionAdapter()
        env1 = await adapter.build_envelope(_BASE_PAYLOAD, scope=SCOPE_A, thread=None)
        payload2 = {**_BASE_PAYLOAD, "file_name": "copy_of_agreement.pdf"}
        env2 = await adapter.build_envelope(payload2, scope=SCOPE_A, thread=None)
        assert env1.idempotency_key == env2.idempotency_key

    @pytest.mark.asyncio
    async def test_client_provided_idempotency_key_used(self) -> None:
        payload = {**_BASE_PAYLOAD, "idempotency_key": "my-custom-key-123"}
        adapter = DocumentIngestionAdapter()
        env = await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        assert env.idempotency_key == "my-custom-key-123"

    @pytest.mark.asyncio
    async def test_fallback_idempotency_when_no_sha256(self) -> None:
        payload = {**_BASE_PAYLOAD, "sha256": None}
        adapter = DocumentIngestionAdapter()
        env = await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        # Falls back to filename-based key
        assert "service_agreement.pdf" in env.idempotency_key
        assert "512000" in env.idempotency_key

    @pytest.mark.asyncio
    async def test_mime_type_to_ext_pdf(self) -> None:
        adapter = DocumentIngestionAdapter()
        env = await adapter.build_envelope(_BASE_PAYLOAD, scope=SCOPE_A, thread=None)
        assert "PDF" in env.summary

    @pytest.mark.asyncio
    async def test_mime_type_to_ext_image(self) -> None:
        payload = {**_BASE_PAYLOAD, "mime_type": "image/jpeg", "file_name": "photo.jpg"}
        adapter = DocumentIngestionAdapter()
        env = await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        assert "JPEG" in env.summary

    @pytest.mark.asyncio
    async def test_detail_fields_present(self) -> None:
        adapter = DocumentIngestionAdapter()
        env = await adapter.build_envelope(_BASE_PAYLOAD, scope=SCOPE_A, thread=None)
        required = {"file_name", "file_size_bytes", "mime_type", "s3_url", "uploaded_by_user_id",
                    "uploaded_at", "sha256", "page_count", "version_no", "original_filename"}
        for field in required:
            assert field in env.detail, f"Missing detail field: {field}"

    @pytest.mark.asyncio
    async def test_no_raw_s3_url_logged(self) -> None:
        """S3 URL is in detail (not summary/title) — verify it is scoped correctly."""
        adapter = DocumentIngestionAdapter()
        env = await adapter.build_envelope(_BASE_PAYLOAD, scope=SCOPE_A, thread=None)
        # S3 URL should NOT appear in title or summary (only in detail)
        assert "s3.amazonaws.com" not in env.title
        assert "s3.amazonaws.com" not in env.summary

    @pytest.mark.asyncio
    async def test_title_truncated_at_80_chars(self) -> None:
        """Law #9 — title capped at 80 chars."""
        long_name = "a" * 100 + ".pdf"
        payload = {**_BASE_PAYLOAD, "file_name": long_name}
        adapter = DocumentIngestionAdapter()
        env = await adapter.build_envelope(payload, scope=SCOPE_A, thread=None)
        assert len(env.title) <= 80

    @pytest.mark.asyncio
    async def test_status_is_always_executed(self) -> None:
        """Uploads are terminal — always executed."""
        adapter = DocumentIngestionAdapter()
        env = await adapter.build_envelope(_BASE_PAYLOAD, scope=SCOPE_A, thread=None)
        assert env.status == "executed"

    @pytest.mark.asyncio
    async def test_idempotency_is_deterministic(self) -> None:
        adapter = DocumentIngestionAdapter()
        env1 = await adapter.build_envelope(_BASE_PAYLOAD, scope=SCOPE_A, thread=None)
        env2 = await adapter.build_envelope(_BASE_PAYLOAD, scope=SCOPE_A, thread=None)
        assert env1.idempotency_key == env2.idempotency_key
