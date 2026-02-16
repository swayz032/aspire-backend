"""S3 Provider Client — Document storage for Tec (Documents) skill pack.

Provider: AWS S3 (via httpx with presigned URLs / stub implementation)
Auth: AWS credentials from environment (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
Risk tier: GREEN (url.sign — read-only) / YELLOW (document.upload — state change)
Idempotency: Yes (S3 PUT is inherently idempotent by key)

NOTE: This is a STUB implementation. Production S3 access requires boto3
or presigned URL generation. The stub follows the full provider client
pattern (receipts, error handling) for correctness.

Tools:
  - s3.document.upload: Upload a document to S3 (YELLOW tier — state change)
  - s3.url.sign: Generate a presigned URL for document access (GREEN tier)
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from aspire_orchestrator.config.settings import settings
from aspire_orchestrator.models import Outcome, ReceiptType
from aspire_orchestrator.providers.base_client import (
    BaseProviderClient,
    ProviderError,
    ProviderRequest,
    ProviderResponse,
)
from aspire_orchestrator.providers.error_codes import InternalErrorCode
from aspire_orchestrator.services.tool_types import ToolExecutionResult

logger = logging.getLogger(__name__)


class S3Client(BaseProviderClient):
    """AWS S3 document storage client (stub implementation).

    In production, this would use boto3 or generate presigned URLs.
    For Phase 2, returns stub success with receipt tracking.
    """

    provider_id = "s3"
    base_url = ""  # Not used — S3 uses per-bucket/region URLs
    timeout_seconds = 15.0
    max_retries = 1
    idempotency_support = True  # S3 PUT is idempotent by key

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        access_key = settings.aws_access_key_id
        secret_key = settings.aws_secret_access_key
        if not access_key or not secret_key:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="AWS credentials not configured (ASPIRE_AWS_ACCESS_KEY_ID, ASPIRE_AWS_SECRET_ACCESS_KEY)",
                provider_id=self.provider_id,
            )
        # In production: SigV4 signing. For stub: just verify credentials exist.
        return {"X-Aws-Auth": "stub-sigv4"}

    async def _request(self, request: ProviderRequest) -> ProviderResponse:
        """Override: skip HTTP, return stub result.

        In production, this would use boto3 for S3 operations or
        generate presigned URLs via AWS SDK.
        """
        # Authenticate first (verifies credentials are configured)
        try:
            await self._authenticate_headers(request)
        except ProviderError as e:
            self._circuit.record_failure()
            return ProviderResponse(
                status_code=401,
                body={"error": e.code.value, "message": e.message},
                success=False,
                error_code=e.code,
                error_message=e.message,
            )

        logger.info(
            "S3 stub request: %s %s (suite=%s, corr=%s)",
            request.method,
            request.path,
            (request.suite_id[:8] if len(request.suite_id) > 8 else request.suite_id),
            (request.correlation_id[:8] if len(request.correlation_id) > 8 else request.correlation_id),
        )

        body = request.body or {}

        if "/upload" in request.path:
            return ProviderResponse(
                status_code=200,
                body={
                    "stub": True,
                    "uploaded": True,
                    "bucket": body.get("bucket", ""),
                    "key": body.get("key", ""),
                    "etag": f"stub-{uuid.uuid4().hex[:12]}",
                    "uploaded_at": datetime.now(timezone.utc).isoformat(),
                },
                success=True,
                latency_ms=0.2,
            )
        elif "/sign" in request.path:
            bucket = body.get("bucket", "aspire-documents")
            key = body.get("key", "unknown")
            region = settings.aws_s3_region
            expires_in = body.get("expires_in", 3600)
            return ProviderResponse(
                status_code=200,
                body={
                    "stub": True,
                    "presigned_url": f"https://{bucket}.s3.{region}.amazonaws.com/{key}?X-Amz-Expires={expires_in}&stub=true",
                    "expires_in": expires_in,
                    "bucket": bucket,
                    "key": key,
                },
                success=True,
                latency_ms=0.1,
            )
        else:
            return ProviderResponse(
                status_code=400,
                body={"error": "Unknown S3 operation"},
                success=False,
                error_code=InternalErrorCode.INPUT_INVALID_FORMAT,
                error_message="Unknown S3 operation path",
            )

    def _parse_error(
        self, status_code: int, body: dict[str, Any]
    ) -> InternalErrorCode:
        if status_code == 403:
            return InternalErrorCode.AUTH_SCOPE_INSUFFICIENT
        if status_code == 404:
            return InternalErrorCode.DOMAIN_NOT_FOUND
        return super()._parse_error(status_code, body)


_client: S3Client | None = None


def _get_client() -> S3Client:
    global _client
    if _client is None:
        _client = S3Client()
    return _client


async def execute_s3_document_upload(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "yellow",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute s3.document.upload — upload a document to S3.

    Required payload:
      - bucket: str — S3 bucket name
      - key: str — object key (path)
      - content_type: str — MIME type (e.g. "application/pdf")

    Optional payload:
      - body_base64: str — base64-encoded file content
      - metadata: dict — S3 object metadata
    """
    client = _get_client()

    bucket = payload.get("bucket", "")
    key = payload.get("key", "")
    content_type = payload.get("content_type", "")

    if not all([bucket, key, content_type]):
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="s3.document.upload",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="s3.document.upload",
            error="Missing required parameters: bucket, key, content_type",
            receipt_data=receipt,
        )

    response = await client._request(
        ProviderRequest(
            method="PUT",
            path="/upload",
            body={
                "bucket": bucket,
                "key": key,
                "content_type": content_type,
                "body_base64": payload.get("body_base64", ""),
            },
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
    )

    outcome = Outcome.SUCCESS if response.success else Outcome.FAILED
    reason = "EXECUTED" if response.success else (
        response.error_code.value if response.error_code else "FAILED"
    )

    receipt = client.make_receipt_data(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        tool_id="s3.document.upload",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="s3.document.upload",
            data={
                "bucket": bucket,
                "key": key,
                "content_type": content_type,
                "etag": response.body.get("etag", ""),
                "uploaded": True,
                "stub": response.body.get("stub", False),
            },
            receipt_data=receipt,
            is_stub=True,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="s3.document.upload",
            error=response.error_message or "S3 upload failed",
            receipt_data=receipt,
        )


async def execute_s3_url_sign(
    *,
    payload: dict[str, Any],
    correlation_id: str,
    suite_id: str,
    office_id: str,
    risk_tier: str = "green",
    capability_token_id: str | None = None,
    capability_token_hash: str | None = None,
) -> ToolExecutionResult:
    """Execute s3.url.sign — generate a presigned URL for S3 object access.

    Required payload:
      - bucket: str — S3 bucket name
      - key: str — object key (path)

    Optional payload:
      - expires_in: int — URL expiry in seconds (default 3600)
    """
    client = _get_client()

    bucket = payload.get("bucket", "")
    key = payload.get("key", "")

    if not bucket or not key:
        receipt = client.make_receipt_data(
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
            tool_id="s3.url.sign",
            risk_tier=risk_tier,
            outcome=Outcome.FAILED,
            reason_code="INPUT_MISSING_REQUIRED",
            capability_token_id=capability_token_id,
            capability_token_hash=capability_token_hash,
        )
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="s3.url.sign",
            error="Missing required parameters: bucket, key",
            receipt_data=receipt,
        )

    expires_in = payload.get("expires_in", 3600)

    response = await client._request(
        ProviderRequest(
            method="GET",
            path="/sign",
            body={
                "bucket": bucket,
                "key": key,
                "expires_in": expires_in,
            },
            correlation_id=correlation_id,
            suite_id=suite_id,
            office_id=office_id,
        )
    )

    outcome = Outcome.SUCCESS if response.success else Outcome.FAILED
    reason = "EXECUTED" if response.success else (
        response.error_code.value if response.error_code else "FAILED"
    )

    receipt = client.make_receipt_data(
        correlation_id=correlation_id,
        suite_id=suite_id,
        office_id=office_id,
        tool_id="s3.url.sign",
        risk_tier=risk_tier,
        outcome=outcome,
        reason_code=reason,
        capability_token_id=capability_token_id,
        capability_token_hash=capability_token_hash,
        provider_response=response,
    )

    if response.success:
        return ToolExecutionResult(
            outcome=Outcome.SUCCESS,
            tool_id="s3.url.sign",
            data={
                "presigned_url": response.body.get("presigned_url", ""),
                "bucket": bucket,
                "key": key,
                "expires_in": expires_in,
                "stub": response.body.get("stub", False),
            },
            receipt_data=receipt,
            is_stub=True,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="s3.url.sign",
            error=response.error_message or "S3 presign URL generation failed",
            receipt_data=receipt,
        )
