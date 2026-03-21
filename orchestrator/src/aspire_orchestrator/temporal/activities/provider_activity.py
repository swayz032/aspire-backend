"""Provider activities — execute provider calls with Temporal guarantees.

Enhancement #3: Heartbeat calls before/after HTTP calls.
Enhancement #8: Async activity completion for webhook-based providers.
"""

from __future__ import annotations

import logging
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from aspire_orchestrator.temporal.models import (
    AsyncProviderCallInput,
    ProviderCallInput,
    ProviderCallOutput,
)

logger = logging.getLogger(__name__)


@activity.defn
async def execute_provider_call(input: ProviderCallInput) -> ProviderCallOutput:
    """Execute a synchronous provider call with heartbeat monitoring.

    Enhancement #3: Heartbeat before/after HTTP call.
    Maps ProviderError.category to non-retryable ApplicationError for
    AUTH/INPUT/DOMAIN errors (Enhancement #10).
    """
    from aspire_orchestrator.providers.error_codes import ProviderErrorCategory
    from aspire_orchestrator.services.tool_executor import execute_tool

    # Enhancement #3: Heartbeat before provider call
    activity.heartbeat({
        "phase": "provider_call_start",
        "provider": input.provider,
        "action": input.action,
    })

    try:
        result = await execute_tool(
            tool_name=f"{input.provider}_{input.action}",
            params={
                **input.payload,
                "suite_id": input.suite_id,
                "office_id": input.office_id,
                "correlation_id": input.correlation_id,
                "idempotency_key": input.idempotency_key,
                "capability_token_id": input.capability_token_id,
            },
        )

        # Enhancement #3: Heartbeat after successful call
        activity.heartbeat({
            "phase": "provider_call_complete",
            "provider": input.provider,
            "action": input.action,
        })

        return ProviderCallOutput(
            success=True,
            provider=input.provider,
            action=input.action,
            result=result if isinstance(result, dict) else {"raw": str(result)},
        )

    except Exception as e:
        activity.heartbeat({
            "phase": "provider_call_error",
            "provider": input.provider,
            "error": str(e)[:200],
        })

        # Enhancement #10: Map provider errors to Temporal non-retryable types
        error_category = _classify_error(e)
        if error_category in (
            ProviderErrorCategory.AUTH,
            ProviderErrorCategory.INPUT,
            ProviderErrorCategory.DOMAIN,
        ):
            raise ApplicationError(
                str(e),
                type=f"{error_category.value.title()}Error",
                non_retryable=True,
            ) from e

        # Retryable errors (NETWORK, RATE, SERVER) — let Temporal retry
        raise


@activity.defn
async def execute_webhook_provider_call(input: AsyncProviderCallInput) -> None:
    """Start a provider action and raise CompleteAsync for webhook completion.

    Enhancement #8: Activity starts the provider action, saves the task token,
    then raises CompleteAsync. The webhook handler completes the activity
    externally using the saved task token.
    """
    from aspire_orchestrator.services.supabase_client import supabase_insert
    from aspire_orchestrator.services.tool_executor import execute_tool

    task_token = activity.info().task_token

    activity.heartbeat({
        "phase": "async_provider_init",
        "provider": input.provider,
        "action": input.action,
    })

    # Save task token for webhook handler to complete later
    await supabase_insert(
        "temporal_task_tokens",
        {
            "provider": input.provider,
            "ref_id": f"{input.provider}:{input.correlation_id}",
            "task_token": task_token.hex(),
            "suite_id": input.suite_id,
            "correlation_id": input.correlation_id,
        },
    )

    # Start the provider action (e.g., create invoice, send for signature)
    await execute_tool(
        tool_name=f"{input.provider}_{input.action}",
        params={
            **input.payload,
            "suite_id": input.suite_id,
            "office_id": input.office_id,
            "correlation_id": input.correlation_id,
            "callback_url": input.callback_url,
        },
    )

    # Enhancement #8: Raise CompleteAsync — webhook will complete this activity
    activity.raise_complete_async()


def _classify_error(error: Exception) -> Any:
    """Classify an exception into ProviderErrorCategory."""
    from aspire_orchestrator.providers.error_codes import ProviderErrorCategory

    # Check if it's a ProviderError with a category
    if hasattr(error, "category"):
        return error.category
    if hasattr(error, "error_code") and hasattr(error.error_code, "category"):
        return error.error_code.category

    # Law #3: Fail closed — unknown exceptions must NOT retry (non-retryable INPUT)
    logger.warning(
        "Unknown exception type %s classified as INPUT (non-retryable) — Law #3 fail-closed",
        type(error).__name__,
    )
    return ProviderErrorCategory.INPUT
