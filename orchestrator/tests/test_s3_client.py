"""S3 Provider Client Tests — boto3 implementation via moto.

Validates:
- Real S3 upload via put_object (returns etag)
- Presigned URL generation via generate_presigned_url
- Missing credentials → auth error receipt
- Non-existent bucket → error response
- Receipt emission for all outcomes (Law #2)
"""

from __future__ import annotations

import base64
import os
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from aspire_orchestrator.models import Outcome
from aspire_orchestrator.providers.base_client import ProviderRequest
from aspire_orchestrator.providers.s3_client import (
    S3Client,
    _get_client,
    execute_s3_document_upload,
    execute_s3_url_sign,
)

# =============================================================================
# Constants
# =============================================================================

TEST_BUCKET = "aspire-documents-test"
TEST_KEY = "tenants/t-123/docs/invoice.pdf"
TEST_REGION = "us-east-1"
TEST_SUITE_ID = "suite-test-001"
TEST_CORRELATION_ID = "corr-test-001"
TEST_OFFICE_ID = "office-test-001"


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Reset module-level singleton between tests."""
    import aspire_orchestrator.providers.s3_client as mod

    mod._client = None
    yield
    mod._client = None


@pytest.fixture()
def aws_env():
    """Set fake AWS credentials for moto."""
    env = {
        "ASPIRE_AWS_ACCESS_KEY_ID": "testing",
        "ASPIRE_AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SECURITY_TOKEN": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": TEST_REGION,
    }
    with patch.dict(os.environ, env):
        yield


@pytest.fixture()
def settings_with_creds():
    """Patch settings to have AWS credentials."""
    with patch(
        "aspire_orchestrator.providers.s3_client.settings"
    ) as mock_settings:
        mock_settings.aws_access_key_id = "testing"
        mock_settings.aws_secret_access_key = "testing"
        mock_settings.aws_s3_region = TEST_REGION
        yield mock_settings


@pytest.fixture()
def settings_no_creds():
    """Patch settings to have empty AWS credentials."""
    with patch(
        "aspire_orchestrator.providers.s3_client.settings"
    ) as mock_settings:
        mock_settings.aws_access_key_id = ""
        mock_settings.aws_secret_access_key = ""
        mock_settings.aws_s3_region = TEST_REGION
        yield mock_settings


@pytest.fixture()
def s3_bucket(aws_env, settings_with_creds):
    """Create a mocked S3 bucket for testing."""
    with mock_aws():
        s3 = boto3.client("s3", region_name=TEST_REGION)
        s3.create_bucket(Bucket=TEST_BUCKET)
        yield s3


# =============================================================================
# Tests
# =============================================================================


@pytest.mark.asyncio
async def test_s3_upload_success(s3_bucket):
    """Upload succeeds and returns etag from S3."""
    client = S3Client()
    # Force boto3 client to use moto's mocked client
    client._boto3_client = s3_bucket

    file_content = b"Hello, Aspire document!"
    body_b64 = base64.b64encode(file_content).decode()

    request = ProviderRequest(
        method="PUT",
        path="/upload",
        body={
            "bucket": TEST_BUCKET,
            "key": TEST_KEY,
            "content_type": "application/pdf",
            "body_base64": body_b64,
        },
        correlation_id=TEST_CORRELATION_ID,
        suite_id=TEST_SUITE_ID,
        office_id=TEST_OFFICE_ID,
    )

    response = await client._request(request)

    assert response.success is True
    assert response.status_code == 200
    assert response.body["uploaded"] is True
    assert response.body["bucket"] == TEST_BUCKET
    assert response.body["key"] == TEST_KEY
    assert response.body["etag"] != ""
    assert "uploaded_at" in response.body

    # Verify object actually exists in S3
    obj = s3_bucket.get_object(Bucket=TEST_BUCKET, Key=TEST_KEY)
    assert obj["Body"].read() == file_content


@pytest.mark.asyncio
async def test_s3_upload_missing_credentials(settings_no_creds):
    """Missing AWS credentials returns auth error receipt."""
    client = S3Client()

    request = ProviderRequest(
        method="PUT",
        path="/upload",
        body={
            "bucket": TEST_BUCKET,
            "key": TEST_KEY,
            "content_type": "application/pdf",
            "body_base64": "",
        },
        correlation_id=TEST_CORRELATION_ID,
        suite_id=TEST_SUITE_ID,
        office_id=TEST_OFFICE_ID,
    )

    response = await client._request(request)

    assert response.success is False
    assert response.status_code == 401
    assert "AUTH_INVALID_KEY" in str(response.error_code.value)
    assert "AWS credentials not configured" in response.error_message


@pytest.mark.asyncio
async def test_s3_presign_url_generation(s3_bucket):
    """Presigned URL is generated correctly for an S3 object."""
    # First upload an object so it exists
    s3_bucket.put_object(
        Bucket=TEST_BUCKET, Key=TEST_KEY, Body=b"test content"
    )

    client = S3Client()
    client._boto3_client = s3_bucket

    request = ProviderRequest(
        method="GET",
        path="/sign",
        body={
            "bucket": TEST_BUCKET,
            "key": TEST_KEY,
            "expires_in": 1800,
        },
        correlation_id=TEST_CORRELATION_ID,
        suite_id=TEST_SUITE_ID,
        office_id=TEST_OFFICE_ID,
    )

    response = await client._request(request)

    assert response.success is True
    assert response.status_code == 200
    assert "presigned_url" in response.body
    assert response.body["presigned_url"] != ""
    assert response.body["bucket"] == TEST_BUCKET
    assert response.body["key"] == TEST_KEY
    assert response.body["expires_in"] == 1800


@pytest.mark.asyncio
async def test_s3_upload_returns_receipt(s3_bucket):
    """Upload via execute function produces receipt data."""
    # Inject moto client into the singleton
    import aspire_orchestrator.providers.s3_client as mod

    mod._client = S3Client()
    mod._client._boto3_client = s3_bucket

    file_content = b"Receipt test PDF"
    body_b64 = base64.b64encode(file_content).decode()

    result = await execute_s3_document_upload(
        payload={
            "bucket": TEST_BUCKET,
            "key": TEST_KEY,
            "content_type": "application/pdf",
            "body_base64": body_b64,
        },
        correlation_id=TEST_CORRELATION_ID,
        suite_id=TEST_SUITE_ID,
        office_id=TEST_OFFICE_ID,
        risk_tier="yellow",
    )

    assert result.outcome == Outcome.SUCCESS
    assert result.tool_id == "s3.document.upload"
    assert result.receipt_data is not None
    assert result.receipt_data["outcome"] == "success"
    assert result.receipt_data["tool_used"] == "s3.document.upload"
    assert result.receipt_data["risk_tier"] == "yellow"
    assert result.receipt_data["correlation_id"] == TEST_CORRELATION_ID
    assert result.receipt_data["suite_id"] == TEST_SUITE_ID
    assert result.data["uploaded"] is True
    assert result.data["etag"] != ""


@pytest.mark.asyncio
async def test_s3_bucket_not_found_error(aws_env, settings_with_creds):
    """Non-existent bucket returns error response."""
    with mock_aws():
        # Create a boto3 client but do NOT create the bucket
        s3 = boto3.client("s3", region_name=TEST_REGION)

        client = S3Client()
        client._boto3_client = s3

        request = ProviderRequest(
            method="PUT",
            path="/upload",
            body={
                "bucket": "this-bucket-does-not-exist",
                "key": TEST_KEY,
                "content_type": "application/pdf",
                "body_base64": base64.b64encode(b"test").decode(),
            },
            correlation_id=TEST_CORRELATION_ID,
            suite_id=TEST_SUITE_ID,
            office_id=TEST_OFFICE_ID,
        )

        response = await client._request(request)

        assert response.success is False
        assert response.status_code in (404, 500)
        assert response.error_code is not None
