"""
Browser Automation Service — Hybrid Browser View (Wave 1)

Production-grade Playwright-based browser automation for screenshot capture.
Implements Aspire Law #9 (Security): SSRF prevention, domain allowlist, PII redaction.

Architecture:
- Headless Chromium via Playwright
- Screenshot upload to S3 with presigned URLs (1hr expiry), or local storage fallback
- Domain allowlist enforcement (prevents SSRF attacks)
- 30s hard timeout (Law #3: fail closed)
- Receipt generation for all navigation attempts
"""

import asyncio
import io
import logging
import os
import pathlib
import re
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from dataclasses import dataclass

try:
    import boto3
    from botocore.exceptions import ClientError
    _HAS_BOTO3 = True
except ImportError:
    _HAS_BOTO3 = False
    ClientError = Exception  # fallback for type hints

from playwright.async_api import async_playwright, Browser, Page, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)


@dataclass
class ScreenshotResult:
    """Result of browser screenshot capture"""
    screenshot_id: str
    screenshot_url: str  # S3 presigned URL
    page_url: str  # Redacted URL
    page_title: str  # PII-redacted title
    viewport_width: int
    viewport_height: int
    screenshot_bytes: bytes  # For testing/local storage
    page_load_time_ms: int


class BrowserServiceError(Exception):
    """Base exception for browser service errors"""
    pass


class DomainDeniedError(BrowserServiceError):
    """Domain not in allowlist (SSRF prevention)"""
    pass


class NavigationTimeoutError(BrowserServiceError):
    """Page navigation exceeded timeout"""
    pass


class ScreenshotUploadError(BrowserServiceError):
    """S3 screenshot upload failed"""
    pass


