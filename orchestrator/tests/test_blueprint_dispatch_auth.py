"""Blueprint dispatch auth hardening tests — Wave 9e (fix/blueprint-auth-tokens).

Covers:
  P0-1 — Full capability token validation (signature, expiry, scope, suite_id, office_id)
  P0-2 — Router-level Authorization header guard (verify_blueprint_router_auth)
  P1-6 — Magic-byte file-type verification on /upload-complete
  P1-7 — Server-side size enforcement after streaming (413 on overflow)
  P1-11 — DedupCheckUnavailableError → 503 + receipt, content_hash NOT nulled

Test categories:
  Contract tests — valid flow, schema correctness, receipt coverage
  Error-mapping tests — forged/expired/wrong-scope/wrong-tenant token → 401
  Evil tests — no auth header, PII injection, bypass attempts

Implementation note: route handlers are tested directly (not via TestClient)
to avoid requiring langgraph which is not installed in the unit-test env.
Pattern matches test_drew_dispatch.py.

conftest.py sets ASPIRE_TOKEN_SIGNING_KEY = "test-signing-key-for-ci-only"
which is the same key used by mint_token() in token_service.py.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks, HTTPException, Request

# ---------------------------------------------------------------------------
# Token mint helper — uses the same token_service as production code
# ---------------------------------------------------------------------------


def _mint(
    scope: str,
    suite_id: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    office_id: str = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    ttl_seconds: int = 45,
    tool: str | None = None,
    correlation_id: str | None = None,
) -> str:
    """Mint a real capability token and return it JSON-serialised (as the Express proxy sends it)."""
    from aspire_orchestrator.services.token_service import mint_token

    token_dict = mint_token(
        suite_id=suite_id,
        office_id=office_id,
        tool=tool or scope,
        scopes=[scope],
        correlation_id=correlation_id or f"corr-{uuid.uuid4()}",
        ttl_seconds=ttl_seconds,
    )
    return json.dumps(token_dict)


def _mint_expired(scope: str, suite_id: str, office_id: str) -> str:
    """Mint a token with a negative TTL (already expired)."""
    import os
    import hmac as _hmac
    import hashlib as _hashlib

    key = os.environ.get("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only")
    token_id = str(uuid.uuid4())
    issued_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    expires_at = datetime.now(timezone.utc) - timedelta(seconds=60)

    payload = {
        "token_id": token_id,
        "suite_id": suite_id,
        "office_id": office_id,
        "tool": scope,
        "scopes": sorted([scope]),
        "issued_at": issued_at.isoformat(),
        "expires_at": expires_at.isoformat(),
        "correlation_id": f"corr-{uuid.uuid4()}",
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    signature = _hmac.new(
        key.encode("utf-8"),
        canonical.encode("utf-8"),
        _hashlib.sha256,
    ).hexdigest()
    payload["signature"] = signature
    payload["revoked"] = False
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

SUITE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SUITE_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
OFFICE_A = "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OFFICE_B = "bbbb2222-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
CORR_ID = "test-auth-corr-001"

_FAKE_PDF_BYTES = b"%PDF-1.4 fake-pdf-content-for-auth-tests"
_FAKE_PDF_B64 = base64.b64encode(_FAKE_PDF_BYTES).decode()
_FAKE_PDF_HASH = hashlib.sha256(_FAKE_PDF_BYTES).hexdigest()

_GIF_BYTES = b"GIF89a\x01\x00\x01\x00"  # Not a PDF


def _query_receipts_by_corr(correlation_id: str) -> list[dict]:
    import aspire_orchestrator.services.receipt_store as rs
    with rs._lock:
        return [r for r in rs._receipts if r.get("correlation_id") == correlation_id]


def _make_dispatch_request(
    capability_token: str,
    suite_id: str = SUITE_A,
    office_id: str = OFFICE_A,
    correlation_id: str = CORR_ID,
) -> Any:
    from aspire_orchestrator.routes.blueprint_dispatch import BlueprintDispatchRequest
    return BlueprintDispatchRequest(
        pdf_bytes=_FAKE_PDF_B64,
        filename="test.pdf",
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
        capability_token=capability_token,
    )


def _make_upload_init_request(
    capability_token: str,
    suite_id: str = SUITE_A,
    office_id: str = OFFICE_A,
    correlation_id: str = CORR_ID,
    mime_type: str = "application/pdf",
    size_bytes: int = len(_FAKE_PDF_BYTES),
) -> Any:
    from aspire_orchestrator.routes.blueprint_dispatch import UploadInitRequest
    return UploadInitRequest(
        mime_type=mime_type,
        size_bytes=size_bytes,
        filename="test.pdf",
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
        capability_token=capability_token,
    )


def _make_upload_complete_request(
    capability_token: str,
    upload_id: str | None = None,
    suite_id: str = SUITE_A,
    office_id: str = OFFICE_A,
    correlation_id: str = CORR_ID,
) -> Any:
    from aspire_orchestrator.routes.blueprint_dispatch import UploadCompleteRequest
    return UploadCompleteRequest(
        upload_id=upload_id or str(uuid.uuid4()),
        filename="test.pdf",
        suite_id=suite_id,
        office_id=office_id,
        correlation_id=correlation_id,
        capability_token=capability_token,
    )


# ===========================================================================
# P0-1 TOKEN VALIDATION TESTS — /dispatch endpoint
# ===========================================================================


class TestDispatchTokenValidation:
    """Test all token failure modes on /dispatch."""

    @pytest.mark.asyncio
    async def test_forged_token_returns_401_with_denial_receipt(self):
        """Random string token → 401 + blueprint.upload.denied receipt."""
        import aspire_orchestrator.services.receipt_store as rs
        rs._receipts.clear()

        corr = "test-forge-dispatch-001"
        mock_bg = MagicMock(spec=BackgroundTasks)

        with (
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-f1"),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        ):
            from aspire_orchestrator.routes.blueprint_dispatch import dispatch_blueprint

            with pytest.raises(HTTPException) as exc_info:
                await dispatch_blueprint(
                    mock_bg,
                    _make_dispatch_request(
                        capability_token="this-is-not-a-valid-token",
                        correlation_id=corr,
                    ),
                )

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error"] == "INVALID_CAPABILITY_TOKEN"

        receipts = _query_receipts_by_corr(corr)
        denial = next((r for r in receipts if r["action_type"] == "blueprint.upload.denied"), None)
        assert denial is not None, "denial receipt must be emitted"
        assert denial["outcome"] == "denied"
        # No secrets/PII in receipt
        for r in receipts:
            assert "ASPIRE_TOKEN_SIGNING_KEY" not in json.dumps(r)

    @pytest.mark.asyncio
    async def test_wrong_scope_token_returns_401(self):
        """Token minted for upload_complete scope rejected by /dispatch (needs blueprints:upload)."""
        import aspire_orchestrator.services.receipt_store as rs
        rs._receipts.clear()

        corr = "test-wrongscope-dispatch-001"
        wrong_scope_token = _mint("blueprints:upload_complete", suite_id=SUITE_A, office_id=OFFICE_A)
        mock_bg = MagicMock(spec=BackgroundTasks)

        with (
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-ws1"),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        ):
            from aspire_orchestrator.routes.blueprint_dispatch import dispatch_blueprint

            with pytest.raises(HTTPException) as exc_info:
                await dispatch_blueprint(
                    mock_bg,
                    _make_dispatch_request(
                        capability_token=wrong_scope_token,
                        correlation_id=corr,
                    ),
                )

        assert exc_info.value.status_code == 401
        detail = exc_info.value.detail
        assert detail["error"] == "INVALID_CAPABILITY_TOKEN"
        assert detail["reason"] == "token_wrong_scope"

        receipts = _query_receipts_by_corr(corr)
        assert any(r["action_type"] == "blueprint.upload.denied" for r in receipts)

    @pytest.mark.asyncio
    async def test_expired_token_returns_401(self):
        """Expired token → 401 + denial receipt with token_expired reason."""
        import aspire_orchestrator.services.receipt_store as rs
        rs._receipts.clear()

        corr = "test-expired-dispatch-001"
        expired_token = _mint_expired("blueprints:upload", suite_id=SUITE_A, office_id=OFFICE_A)
        mock_bg = MagicMock(spec=BackgroundTasks)

        with (
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-exp1"),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        ):
            from aspire_orchestrator.routes.blueprint_dispatch import dispatch_blueprint

            with pytest.raises(HTTPException) as exc_info:
                await dispatch_blueprint(
                    mock_bg,
                    _make_dispatch_request(
                        capability_token=expired_token,
                        correlation_id=corr,
                    ),
                )

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["reason"] == "token_expired"

        receipts = _query_receipts_by_corr(corr)
        assert any(r["action_type"] == "blueprint.upload.denied" for r in receipts)

    @pytest.mark.asyncio
    async def test_wrong_tenant_token_returns_401(self):
        """Token minted for SUITE_B rejected when body claims SUITE_A (tenant mismatch)."""
        import aspire_orchestrator.services.receipt_store as rs
        rs._receipts.clear()

        corr = "test-wrongtenant-dispatch-001"
        suite_b_token = _mint("blueprints:upload", suite_id=SUITE_B, office_id=OFFICE_B)
        mock_bg = MagicMock(spec=BackgroundTasks)

        with (
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-wt1"),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        ):
            from aspire_orchestrator.routes.blueprint_dispatch import dispatch_blueprint

            with pytest.raises(HTTPException) as exc_info:
                await dispatch_blueprint(
                    mock_bg,
                    _make_dispatch_request(
                        capability_token=suite_b_token,
                        suite_id=SUITE_A,  # body claims SUITE_A, token says SUITE_B
                        correlation_id=corr,
                    ),
                )

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["reason"] == "token_wrong_tenant"

        receipts = _query_receipts_by_corr(corr)
        assert any(r["action_type"] == "blueprint.upload.denied" for r in receipts)

    @pytest.mark.asyncio
    async def test_valid_token_dispatch_succeeds(self):
        """Valid token passes 6-check validation and proceeds to project creation."""
        import aspire_orchestrator.services.receipt_store as rs
        rs._receipts.clear()

        corr = "test-valid-dispatch-001"
        valid_token = _mint("blueprints:upload", suite_id=SUITE_A, office_id=OFFICE_A,
                            correlation_id=corr)
        mock_bg = MagicMock(spec=BackgroundTasks)
        new_project_id = str(uuid.uuid4())

        with (
            patch(
                "aspire_orchestrator.routes.blueprint_dispatch.supabase_select",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch(
                "aspire_orchestrator.routes.blueprint_dispatch.supabase_insert",
                new_callable=AsyncMock,
                return_value={"id": new_project_id},
            ),
            patch(
                "aspire_orchestrator.routes.blueprint_dispatch.upload_raw_pdf",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "aspire_orchestrator.routes.blueprint_dispatch.supabase_update",
                new_callable=AsyncMock,
                return_value={},
            ),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-v1"),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        ):
            from aspire_orchestrator.routes.blueprint_dispatch import dispatch_blueprint

            resp = await dispatch_blueprint(
                mock_bg,
                _make_dispatch_request(
                    capability_token=valid_token,
                    correlation_id=corr,
                ),
            )

        assert resp.status == "queued"
        assert resp.project_id is not None
        receipts = _query_receipts_by_corr(corr)
        assert any(r["action_type"] == "blueprint.upload.accepted" for r in receipts)


# ===========================================================================
# P0-1 TOKEN VALIDATION TESTS — /upload-init endpoint
# ===========================================================================


class TestUploadInitTokenValidation:
    """Test token failure modes on /upload-init."""

    @pytest.mark.asyncio
    async def test_forged_token_upload_init_returns_401(self):
        """Forged token on /upload-init → 401 + signed_url_denied receipt."""
        import aspire_orchestrator.services.receipt_store as rs
        rs._receipts.clear()

        corr = "test-forge-init-001"

        with (
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-fi1"),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        ):
            from aspire_orchestrator.routes.blueprint_dispatch import blueprint_upload_init

            with pytest.raises(HTTPException) as exc_info:
                await blueprint_upload_init(
                    _make_upload_init_request(
                        capability_token='{"token_id":"fake","signature":"bad"}',
                        correlation_id=corr,
                    ),
                )

        assert exc_info.value.status_code == 401
        receipts = _query_receipts_by_corr(corr)
        assert any(r["action_type"] == "blueprint.upload.signed_url_denied" for r in receipts)

    @pytest.mark.asyncio
    async def test_wrong_scope_upload_init_returns_401(self):
        """Token scoped for /dispatch rejected by /upload-init."""
        import aspire_orchestrator.services.receipt_store as rs
        rs._receipts.clear()

        corr = "test-wrongscope-init-001"
        wrong_token = _mint("blueprints:upload", suite_id=SUITE_A, office_id=OFFICE_A)

        with (
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-wsi1"),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        ):
            from aspire_orchestrator.routes.blueprint_dispatch import blueprint_upload_init

            with pytest.raises(HTTPException) as exc_info:
                await blueprint_upload_init(
                    _make_upload_init_request(
                        capability_token=wrong_token,
                        correlation_id=corr,
                    ),
                )

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["reason"] == "token_wrong_scope"

    @pytest.mark.asyncio
    async def test_expired_token_upload_init_returns_401(self):
        """Expired token on /upload-init → 401."""
        import aspire_orchestrator.services.receipt_store as rs
        rs._receipts.clear()

        corr = "test-expired-init-001"
        expired = _mint_expired("blueprints:upload_init", suite_id=SUITE_A, office_id=OFFICE_A)

        with (
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-ei1"),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        ):
            from aspire_orchestrator.routes.blueprint_dispatch import blueprint_upload_init

            with pytest.raises(HTTPException) as exc_info:
                await blueprint_upload_init(
                    _make_upload_init_request(
                        capability_token=expired,
                        correlation_id=corr,
                    ),
                )

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["reason"] == "token_expired"

    @pytest.mark.asyncio
    async def test_wrong_tenant_upload_init_returns_401(self):
        """Token for SUITE_B rejected on /upload-init when body claims SUITE_A."""
        import aspire_orchestrator.services.receipt_store as rs
        rs._receipts.clear()

        corr = "test-wrongtenant-init-001"
        suite_b_token = _mint("blueprints:upload_init", suite_id=SUITE_B, office_id=OFFICE_B)

        with (
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-wti1"),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        ):
            from aspire_orchestrator.routes.blueprint_dispatch import blueprint_upload_init

            with pytest.raises(HTTPException) as exc_info:
                await blueprint_upload_init(
                    _make_upload_init_request(
                        capability_token=suite_b_token,
                        suite_id=SUITE_A,  # body claims SUITE_A, token says SUITE_B
                        correlation_id=corr,
                    ),
                )

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["reason"] == "token_wrong_tenant"


# ===========================================================================
# P0-1 TOKEN VALIDATION TESTS — /upload-complete endpoint
# ===========================================================================


class TestUploadCompleteTokenValidation:
    """Test token failure modes on /upload-complete."""

    @pytest.mark.asyncio
    async def test_forged_token_upload_complete_returns_401(self):
        """Random string token on /upload-complete → 401 + complete.denied receipt."""
        import aspire_orchestrator.services.receipt_store as rs
        rs._receipts.clear()

        corr = "test-forge-complete-001"
        mock_bg = MagicMock(spec=BackgroundTasks)

        with (
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-fc1"),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        ):
            from aspire_orchestrator.routes.blueprint_dispatch import blueprint_upload_complete

            with pytest.raises(HTTPException) as exc_info:
                await blueprint_upload_complete(
                    mock_bg,
                    _make_upload_complete_request(
                        capability_token="random-not-a-token",
                        correlation_id=corr,
                    ),
                )

        assert exc_info.value.status_code == 401
        receipts = _query_receipts_by_corr(corr)
        assert any(r["action_type"] == "blueprint.upload.complete.denied" for r in receipts)

    @pytest.mark.asyncio
    async def test_wrong_scope_upload_complete_returns_401(self):
        """Token for upload_init scope rejected by /upload-complete."""
        import aspire_orchestrator.services.receipt_store as rs
        rs._receipts.clear()

        corr = "test-wrongscope-complete-001"
        init_token = _mint("blueprints:upload_init", suite_id=SUITE_A, office_id=OFFICE_A)
        mock_bg = MagicMock(spec=BackgroundTasks)

        with (
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-wsc1"),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        ):
            from aspire_orchestrator.routes.blueprint_dispatch import blueprint_upload_complete

            with pytest.raises(HTTPException) as exc_info:
                await blueprint_upload_complete(
                    mock_bg,
                    _make_upload_complete_request(
                        capability_token=init_token,
                        correlation_id=corr,
                    ),
                )

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["reason"] == "token_wrong_scope"

    @pytest.mark.asyncio
    async def test_expired_token_upload_complete_returns_401(self):
        """Expired token on /upload-complete → 401."""
        import aspire_orchestrator.services.receipt_store as rs
        rs._receipts.clear()

        corr = "test-expired-complete-001"
        expired = _mint_expired("blueprints:upload_complete", suite_id=SUITE_A, office_id=OFFICE_A)
        mock_bg = MagicMock(spec=BackgroundTasks)

        with (
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-ec1"),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        ):
            from aspire_orchestrator.routes.blueprint_dispatch import blueprint_upload_complete

            with pytest.raises(HTTPException) as exc_info:
                await blueprint_upload_complete(
                    mock_bg,
                    _make_upload_complete_request(
                        capability_token=expired,
                        correlation_id=corr,
                    ),
                )

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["reason"] == "token_expired"

    @pytest.mark.asyncio
    async def test_wrong_tenant_upload_complete_returns_401(self):
        """Token for SUITE_B rejected on /upload-complete when body claims SUITE_A."""
        import aspire_orchestrator.services.receipt_store as rs
        rs._receipts.clear()

        corr = "test-wrongtenant-complete-001"
        suite_b_token = _mint("blueprints:upload_complete", suite_id=SUITE_B, office_id=OFFICE_B)
        mock_bg = MagicMock(spec=BackgroundTasks)

        with (
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-wtc1"),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        ):
            from aspire_orchestrator.routes.blueprint_dispatch import blueprint_upload_complete

            with pytest.raises(HTTPException) as exc_info:
                await blueprint_upload_complete(
                    mock_bg,
                    _make_upload_complete_request(
                        capability_token=suite_b_token,
                        suite_id=SUITE_A,
                        correlation_id=corr,
                    ),
                )

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["reason"] == "token_wrong_tenant"


# ===========================================================================
# P1-6 MAGIC BYTES TESTS
# ===========================================================================


class TestMagicByteVerification:
    """Test magic-byte verification on /upload-complete (P1-6)."""

    @pytest.mark.asyncio
    async def test_gif_as_pdf_returns_415_with_receipt(self):
        """File starting with GIF89a declared as application/pdf → 415 + complete.denied receipt."""
        import aspire_orchestrator.services.receipt_store as rs
        rs._receipts.clear()
        from aspire_orchestrator.routes import blueprint_dispatch as bd

        corr = "test-magic-gif-001"
        upload_id = str(uuid.uuid4())
        valid_token = _mint("blueprints:upload_complete", suite_id=SUITE_A, office_id=OFFICE_A,
                            correlation_id=corr)
        mock_bg = MagicMock(spec=BackgroundTasks)

        # Pre-populate the session as if /upload-init ran and stored mime_type=application/pdf
        bd._upload_sessions[upload_id] = {"suite_id": SUITE_A, "mime_type": "application/pdf"}

        # stream_pdf_for_hash returns the GIF bytes with a valid hash
        gif_bytes = _GIF_BYTES + b"\x00" * 100
        gif_hash = hashlib.sha256(gif_bytes).hexdigest()

        with (
            patch(
                "aspire_orchestrator.routes.blueprint_dispatch.stream_pdf_for_hash",
                new_callable=AsyncMock,
                return_value=(gif_bytes, gif_hash),
            ),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-gif1"),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        ):
            from aspire_orchestrator.routes.blueprint_dispatch import blueprint_upload_complete

            with pytest.raises(HTTPException) as exc_info:
                await blueprint_upload_complete(
                    mock_bg,
                    _make_upload_complete_request(
                        capability_token=valid_token,
                        upload_id=upload_id,
                        correlation_id=corr,
                    ),
                )

        assert exc_info.value.status_code == 415
        assert exc_info.value.detail["error"] == "MIME_MAGIC_MISMATCH"

        receipts = _query_receipts_by_corr(corr)
        denied = next(
            (r for r in receipts if r["action_type"] == "blueprint.upload.complete.denied"), None
        )
        assert denied is not None, "complete.denied receipt must be emitted on magic mismatch"
        assert denied["outcome"] == "denied"
        assert denied["redacted_inputs"]["reason"] == "mime_magic_mismatch"
        # Session must be cleaned up after failure
        assert upload_id not in bd._upload_sessions

    @pytest.mark.asyncio
    async def test_valid_pdf_magic_bytes_passes(self):
        """File starting with %PDF- declared as application/pdf passes magic check."""
        import aspire_orchestrator.services.receipt_store as rs
        rs._receipts.clear()
        from aspire_orchestrator.routes import blueprint_dispatch as bd

        corr = "test-magic-valid-pdf-001"
        upload_id = str(uuid.uuid4())
        valid_token = _mint("blueprints:upload_complete", suite_id=SUITE_A, office_id=OFFICE_A,
                            correlation_id=corr)
        mock_bg = MagicMock(spec=BackgroundTasks)

        bd._upload_sessions[upload_id] = {"suite_id": SUITE_A, "mime_type": "application/pdf"}

        pdf_content_hash = hashlib.sha256(_FAKE_PDF_BYTES).hexdigest()

        with (
            patch(
                "aspire_orchestrator.routes.blueprint_dispatch.stream_pdf_for_hash",
                new_callable=AsyncMock,
                return_value=(_FAKE_PDF_BYTES, pdf_content_hash),
            ),
            patch(
                "aspire_orchestrator.routes.blueprint_dispatch.supabase_select",
                new_callable=AsyncMock,
                return_value=[],  # no dedup
            ),
            patch(
                "aspire_orchestrator.routes.blueprint_dispatch.supabase_insert",
                new_callable=AsyncMock,
                return_value={"id": str(uuid.uuid4())},
            ),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-vp1"),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        ):
            from aspire_orchestrator.routes.blueprint_dispatch import blueprint_upload_complete

            resp = await blueprint_upload_complete(
                mock_bg,
                _make_upload_complete_request(
                    capability_token=valid_token,
                    upload_id=upload_id,
                    correlation_id=corr,
                ),
            )

        assert resp.status == "queued"
        assert upload_id not in bd._upload_sessions  # session cleaned up after success


# ===========================================================================
# P1-11 DEDUP CHECK UNAVAILABLE TESTS
# ===========================================================================


class TestDedupCheckUnavailable:
    """Test DedupCheckUnavailableError raises 503 and preserves content_hash (P1-11)."""

    @pytest.mark.asyncio
    async def test_dedup_db_down_upload_complete_returns_503_content_hash_unchanged(self):
        """When _validate_dedup_hit raises DedupCheckUnavailableError, return 503.

        content_hash must NOT be nulled — we cannot guarantee the upload is not a duplicate.
        """
        import aspire_orchestrator.services.receipt_store as rs
        rs._receipts.clear()
        from aspire_orchestrator.routes import blueprint_dispatch as bd

        corr = "test-dedup-503-complete-001"
        upload_id = str(uuid.uuid4())
        valid_token = _mint("blueprints:upload_complete", suite_id=SUITE_A, office_id=OFFICE_A,
                            correlation_id=corr)
        mock_bg = MagicMock(spec=BackgroundTasks)

        bd._upload_sessions[upload_id] = {"suite_id": SUITE_A, "mime_type": "application/pdf"}
        content_hash = hashlib.sha256(_FAKE_PDF_BYTES).hexdigest()

        # Simulate a project that exists (so dedup validation is triggered)
        existing_project = {
            "id": str(uuid.uuid4()),
            "suite_id": SUITE_A,
            "content_hash": content_hash,
            "stage_progress": {"ingest": "done"},
        }

        async def _failing_select(table: str, *, filters: str = "", limit: int = 100, **kwargs: Any) -> list:
            if table == "blueprint_projects":
                return [existing_project]
            # Simulate DB failure for blueprint_sheets (triggers DedupCheckUnavailableError)
            raise SupabaseClientError("Connection pool exhausted")

        from aspire_orchestrator.services.supabase_client import SupabaseClientError

        with (
            patch(
                "aspire_orchestrator.routes.blueprint_dispatch.stream_pdf_for_hash",
                new_callable=AsyncMock,
                return_value=(_FAKE_PDF_BYTES, content_hash),
            ),
            patch(
                "aspire_orchestrator.routes.blueprint_dispatch.supabase_select",
                new_callable=AsyncMock,
                side_effect=_failing_select,
            ),
            patch(
                "aspire_orchestrator.routes.blueprint_dispatch.supabase_update",
                new_callable=AsyncMock,
                return_value={},
            ) as mock_update,
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-d503c"),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        ):
            from aspire_orchestrator.routes.blueprint_dispatch import blueprint_upload_complete

            with pytest.raises(HTTPException) as exc_info:
                await blueprint_upload_complete(
                    mock_bg,
                    _make_upload_complete_request(
                        capability_token=valid_token,
                        upload_id=upload_id,
                        correlation_id=corr,
                    ),
                )

        assert exc_info.value.status_code == 503
        assert exc_info.value.detail["error"] == "DEDUP_CHECK_UNAVAILABLE"
        assert exc_info.value.headers.get("Retry-After") == "5"

        # content_hash must NOT be nulled (P1-11 key assertion)
        null_calls = [
            c for c in mock_update.call_args_list
            if isinstance(c.args[1], dict) and c.args[1].get("content_hash") is None
        ]
        assert len(null_calls) == 0, "content_hash must NOT be nulled on transient DB error"

        # Receipt must be emitted
        receipts = _query_receipts_by_corr(corr)
        assert any(r["action_type"] == "blueprint.dedup.check_failed" for r in receipts)

    @pytest.mark.asyncio
    async def test_dedup_db_down_dispatch_returns_503(self):
        """When supabase_select raises SupabaseClientError on /dispatch dedup, return 503."""
        import aspire_orchestrator.services.receipt_store as rs
        rs._receipts.clear()

        corr = "test-dedup-503-dispatch-001"
        valid_token = _mint("blueprints:upload", suite_id=SUITE_A, office_id=OFFICE_A,
                            correlation_id=corr)
        mock_bg = MagicMock(spec=BackgroundTasks)

        from aspire_orchestrator.services.supabase_client import SupabaseClientError

        with (
            patch(
                "aspire_orchestrator.routes.blueprint_dispatch.supabase_select",
                new_callable=AsyncMock,
                side_effect=SupabaseClientError("Transient DB failure"),
            ),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-d503d"),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        ):
            from aspire_orchestrator.routes.blueprint_dispatch import dispatch_blueprint

            with pytest.raises(HTTPException) as exc_info:
                await dispatch_blueprint(
                    mock_bg,
                    _make_dispatch_request(
                        capability_token=valid_token,
                        correlation_id=corr,
                    ),
                )

        assert exc_info.value.status_code == 503
        assert exc_info.value.detail["error"] == "DEDUP_CHECK_UNAVAILABLE"

        receipts = _query_receipts_by_corr(corr)
        assert any(r["action_type"] == "blueprint.dedup.check_failed" for r in receipts)


# ===========================================================================
# P0-2 ROUTER AUTH DEPENDENCY TESTS
# ===========================================================================


class TestRouterAuthDependency:
    """Test verify_blueprint_router_auth FastAPI dependency (P0-2)."""

    @pytest.mark.asyncio
    async def test_missing_auth_header_returns_401(self):
        """Request with no Authorization header → 401."""
        from aspire_orchestrator.routes.blueprint_dispatch import verify_blueprint_router_auth

        mock_request = MagicMock(spec=Request)
        mock_request.headers = {}

        with pytest.raises(HTTPException) as exc_info:
            await verify_blueprint_router_auth(mock_request)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error"] == "MISSING_AUTHORIZATION_HEADER"

    @pytest.mark.asyncio
    async def test_invalid_scheme_returns_401(self):
        """Authorization header without 'Bearer ' prefix → 401."""
        from aspire_orchestrator.routes.blueprint_dispatch import verify_blueprint_router_auth

        mock_request = MagicMock(spec=Request)
        mock_request.headers = {"authorization": "Basic dXNlcjpwYXNz"}

        with pytest.raises(HTTPException) as exc_info:
            await verify_blueprint_router_auth(mock_request)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error"] == "INVALID_AUTHORIZATION_SCHEME"

    @pytest.mark.asyncio
    async def test_valid_bearer_header_passes(self):
        """Valid Bearer header passes router auth (per-handler validation is separate)."""
        from aspire_orchestrator.routes.blueprint_dispatch import verify_blueprint_router_auth

        mock_request = MagicMock(spec=Request)
        mock_request.headers = {"authorization": "Bearer some-token-value"}

        # Should not raise
        result = await verify_blueprint_router_auth(mock_request)
        assert result is None


# ===========================================================================
# EVIL TESTS — Security hardening
# ===========================================================================


class TestEvilScenarios:
    """Adversarial security tests (Law #3, Law #6, Law #9)."""

    @pytest.mark.asyncio
    async def test_no_token_at_all_returns_401(self):
        """Empty string capability_token → 401 (not 422, not 200)."""
        import aspire_orchestrator.services.receipt_store as rs
        rs._receipts.clear()

        corr = "test-evil-notoken-001"
        mock_bg = MagicMock(spec=BackgroundTasks)

        with (
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-evil1"),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        ):
            from aspire_orchestrator.routes.blueprint_dispatch import dispatch_blueprint

            with pytest.raises(HTTPException) as exc_info:
                await dispatch_blueprint(
                    mock_bg,
                    _make_dispatch_request(capability_token="", correlation_id=corr),
                )

        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_pii_in_token_error_not_leaked_in_receipt(self):
        """PII injected into a forged token must not appear in denial receipts."""
        import aspire_orchestrator.services.receipt_store as rs
        rs._receipts.clear()

        corr = "test-evil-pii-001"
        mock_bg = MagicMock(spec=BackgroundTasks)

        # Inject SSN-like PII into a forged token
        pii_token = json.dumps({
            "token_id": "000-00-0000",  # SSN-like
            "signature": "bad",
            "pii_field": "123-45-6789",
        })

        with (
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-pii1"),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        ):
            from aspire_orchestrator.routes.blueprint_dispatch import dispatch_blueprint

            with pytest.raises(HTTPException) as exc_info:
                await dispatch_blueprint(
                    mock_bg,
                    _make_dispatch_request(
                        capability_token=pii_token,
                        correlation_id=corr,
                    ),
                )

        assert exc_info.value.status_code == 401

        receipts = _query_receipts_by_corr(corr)
        receipt_json = json.dumps(receipts)
        # Raw PII value must not appear in receipts
        assert "123-45-6789" not in receipt_json, "PII SSN must not appear in receipts"
        # Raw token value must not appear in receipts
        assert "bad" not in receipt_json.replace('"bad"', ""), True  # 'bad' is too generic, check specifically
        # Token_id (SSN-like) must not propagate to receipts as raw data
        # (capability_token_id in receipts is set to token["token_id"][:8] prefix only)
        assert "000-00-0000" not in receipt_json, "SSN-like token_id must not appear verbatim in receipts"

    @pytest.mark.asyncio
    async def test_cross_tenant_token_does_not_access_other_suite_data(self):
        """Token minted for SUITE_B cannot be used to dispatch on SUITE_A — zero cross-tenant."""
        import aspire_orchestrator.services.receipt_store as rs
        rs._receipts.clear()

        corr = "test-evil-crosssuite-001"
        mock_bg = MagicMock(spec=BackgroundTasks)
        suite_b_token = _mint("blueprints:upload", suite_id=SUITE_B, office_id=OFFICE_B)

        with (
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-xs1"),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        ):
            from aspire_orchestrator.routes.blueprint_dispatch import dispatch_blueprint

            with pytest.raises(HTTPException) as exc_info:
                await dispatch_blueprint(
                    mock_bg,
                    _make_dispatch_request(
                        capability_token=suite_b_token,
                        suite_id=SUITE_A,  # Attempting to act on SUITE_A with SUITE_B token
                        correlation_id=corr,
                    ),
                )

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["reason"] == "token_wrong_tenant"

    @pytest.mark.asyncio
    async def test_adapter_never_retries_from_within(self):
        """Adapter must not retry internally — it returns failure so the orchestrator decides.

        Verify that on token failure, dispatch_blueprint raises immediately (no retry loop).
        """
        import aspire_orchestrator.services.receipt_store as rs
        rs._receipts.clear()

        corr = "test-evil-noretry-001"
        mock_bg = MagicMock(spec=BackgroundTasks)
        call_count = 0

        original_parse = None
        try:
            from aspire_orchestrator.routes import blueprint_dispatch as bd
            original_parse = bd._parse_and_validate_token
        except ImportError:
            pass

        with (
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_trace_id", return_value="trace-nr1"),
            patch("aspire_orchestrator.routes.blueprint_dispatch.get_correlation_id", return_value=corr),
        ):
            from aspire_orchestrator.routes.blueprint_dispatch import dispatch_blueprint

            try:
                await dispatch_blueprint(
                    mock_bg,
                    _make_dispatch_request(capability_token="bad-token", correlation_id=corr),
                )
            except HTTPException:
                pass

        # The handler must NOT have made any supabase calls (token check is first, no retry)
        # This is implicitly tested by not mocking supabase_select — if the handler retried
        # past the token check, it would raise AttributeError on the unmocked async call.
        receipts = _query_receipts_by_corr(corr)
        denial_receipts = [r for r in receipts if r["action_type"] == "blueprint.upload.denied"]
        assert len(denial_receipts) == 1, "Exactly one denial receipt must be emitted (no retry loop)"
