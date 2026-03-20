"""Read-only Sentry API client for Admin sync.

Returns lightweight issue summaries and deep links without mirroring raw events.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = httpx.Timeout(5.0, connect=2.0)
_DEFAULT_CACHE_TTL_SECONDS = 45.0


def _parse_int(value: str | None, default: int) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_float(value: str | None, default: float) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_projects(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _configured_projects() -> list[str]:
    projects: list[str] = []
    for env_name in (
        "SENTRY_PROJECT_SLUGS",
        "SENTRY_PROJECTS",
    ):
        projects.extend(_parse_projects((os.getenv(env_name) or "").strip()))

    for env_name in (
        "SENTRY_PROJECT_BACKEND",
        "SENTRY_PROJECT_ADMIN_PORTAL",
        "SENTRY_PROJECT_DESKTOP_SERVER",
        "SENTRY_PROJECT_DESKTOP_CLIENT",
        "SENTRY_PROJECT",
    ):
        value = (os.getenv(env_name) or "").strip()
        if value:
            projects.append(value)

    deduped: list[str] = []
    seen: set[str] = set()
    for project in projects:
        if project in seen:
            continue
        seen.add(project)
        deduped.append(project)
    return deduped


def _build_api_base_url() -> str:
    explicit = (os.getenv("SENTRY_API_BASE_URL") or "").strip().rstrip("/")
    if explicit:
        return explicit

    host = (os.getenv("SENTRY_URL") or os.getenv("SENTRY_BASE_URL") or "https://sentry.io").strip().rstrip("/")
    return f"{host}/api/0"


def _build_ui_base_url(api_base_url: str) -> str:
    if api_base_url.endswith("/api/0"):
        return api_base_url[: -len("/api/0")]
    return api_base_url


class SentryReadService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._summary_cache: tuple[float, dict[str, Any]] | None = None
        self._issues_cache: dict[int, tuple[float, dict[str, Any]]] = {}

    def _config(self) -> dict[str, Any]:
        api_base_url = _build_api_base_url()
        organization = (
            (os.getenv("SENTRY_ORG_SLUG") or "").strip()
            or (os.getenv("SENTRY_ORG") or "").strip()
        )
        token = (os.getenv("SENTRY_AUTH_TOKEN") or "").strip()
        projects = _configured_projects()
        enabled = bool(organization and token)
        return {
            "enabled": enabled,
            "api_base_url": api_base_url,
            "ui_base_url": _build_ui_base_url(api_base_url),
            "organization": organization or None,
            "token": token,
            "projects": projects,
            "cache_ttl_seconds": max(_parse_float(os.getenv("SENTRY_CACHE_TTL_SECONDS"), _DEFAULT_CACHE_TTL_SECONDS), 0.0),
            "summary_limit": max(_parse_int(os.getenv("SENTRY_SUMMARY_LIMIT"), 25), 1),
        }

    def _is_fresh(self, expires_at: float) -> bool:
        return expires_at > time.monotonic()

    def _issues_query(self) -> str:
        return "is:unresolved"

    def _issues_url(self, config: dict[str, Any]) -> str | None:
        organization = config.get("organization")
        if not organization:
            return None
        return f"{config['ui_base_url']}/organizations/{organization}/issues/"

    def _alerts_url(self, config: dict[str, Any]) -> str | None:
        organization = config.get("organization")
        if not organization:
            return None
        return f"{config['ui_base_url']}/organizations/{organization}/alerts/rules/"

    def _disabled_summary(self, config: dict[str, Any]) -> dict[str, Any]:
        return {
            "configured": False,
            "source": "disabled",
            "status": "disabled",
            "open_issue_count": 0,
            "critical_count": 0,
            "regression_count": 0,
            "project_count": len(config.get("projects", [])),
            "last_seen": None,
            "issues_url": self._issues_url(config),
            "alerts_url": self._alerts_url(config),
            "warnings": ["Sentry admin sync is disabled because SENTRY_AUTH_TOKEN or SENTRY_ORG is missing."],
        }

    def _disabled_issues(self, config: dict[str, Any]) -> dict[str, Any]:
        return {
            "items": [],
            "count": 0,
            "configured": False,
            "source": "disabled",
            "warnings": self._disabled_summary(config)["warnings"],
        }

    def _normalize_issue(self, raw: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
        project = raw.get("project") if isinstance(raw.get("project"), dict) else {}
        project_slug = str(project.get("slug") or project.get("name") or "")
        project_name = str(project.get("name") or project_slug or "unknown")

        permalink = str(raw.get("permalink") or "").strip()
        if not permalink and raw.get("id"):
            permalink = f"{config['ui_base_url']}/organizations/{config['organization']}/issues/{raw['id']}/"

        return {
            "id": str(raw.get("id") or raw.get("shortId") or ""),
            "short_id": str(raw.get("shortId") or ""),
            "title": str(raw.get("title") or raw.get("culprit") or "Untitled Sentry issue"),
            "level": str(raw.get("level") or "error").lower(),
            "status": str(raw.get("status") or "unresolved"),
            "count": _parse_int(str(raw.get("count") or "0"), 0),
            "user_count": _parse_int(str(raw.get("userCount") or "0"), 0),
            "first_seen": str(raw.get("firstSeen")) if raw.get("firstSeen") else None,
            "last_seen": str(raw.get("lastSeen")) if raw.get("lastSeen") else None,
            "project_slug": project_slug,
            "project_name": project_name,
            "culprit": str(raw.get("culprit") or ""),
            "permalink": permalink,
            "is_regression": bool(raw.get("isRegression")),
            "is_unhandled": bool(raw.get("isUnhandled")),
        }

    async def _fetch_project_issues(
        self,
        *,
        client: httpx.AsyncClient,
        project: str,
        limit: int,
        config: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], int]:
        headers = {
            "Authorization": f"Bearer {config['token']}",
            "Accept": "application/json",
        }
        params = {
            "query": self._issues_query(),
            "sort": "date",
            "limit": str(limit),
        }

        response = await client.get(
            f"{config['api_base_url']}/projects/{config['organization']}/{project}/issues/",
            headers=headers,
            params=params,
        )
        response.raise_for_status()
        payload = response.json()

        if not isinstance(payload, list):
            raise ValueError("Unexpected Sentry issues payload")

        total_hits = _parse_int(response.headers.get("X-Hits"), len(payload))
        issues = [self._normalize_issue(item, config) for item in payload if isinstance(item, dict)]
        return issues, total_hits

    async def _fetch_unresolved_issues(self, *, limit: int, config: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
        projects = config.get("projects", [])
        if not projects:
            return [], 0

        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            results = await asyncio.gather(
                *[
                    self._fetch_project_issues(
                        client=client,
                        project=project,
                        limit=limit,
                        config=config,
                    )
                    for project in projects
                ]
            )

        issues: list[dict[str, Any]] = []
        total_hits = 0
        for project_issues, project_hits in results:
            total_hits += project_hits
            issues.extend(project_issues)

        issues.sort(
            key=lambda issue: issue.get("last_seen") or issue.get("first_seen") or "",
            reverse=True,
        )
        return issues[:limit], total_hits

    def _unavailable_summary(self, config: dict[str, Any], warning: str) -> dict[str, Any]:
        return {
            "configured": True,
            "source": "unavailable",
            "status": "unavailable",
            "open_issue_count": 0,
            "critical_count": 0,
            "regression_count": 0,
            "project_count": len(config.get("projects", [])),
            "last_seen": None,
            "issues_url": self._issues_url(config),
            "alerts_url": self._alerts_url(config),
            "warnings": [warning],
        }

    def _unavailable_issues(self, warning: str) -> dict[str, Any]:
        return {
            "items": [],
            "count": 0,
            "configured": True,
            "source": "unavailable",
            "warnings": [warning],
        }

    async def get_summary(self) -> dict[str, Any]:
        config = self._config()
        if not config["enabled"]:
            return self._disabled_summary(config)

        async with self._lock:
            if self._summary_cache and self._is_fresh(self._summary_cache[0]):
                return self._summary_cache[1]

            try:
                issues, total_hits = await self._fetch_unresolved_issues(limit=config["summary_limit"], config=config)
            except Exception as exc:
                warning = f"Sentry summary unavailable: {exc}"
                logger.warning(warning)
                summary = self._unavailable_summary(config, warning)
                self._summary_cache = (time.monotonic() + config["cache_ttl_seconds"], summary)
                return summary

            critical_count = sum(1 for issue in issues if issue["level"] in {"fatal", "error"})
            regression_count = sum(1 for issue in issues if issue["is_regression"])
            project_count = len({issue["project_slug"] for issue in issues if issue["project_slug"]})
            last_seen = next((issue["last_seen"] for issue in issues if issue["last_seen"]), None)

            status = "healthy"
            if critical_count > 0 or regression_count > 0:
                status = "critical"
            elif total_hits > 0:
                status = "degraded"

            summary = {
                "configured": True,
                "source": "sentry",
                "status": status,
                "open_issue_count": total_hits,
                "critical_count": critical_count,
                "regression_count": regression_count,
                "project_count": project_count,
                "last_seen": last_seen,
                "issues_url": self._issues_url(config),
                "alerts_url": self._alerts_url(config),
                "warnings": [],
            }
            self._summary_cache = (time.monotonic() + config["cache_ttl_seconds"], summary)
            return summary

    async def get_issues(self, *, limit: int = 10) -> dict[str, Any]:
        config = self._config()
        if not config["enabled"]:
            return self._disabled_issues(config)

        safe_limit = max(limit, 1)

        async with self._lock:
            cached = self._issues_cache.get(safe_limit)
            if cached and self._is_fresh(cached[0]):
                return cached[1]

            try:
                issues, total_hits = await self._fetch_unresolved_issues(limit=safe_limit, config=config)
            except Exception as exc:
                warning = f"Sentry issues unavailable: {exc}"
                logger.warning(warning)
                payload = self._unavailable_issues(warning)
                self._issues_cache[safe_limit] = (time.monotonic() + config["cache_ttl_seconds"], payload)
                return payload

            payload = {
                "items": issues,
                "count": total_hits,
                "configured": True,
                "source": "sentry",
                "warnings": [],
            }
            self._issues_cache[safe_limit] = (time.monotonic() + config["cache_ttl_seconds"], payload)
            return payload

    def reset(self) -> None:
        self._summary_cache = None
        self._issues_cache.clear()


_service: SentryReadService | None = None


def get_sentry_read_service() -> SentryReadService:
    global _service
    if _service is None:
        _service = SentryReadService()
    return _service


def reset_sentry_read_service() -> None:
    global _service
    if _service is not None:
        _service.reset()
    _service = None
