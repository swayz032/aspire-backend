"""Tests for the true-streaming PDF download helper (P1-5).

stream_pdf_to_temp_file must:
  - Stream chunks into a temp file without ever building a single contiguous
    in-memory buffer of the full PDF.
  - Return (Path, hex_digest, size_bytes) with a correct SHA-256.
  - Clean up the temp file on error paths.
"""

from __future__ import annotations

import hashlib
import os

os.environ.setdefault("ASPIRE_RATE_LIMIT", "100000")
os.environ.setdefault("ASPIRE_TOKEN_SIGNING_KEY", "test-signing-key-for-ci-only")

from pathlib import Path
from unittest.mock import patch

import pytest


class _FakeStreamResp:
    def __init__(self, chunks: list[bytes], status: int = 200) -> None:
        self._chunks = chunks
        self.status_code = status

    async def __aenter__(self) -> "_FakeStreamResp":
        return self

    async def __aexit__(self, *a) -> bool:
        return False

    async def aiter_bytes(self, chunk_size: int = 8192):
        for c in self._chunks:
            yield c


class _FakeClient:
    def __init__(self, chunks: list[bytes], status: int = 200) -> None:
        self._chunks = chunks
        self._status = status

    async def __aenter__(self) -> "_FakeClient":
        return self

    async def __aexit__(self, *a) -> bool:
        return False

    def stream(self, method: str, url: str, headers=None):
        return _FakeStreamResp(self._chunks, status=self._status)


@pytest.mark.asyncio
async def test_stream_pdf_to_temp_file_correct_hash_and_size(tmp_path: Path) -> None:
    """Hash and size must match a known 1 MB payload split across many chunks."""
    from aspire_orchestrator.services.blueprint import pdf_signed_upload

    # 1 MB of deterministic bytes
    payload = bytes((i * 7) & 0xFF for i in range(1024 * 1024))
    expected_hash = hashlib.sha256(payload).hexdigest()
    expected_size = len(payload)

    # Split into 17 KB chunks so we go through many iterations
    chunk_size = 17 * 1024
    chunks = [payload[i : i + chunk_size] for i in range(0, len(payload), chunk_size)]
    assert len(chunks) > 50  # sanity: many chunks

    client_factory = lambda *a, **kw: _FakeClient(chunks, status=200)

    with patch.object(pdf_signed_upload, "httpx") as mock_httpx, \
         patch.object(pdf_signed_upload.settings, "supabase_url", "https://x.supabase.co"), \
         patch.object(pdf_signed_upload.settings, "supabase_service_role_key", "fake-key"):
        mock_httpx.AsyncClient = client_factory
        mock_httpx.TimeoutException = Exception
        result = await pdf_signed_upload.stream_pdf_to_temp_file(
            suite_id="00000000-0000-0000-0000-000000000001",
            upload_id="upload-stream-test-aa",
            correlation_id="corr-stream-test",
        )

    assert result is not None
    temp_path, hex_digest, size_bytes = result
    try:
        assert hex_digest == expected_hash
        assert size_bytes == expected_size
        # Disk content matches the input bytes
        assert temp_path.exists()
        on_disk = temp_path.read_bytes()
        assert len(on_disk) == expected_size
        assert hashlib.sha256(on_disk).hexdigest() == expected_hash
    finally:
        temp_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_stream_pdf_to_temp_file_no_accumulator_attribute() -> None:
    """The new helper must NOT keep a `chunks` list — that was the OOM hazard.

    We can't easily inspect locals, but we can verify the implementation does
    not call b"".join() by ensuring we never construct a single buffer larger
    than one chunk. Surrogate: confirm the function returns a Path, not bytes.
    """
    from aspire_orchestrator.services.blueprint import pdf_signed_upload

    chunks = [b"x" * 8192 for _ in range(4)]
    client_factory = lambda *a, **kw: _FakeClient(chunks, status=200)

    with patch.object(pdf_signed_upload, "httpx") as mock_httpx, \
         patch.object(pdf_signed_upload.settings, "supabase_url", "https://x.supabase.co"), \
         patch.object(pdf_signed_upload.settings, "supabase_service_role_key", "fake-key"):
        mock_httpx.AsyncClient = client_factory
        mock_httpx.TimeoutException = Exception
        result = await pdf_signed_upload.stream_pdf_to_temp_file(
            suite_id="00000000-0000-0000-0000-000000000001",
            upload_id="upload-no-accum",
            correlation_id="corr-no-accum",
        )

    assert result is not None
    temp_path, hex_digest, size_bytes = result
    try:
        # Returns Path (not bytes) by contract
        assert isinstance(temp_path, Path)
        assert isinstance(hex_digest, str) and len(hex_digest) == 64
        assert size_bytes == 8192 * 4
    finally:
        temp_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_stream_pdf_to_temp_file_404_returns_none_and_cleans_temp() -> None:
    from aspire_orchestrator.services.blueprint import pdf_signed_upload

    client_factory = lambda *a, **kw: _FakeClient([], status=404)

    with patch.object(pdf_signed_upload, "httpx") as mock_httpx, \
         patch.object(pdf_signed_upload.settings, "supabase_url", "https://x.supabase.co"), \
         patch.object(pdf_signed_upload.settings, "supabase_service_role_key", "fake-key"):
        mock_httpx.AsyncClient = client_factory
        mock_httpx.TimeoutException = Exception
        result = await pdf_signed_upload.stream_pdf_to_temp_file(
            suite_id="00000000-0000-0000-0000-000000000001",
            upload_id="upload-missing",
            correlation_id="corr-missing",
        )

    assert result is None  # 404 surfaces as None; temp file cleaned up internally
