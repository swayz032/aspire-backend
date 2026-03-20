from __future__ import annotations

import json
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKER_ROOT = REPO_ROOT / "infrastructure" / "docker"


def test_prod_compose_declares_required_runtime_env() -> None:
    data = yaml.safe_load((DOCKER_ROOT / "docker-compose.orchestrator-safety.prod.yml").read_text())
    env = data["services"]["aspire-orchestrator"]["environment"]

    for key in (
        "ASPIRE_ENV",
        "NODE_ENV",
        "ASPIRE_REDIS_URL",
        "ASPIRE_METRICS_TOKEN",
        "SENTRY_DSN",
        "SENTRY_ORG",
        "SENTRY_PROJECTS",
        "SENTRY_AUTH_TOKEN",
    ):
        assert key in env


def test_railway_uses_port_aware_launcher() -> None:
    data = json.loads((REPO_ROOT / "orchestrator" / "railway.json").read_text())
    assert data["deploy"]["startCommand"] == "python -m aspire_orchestrator.launch"


def test_prometheus_scrapes_orchestrator_metrics() -> None:
    prometheus_config = (DOCKER_ROOT / "otel" / "prometheus.yml").read_text()
    assert "aspire-orchestrator:8000" in prometheus_config


def test_n8n_requires_encryption_key() -> None:
    data = yaml.safe_load((DOCKER_ROOT / "docker-compose.n8n.yml").read_text())
    env = data["services"]["n8n"]["environment"]
    assert "N8N_ENCRYPTION_KEY" in env
