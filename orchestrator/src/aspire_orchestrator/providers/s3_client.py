"""S3 Provider Client — Document storage for Tec (Documents) skill pack.

Provider: AWS S3 (via boto3)
Auth: AWS credentials from environment (AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY)
Risk tier: GREEN (url.sign — read-only) / YELLOW (document.upload — state change)
Idempotency: Yes (S3 PUT is inherently idempotent by key)

Tools:
  - s3.document.upload: Upload a document to S3 (YELLOW tier — state change)
  - s3.url.sign: Generate a presigned URL for document access (GREEN tier)
"""

from __future__ import annotations

import base64
import logging
import time
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
    """AWS S3 document storage client (boto3 implementation).

    Uses boto3 for real S3 operations: put_object for uploads,
    generate_presigned_url for signed URL generation.
    """

    provider_id = "s3"
    base_url = ""  # Not used — S3 uses per-bucket/region URLs
    timeout_seconds = 15.0
    max_retries = 1
    idempotency_support = True  # S3 PUT is idempotent by key

    def __init__(self) -> None:
        super().__init__()
        self._boto3_client: Any = None

    def _get_s3_client(self) -> Any:
        """Lazily create and cache a boto3 S3 client with region from settings."""
        if self._boto3_client is None:
            import boto3

            self._boto3_client = boto3.client(
                "s3",
                region_name=settings.aws_s3_region,
                aws_access_key_id=settings.aws_access_key_id,
                aws_secret_access_key=settings.aws_secret_access_key,
            )
        return self._boto3_client

    async def _authenticate_headers(
        self, request: ProviderRequest
    ) -> dict[str, str]:
        """Verify AWS credentials are configured via STS get_caller_identity."""
        access_key = settings.aws_access_key_id
        secret_key = settings.aws_secret_access_key
        if not access_key or not secret_key:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message="AWS credentials not configured (ASPIRE_AWS_ACCESS_KEY_ID, ASPIRE_AWS_SECRET_ACCESS_KEY)",
                provider_id=self.provider_id,
            )
        # Verify credentials are valid via STS
        try:
            import boto3

            sts = boto3.client(
                "sts",
                region_name=settings.aws_s3_region,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
            )
            sts.get_caller_identity()
        except Exception as exc:
            raise ProviderError(
                code=InternalErrorCode.AUTH_INVALID_KEY,
                message=f"AWS credential verification failed: {str(exc)[:200]}",
                provider_id=self.provider_id,
            ) from exc
        return {"X-Aws-Auth": "verified"}

    async def _request(self, request: ProviderRequest) -> ProviderResponse:
        """Execute real S3 operations via boto3.

        Supported paths:
          /upload — put_object (bucket, key, body_base64, content_type)
          /sign   — generate_presigned_url (bucket, key, expires_in)
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
            "S3 boto3 request: %s %s (suite=%s, corr=%s)",
            request.method,
            request.path,
            (request.suite_id[:8] if len(request.suite_id) > 8 else request.suite_id),
            (request.correlation_id[:8] if len(request.correlation_id) > 8 else request.correlation_id),
        )

        body = request.body or {}
        start = time.monotonic()

        try:
            s3 = self._get_s3_client()

            if "/upload" in request.path:
                bucket = body.get("bucket", "")
                key = body.get("key", "")
                content_type = body.get("content_type", "application/octet-stream")
                body_base64 = body.get("body_base64", "")

                # Decode base64 body if provided, otherwise empty bytes
                file_bytes = base64.b64decode(body_base64) if body_base64 else b""

                put_kwargs: dict[str, Any] = {
                    "Bucket": bucket,
                    "Key": key,
                    "Body": file_bytes,
                    "ContentType": content_type,
                }

                response = s3.put_object(**put_kwargs)
                latency_ms = (time.monotonic() - start) * 1000
                etag = response.get("ETag", "").strip('"')

                self._circuit.record_success()
                return ProviderResponse(
                    status_code=200,
                    body={
                        "uploaded": True,
                        "bucket": bucket,
                        "key": key,
                        "etag": etag,
                        "uploaded_at": datetime.now(timezone.utc).isoformat(),
                    },
                    success=True,
                    latency_ms=latency_ms,
                )

            elif "/sign" in request.path:
                bucket = body.get("bucket", "aspire-documents")
                key = body.get("key", "unknown")
                expires_in = body.get("expires_in", 3600)

                presigned_url = s3.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": bucket, "Key": key},
                    ExpiresIn=expires_in,
                )
                latency_ms = (time.monotonic() - start) * 1000

                self._circuit.record_success()
                return ProviderResponse(
                    status_code=200,
                    body={
                        "presigned_url": presigned_url,
                        "expires_in": expires_in,
                        "bucket": bucket,
                        "key": key,
                    },
                    success=True,
                    latency_ms=latency_ms,
                )

            else:
                return ProviderResponse(
                    status_code=400,
                    body={"error": "Unknown S3 operation"},
                    success=False,
                    error_code=InternalErrorCode.INPUT_INVALID_FORMAT,
                    error_message="Unknown S3 operation path",
                )

        except ProviderError:
            raise
        except Exception as exc:
            latency_ms = (time.monotonic() - start) * 1000
            self._circuit.record_failure()

            # Map common boto3/botocore errors to appropriate codes
            exc_name = type(exc).__name__
            error_message = str(exc)[:200]

            if "NoSuchBucket" in exc_name or "NoSuchBucket" in str(exc):
                error_code = InternalErrorCode.DOMAIN_NOT_FOUND
                status_code = 404
            elif "AccessDenied" in str(exc) or "Forbidden" in str(exc):
                error_code = InternalErrorCode.AUTH_SCOPE_INSUFFICIENT
                status_code = 403
            else:
                error_code = InternalErrorCode.SERVER_INTERNAL_ERROR
                status_code = 500

            logger.error(
                "S3 boto3 error: %s %s — %s: %s",
                request.method, request.path, exc_name, error_message,
            )

            return ProviderResponse(
                status_code=status_code,
                body={"error": error_code.value, "message": error_message},
                success=False,
                error_code=error_code,
                error_message=error_message,
                latency_ms=latency_ms,
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
            },
            receipt_data=receipt,
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
            },
            receipt_data=receipt,
        )
    else:
        return ToolExecutionResult(
            outcome=Outcome.FAILED,
            tool_id="s3.url.sign",
            error=response.error_message or "S3 presign URL generation failed",
            receipt_data=receipt,
        )
