"""JSON Schema Validator Service — Proposal + Receipt validation.

Validates Finn Finance Manager outputs against:
  - 06_output_schema.json (proposal format)
  - receipt_event.schema.json (receipt events)

Fail-closed (Law #3): invalid schema → deny + receipt.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema

logger = logging.getLogger(__name__)

_SCHEMAS_DIR = Path(__file__).parent.parent / "schemas"


@dataclass(frozen=True)
class ValidationResult:
    """Result of JSON schema validation."""

    valid: bool
    errors: list[str]

    @property
    def error_count(self) -> int:
        return len(self.errors)


def _load_schema(schema_name: str) -> dict[str, Any]:
    """Load a JSON schema from the schemas directory.

    Raises FileNotFoundError if schema file is missing (fail-closed).
    """
    schema_path = _SCHEMAS_DIR / schema_name
    if not schema_path.exists():
        raise FileNotFoundError(
            f"Schema not found: {schema_path}. "
            "Fail-closed: cannot validate without schema (Law #3)."
        )
    with open(schema_path, encoding="utf-8") as f:
        return json.load(f)


# Cached schemas (loaded once)
_output_schema: dict[str, Any] | None = None
_receipt_schema: dict[str, Any] | None = None


def _get_output_schema() -> dict[str, Any]:
    global _output_schema
    if _output_schema is None:
        _output_schema = _load_schema("06_output_schema.json")
    return _output_schema


def _get_receipt_schema() -> dict[str, Any]:
    global _receipt_schema
    if _receipt_schema is None:
        _receipt_schema = _load_schema("receipt_event.schema.json")
    return _receipt_schema


def validate_proposal(proposal: dict[str, Any]) -> ValidationResult:
    """Validate a proposal against 06_output_schema.json.

    Fail-closed: if schema file is missing, raises FileNotFoundError.
    If proposal is invalid, returns ValidationResult with errors.
    """
    schema = _get_output_schema()
    return _validate(proposal, schema, "proposal")


def validate_receipt_event(receipt: dict[str, Any]) -> ValidationResult:
    """Validate a receipt event against receipt_event.schema.json.

    Fail-closed: if schema file is missing, raises FileNotFoundError.
    If receipt is invalid, returns ValidationResult with errors.
    """
    schema = _get_receipt_schema()
    return _validate(receipt, schema, "receipt_event")


def _validate(
    data: dict[str, Any],
    schema: dict[str, Any],
    context: str,
) -> ValidationResult:
    """Run JSON schema validation and collect all errors."""
    validator = jsonschema.Draft202012Validator(schema)
    errors: list[str] = []

    for error in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
        path = ".".join(str(p) for p in error.absolute_path) or "<root>"
        errors.append(f"{path}: {error.message}")

    if errors:
        logger.warning(
            "Schema validation FAILED (%s): %d errors — %s",
            context, len(errors), "; ".join(errors[:3]),
        )
    else:
        logger.debug("Schema validation PASSED (%s)", context)

    return ValidationResult(valid=len(errors) == 0, errors=errors)


def reset_schema_cache() -> None:
    """Reset cached schemas. Testing only."""
    global _output_schema, _receipt_schema
    _output_schema = None
    _receipt_schema = None
