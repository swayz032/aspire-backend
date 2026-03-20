"""Temporal client singleton — connection management with codec and interceptors.

Env-aware: connects to local Temporal in dev, AWS Temporal in production.
Enhancement #6: PayloadCodec wired into DataConverter for encryption at rest.
"""

from __future__ import annotations

import logging
from typing import Any

from temporalio.client import Client
from temporalio.converter import DataConverter

from aspire_orchestrator.temporal.codec import create_payload_codec
from aspire_orchestrator.temporal.config import get_namespace, get_temporal_target
from aspire_orchestrator.temporal.interceptors import create_interceptors

logger = logging.getLogger(__name__)

_client: Client | None = None


async def get_temporal_client() -> Client:
    """Get or create the Temporal client singleton.

    Uses lazy initialization. Thread-safe via asyncio (single event loop).
    """
    global _client
    if _client is not None:
        return _client

    target = get_temporal_target()
    namespace = get_namespace()

    logger.info("Connecting to Temporal: target=%s namespace=%s", target, namespace)

    codec = create_payload_codec()
    data_converter = DataConverter(payload_codec=codec)
    interceptors = create_interceptors()

    _client = await Client.connect(
        target,
        namespace=namespace,
        data_converter=data_converter,
        interceptors=interceptors,
    )

    logger.info("Temporal client connected: namespace=%s", namespace)
    return _client


async def close_temporal_client() -> None:
    """Close the Temporal client connection (for graceful shutdown)."""
    global _client
    if _client is not None:
        # Client doesn't have an explicit close — just clear the reference
        _client = None
        logger.info("Temporal client reference cleared")


def reset_client() -> None:
    """Reset client for testing purposes."""
    global _client
    _client = None
