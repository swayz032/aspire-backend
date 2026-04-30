"""Ingestion adapters — every important business artifact flows into Office Memory.

Pass 14 of the Office Memory Engine plan (the-image-was-off-calm-lynx).

Each adapter:
  1. Verifies the upstream provider's webhook signature (signatures.py).
  2. Resolves tenant/suite/office scope from the payload (`called_number`,
     `customer_id`, etc. → `tenant_phone_numbers`, `provider_connections`).
  3. Resolves or upserts the `threads` row via `EntityThreadResolver`.
  4. Builds a `MemoryObjectIn` envelope per the type's contract (§14.C of plan).
  5. Calls `MemoryService.write` — receipt is cut by MemoryService (Law #2).
  6. Returns the inserted `MemoryObjectOut`.

All adapters are idempotent on the provider's webhook event ID (passed as
`idempotency_key` on `MemoryObjectIn`).
"""

from aspire_orchestrator.services.ingestion.base import (
    BaseIngestionAdapter,
    IngestionError,
    IngestionResult,
)

__all__ = [
    "BaseIngestionAdapter",
    "IngestionError",
    "IngestionResult",
]
