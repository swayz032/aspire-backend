"""Wave 5.1b-1 — verify service visibility scope, service surfaces, property_thread.

Tests confirm:
- VisibilityScope literal accepts "service" and rejects unknown values
- 8 new Service Hub source surfaces validate
- ThreadType accepts "property_thread"
- Existing office/finance scopes still validate (regression check)
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from aspire_orchestrator.schemas.memory_v1 import (
    MemoryObjectIn,
    Provenance,
    ScopedIdentity,
    ThreadIn,
)


TENANT = uuid.uuid4()
SUITE = uuid.uuid4()
OFFICE = uuid.uuid4()
TRACE = uuid.uuid4()
CORR = uuid.uuid4()


def _scope() -> ScopedIdentity:
    return ScopedIdentity(
        tenant_id=TENANT,
        suite_id=SUITE,
        office_id=OFFICE,
        actor_id=uuid.uuid4(),
    )


def _provenance() -> Provenance:
    return Provenance(
        trace_id=TRACE,
        correlation_id=CORR,
        source_surface="internal_drew",
    )


class TestVisibilityScopeService:
    """VisibilityScope literal accepts 'service' and rejects bad values."""

    def test_visibility_scope_accepts_service(self) -> None:
        memory = MemoryObjectIn(
            scope=_scope(),
            provenance=_provenance(),
            memory_type="decision_fact",
            summary="Drew picked Ferguson for 1/2 PVC sched 40",
            visibility_scope="service",
        )
        assert memory.visibility_scope == "service"

    def test_visibility_scope_rejects_unknown(self) -> None:
        with pytest.raises(ValidationError):
            MemoryObjectIn(
                scope=_scope(),
                provenance=_provenance(),
                memory_type="decision_fact",
                summary="invalid scope",
                visibility_scope="bogus",
            )

    def test_visibility_scope_rejects_none(self) -> None:
        with pytest.raises(ValidationError):
            MemoryObjectIn(
                scope=_scope(),
                provenance=_provenance(),
                memory_type="decision_fact",
                summary="none scope",
                visibility_scope=None,
            )

    @pytest.mark.parametrize("scope_value", ["office", "finance"])
    def test_office_and_finance_scopes_still_work(self, scope_value: str) -> None:
        """Regression: existing scopes must keep validating."""
        memory = MemoryObjectIn(
            scope=_scope(),
            provenance=_provenance(),
            memory_type="decision_fact",
            summary=f"regression check for {scope_value} scope",
            visibility_scope=scope_value,
        )
        assert memory.visibility_scope == scope_value


SERVICE_SURFACES = [
    "service_hub_estimate_studio",
    "service_hub_jobs",
    "service_hub_dispatch",
    "service_hub_scheduling",
    "service_hub_inspections",
    "internal_drew",
    "elevenlabs_tim_service",
    "anam_tim_service",
]


class TestSourceSurfaceServiceHub:
    """8 new Service Hub source surfaces validate; unknown surface rejected."""

    @pytest.mark.parametrize("surface", SERVICE_SURFACES)
    def test_source_surface_accepts_service_hub_surfaces(self, surface: str) -> None:
        prov = Provenance(
            trace_id=TRACE,
            correlation_id=CORR,
            source_surface=surface,
        )
        assert prov.source_surface == surface

    def test_source_surface_rejects_unknown(self) -> None:
        with pytest.raises(ValidationError):
            Provenance(
                trace_id=TRACE,
                correlation_id=CORR,
                source_surface="drew_unknown_surface",
            )


class TestThreadTypePropertyThread:
    """ThreadType accepts 'property_thread' (new) without breaking existing types."""

    def test_thread_type_accepts_property_thread(self) -> None:
        thread = ThreadIn(
            tenant_id=TENANT,
            suite_id=SUITE,
            office_id=OFFICE,
            thread_type="property_thread",
        )
        assert thread.thread_type == "property_thread"

    @pytest.mark.parametrize("thread_type", ["project_thread", "job_thread"])
    def test_thread_type_existing_service_threads_still_work(
        self, thread_type: str
    ) -> None:
        """Regression: project_thread and job_thread were already present."""
        thread = ThreadIn(
            tenant_id=TENANT,
            suite_id=SUITE,
            office_id=OFFICE,
            thread_type=thread_type,
        )
        assert thread.thread_type == thread_type