class BrowserService:
    """
    Browser automation service for screenshot capture.

    Security Features:
    - Domain allowlist enforcement (SSRF prevention)
    - Private IP blocking (RFC1918, localhost, AWS metadata)
    - URL validation (rejects javascript:, file:, data: protocols)
    - 30s hard timeout on navigation
    - PII redaction in page_url and page_title

    Performance Features:
    - Browser instance pooling (reuse contexts)
    - Headless mode (no GPU rendering)
    - Configurable viewport size
    """

    def __init__(
        self,
        domain_allowlist: Optional[list[str]] = None,
        s3_bucket: Optional[str] = None,
        max_timeout_ms: int = 30000,
        local_storage_dir: Optional[str] = None,
    ):
        """
        Initialize browser service.

        Args:
            domain_allowlist: Allowed domains (e.g., ["bing.com", "google.com"])
            s3_bucket: S3 bucket name for screenshot storage
            max_timeout_ms: Maximum page load timeout (default 30s)
            local_storage_dir: Directory for local screenshot storage (dev fallback)
        """
        self.domain_allowlist = domain_allowlist or self._load_domain_allowlist()
        self.s3_bucket = s3_bucket or os.getenv("BROWSER_SCREENSHOT_S3_BUCKET", "aspire-screenshots")
        self.max_timeout_ms = min(max_timeout_ms, 30000)  # Hard cap at 30s

        # S3 client initialization (lazy, graceful fallback to local storage)
        self.s3_client = None
        self._use_local_storage = False
        if _HAS_BOTO3:
            try:
                self.s3_client = boto3.client("s3")
                # Quick validation — don't fail init if credentials are bad
            except Exception:
                logger.warning("Failed to initialize S3 client — using local storage fallback")
                self._use_local_storage = True
        else:
            logger.warning("boto3 not installed — using local storage fallback")
            self._use_local_storage = True

        # Local storage fallback directory
        self._local_storage_dir = pathlib.Path(
            local_storage_dir
            or os.getenv("BROWSER_SCREENSHOT_LOCAL_DIR", "")
            or os.path.join(os.path.dirname(__file__), "..", "..", "screenshots")
        ).resolve()

        # Browser instance pool (lazy initialization)
        self._browser: Optional[Browser] = None
        self._playwright_context = None

        logger.info(
            "BrowserService initialized",
            extra={
                "domain_allowlist": self.domain_allowlist,
                "s3_bucket": self.s3_bucket,
                "max_timeout_ms": self.max_timeout_ms,
                "storage_mode": "local" if self._use_local_storage else "s3",
            }
        )

    @staticmethod
    def _load_domain_allowlist() -> list[str]:
        """Load domain allowlist from environment variable"""
        allowlist_str = os.getenv("BROWSER_DOMAIN_ALLOWLIST", "")
        if not allowlist_str:
            logger.warning("BROWSER_DOMAIN_ALLOWLIST not set — defaulting to bing.com only")
            return ["bing.com"]

        domains = [d.strip() for d in allowlist_str.split(",") if d.strip()]
        logger.info(f"Loaded {len(domains)} allowed domains", extra={"domains": domains})
        return domains

    def validate_url(self, url: str) -> None:
        """
        Validate URL against security rules.

        Security Checks:
        1. Protocol must be http/https (blocks javascript:, file:, data:)
        2. Hostname must not be private IP (RFC1918, localhost)
        3. Hostname must not be AWS metadata endpoint (169.254.169.254)
        4. Hostname must be in domain allowlist

        Args:
            url: URL to validate

        Raises:
            DomainDeniedError: If URL fails validation
        """
        try:
            parsed = urllib.parse.urlparse(url)
        except Exception as e:
            raise DomainDeniedError(f"Invalid URL format: {e}")

        # Check protocol
        if parsed.scheme not in ("http", "https"):
            raise DomainDeniedError(
                f"Invalid protocol: {parsed.scheme} (only http/https allowed)"
            )

        hostname = parsed.hostname
        if not hostname:
            raise DomainDeniedError("Missing hostname in URL")

        # Block private IPs (RFC1918)
        private_ip_patterns = [
            r"^127\.",  # localhost
            r"^10\.",  # 10.0.0.0/8
            r"^172\.(1[6-9]|2[0-9]|3[01])\.",  # 172.16.0.0/12
            r"^192\.168\.",  # 192.168.0.0/16
            r"^169\.254\.",  # AWS metadata endpoint
        ]

        for pattern in private_ip_patterns:
            if re.match(pattern, hostname):
                raise DomainDeniedError(
                    f"SSRF blocked: {hostname} is a private IP address"
                )

        # Block localhost variants
        if hostname in ("localhost", "0.0.0.0", "[::]", "::1"):
            raise DomainDeniedError(f"SSRF blocked: {hostname} is localhost")

        # Check domain allowlist
        allowed = any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in self.domain_allowlist
        )

        if not allowed:
            raise DomainDeniedError(
                f"Domain {hostname} not in allowlist. Allowed: {self.domain_allowlist}"
            )

        logger.debug(f"URL validation passed: {url}")

    @staticmethod
    def redact_url(url: str) -> str:
        """
        Redact PII from URL (strip query parameters and fragments).

        Example: https://bing.com/search?q=john+doe+ssn -> https://bing.com/search
        """
        parsed = urllib.parse.urlparse(url)
        return urllib.parse.urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            "",  # params (removed)
            "",  # query (removed)
            ""   # fragment (removed)
        ))

    @staticmethod
    def redact_page_title(title: str) -> str:
        """
        Redact potential PII from page title.

        Currently: Basic redaction of common PII patterns.
        Future: Integrate with Presidio DLP for comprehensive redaction.
        """
        # Redact email addresses
        title = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '<EMAIL_REDACTED>', title)

        # Redact phone numbers (simple pattern)
        title = re.sub(r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b', '<PHONE_REDACTED>', title)

        # Redact SSN pattern
        title = re.sub(r'\b\d{3}-\d{2}-\d{4}\b', '<SSN_REDACTED>', title)

        return title

    async def _ensure_browser(self) -> Browser:
        """Ensure browser instance is initialized (lazy initialization)"""
        if self._browser:
            return self._browser

        logger.info("Initializing Playwright browser (headless Chromium)")
        self._playwright_context = await async_playwright().start()
        self._browser = await self._playwright_context.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
            ]
        )
        logger.info("Playwright browser initialized")
        return self._browser

    async def navigate_and_screenshot(
        self,
        url: str,
        screenshot_id: str,
        suite_id: str,
        viewport_width: int = 1280,
        viewport_height: int = 800,
    ) -> ScreenshotResult:
        """
        Navigate to URL and capture screenshot.

        Args:
            url: Target URL (must pass domain allowlist)
            screenshot_id: Unique ID for screenshot (UUID from caller)
            suite_id: Tenant ID (for S3 path scoping)
            viewport_width: Browser viewport width
            viewport_height: Browser viewport height

        Returns:
            ScreenshotResult with screenshot URL and metadata

        Raises:
            DomainDeniedError: URL failed validation
            NavigationTimeoutError: Page load timeout (>30s)
            ScreenshotUploadError: S3 upload failed
        """
        start_time = datetime.utcnow()

        # Step 1: Validate URL (SSRF prevention)
        self.validate_url(url)

        # Step 2: Initialize browser
        browser = await self._ensure_browser()
        context = await browser.new_context(
            viewport={"width": viewport_width, "height": viewport_height},
            java_script_enabled=True,
            ignore_https_errors=False,  # Enforce HTTPS validation
        )

        page: Optional[Page] = None
        try:
            page = await context.new_page()

            # Step 3: Navigate with timeout
            logger.info(f"Navigating to {url} (timeout={self.max_timeout_ms}ms)")
            try:
                await page.goto(url, timeout=self.max_timeout_ms, wait_until="networkidle")
            except PlaywrightTimeout:
                raise NavigationTimeoutError(
                    f"Page navigation timeout after {self.max_timeout_ms}ms"
                )

            # Step 4: Capture screenshot
            screenshot_bytes = await page.screenshot(type="png", full_page=False)
            page_title_raw = await page.title()

            # Step 5: Redact PII
            page_url_redacted = self.redact_url(url)
            page_title_redacted = self.redact_page_title(page_title_raw)

            # Step 6: Upload (S3 or local fallback)
            screenshot_url = await self._upload_screenshot(
                screenshot_bytes=screenshot_bytes,
                screenshot_id=screenshot_id,
                suite_id=suite_id,
            )

            page_load_time_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)

            logger.info(
                f"Screenshot captured successfully",
                extra={
                    "screenshot_id": screenshot_id,
                    "page_url": page_url_redacted,
                    "page_load_time_ms": page_load_time_ms,
                }
            )

            return ScreenshotResult(
                screenshot_id=screenshot_id,
                screenshot_url=screenshot_url,
                page_url=page_url_redacted,
                page_title=page_title_redacted,
                viewport_width=viewport_width,
                viewport_height=viewport_height,
                screenshot_bytes=screenshot_bytes,
                page_load_time_ms=page_load_time_ms,
            )

        finally:
            if page:
                await page.close()
            await context.close()

    async def _upload_screenshot(
        self,
        screenshot_bytes: bytes,
        screenshot_id: str,
        suite_id: str,
    ) -> str:
        """
        Upload screenshot and return URL.

        Uses S3 in production (presigned URL, 1hr expiry).
        Falls back to local file storage in dev (served by FastAPI).

        Args:
            screenshot_bytes: PNG screenshot data
            screenshot_id: Screenshot UUID
            suite_id: Tenant ID (for path scoping)

        Returns:
            URL to access screenshot (S3 presigned or local file path)

        Raises:
            ScreenshotUploadError: Upload failed
        """
        if self._use_local_storage or self.s3_client is None:
            return self._save_screenshot_locally(screenshot_bytes, screenshot_id, suite_id)
        else:
            return self._upload_screenshot_to_s3(screenshot_bytes, screenshot_id, suite_id)

    def _save_screenshot_locally(
        self,
        screenshot_bytes: bytes,
        screenshot_id: str,
        suite_id: str,
    ) -> str:
        """Save screenshot to local filesystem (dev fallback)."""
        suite_dir = self._local_storage_dir / suite_id
        suite_dir.mkdir(parents=True, exist_ok=True)

        file_path = suite_dir / f"{screenshot_id}.png"
        file_path.write_bytes(screenshot_bytes)

        # Return local URL path (served by FastAPI static mount)
        local_url = f"/screenshots/{suite_id}/{screenshot_id}.png"
        logger.info(f"Screenshot saved locally: {file_path}")
        return local_url

    def _upload_screenshot_to_s3(
        self,
        screenshot_bytes: bytes,
        screenshot_id: str,
        suite_id: str,
    ) -> str:
        """Upload screenshot to S3 and return presigned URL."""
        s3_key = f"{suite_id}/{screenshot_id}.png"

        try:
            logger.debug(f"Uploading screenshot to s3://{self.s3_bucket}/{s3_key}")
            self.s3_client.put_object(
                Bucket=self.s3_bucket,
                Key=s3_key,
                Body=screenshot_bytes,
                ContentType="image/png",
                Metadata={
                    "screenshot_id": screenshot_id,
                    "suite_id": suite_id,
                    "uploaded_at": datetime.utcnow().isoformat(),
                }
            )

            presigned_url = self.s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.s3_bucket, "Key": s3_key},
                ExpiresIn=3600,
            )

            logger.info(f"Screenshot uploaded successfully: {s3_key}")
            return presigned_url

        except ClientError as e:
            # Fallback to local storage on S3 failure
            logger.warning(f"S3 upload failed, falling back to local storage: {e}")
            self._use_local_storage = True
            return self._save_screenshot_locally(screenshot_bytes, screenshot_id, suite_id)

    async def close(self):
        """Close browser and cleanup resources"""
        if self._browser:
            await self._browser.close()
            self._browser = None

        if self._playwright_context:
            await self._playwright_context.stop()
            self._playwright_context = None

        logger.info("BrowserService closed")


# Singleton instance (initialized on first import)
_browser_service: Optional[BrowserService] = None


def get_browser_service() -> BrowserService:
    """Get or create singleton BrowserService instance"""
    global _browser_service
    if _browser_service is None:
        _browser_service = BrowserService()
    return _browser_service
