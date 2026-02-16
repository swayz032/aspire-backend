"""Receipt Schema Registry — validates receipts against JSON schemas (Law #2).

Loads all schemas from schemas/ops_receipts/ at import time.
Validates receipts before persistence (fail-closed per Law #3).

Config: ASPIRE_SCHEMA_VALIDATION_MODE env var:
  - "warn" (default): log validation errors but don't block
  - "strict": block on validation failure
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jsonschema
import referencing
import referencing.jsonschema

logger = logging.getLogger(__name__)

_OPS_RECEIPTS_DIR = Path(__file__).parent.parent / "schemas" / "ops_receipts"

# Registry: receipt_type -> schema dict
_schemas: dict[str, dict[str, Any]] = {}
_ref_registry: referencing.Registry | None = None
_loaded = False


@dataclass(frozen=True)
class ValidationResult:
    """Result of receipt schema validation."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    receipt_type: str = ""

    @property
    def error_count(self) -> int:
        return len(self.errors)


def _get_validation_mode() -> str:
    """Get validation mode from env. Default: warn."""
    return os.environ.get("ASPIRE_SCHEMA_VALIDATION_MODE", "warn").lower()


def load_schemas() -> dict[str, dict[str, Any]]:
    """Auto-load all JSON schemas from ops_receipts/ directory.

    Each schema is keyed by its 'title' field (e.g. "deploy.started").
    The base receipt.schema.json is loaded for $ref resolution but not registered as a type.
    """
    global _schemas, _ref_registry, _loaded

    if _loaded:
        return _schemas

    _schemas = {}
    ref_resources: list[tuple[str, referencing.Resource]] = []

    if not _OPS_RECEIPTS_DIR.exists():
        logger.warning("Ops receipts schema dir not found: %s", _OPS_RECEIPTS_DIR)
        _loaded = True
        return _schemas

    for schema_path in sorted(_OPS_RECEIPTS_DIR.glob("*.schema.json")):
        try:
            with open(schema_path, encoding="utf-8") as f:
                schema = json.load(f)

            # Register all schemas for $ref resolution
            ref_uri = f"./{schema_path.name}"
            resource = referencing.Resource.from_contents(schema, default_specification=referencing.jsonschema.DRAFT202012)
            ref_resources.append((ref_uri, resource))

            title = schema.get("title", "")
            if not title:
                # Base schema (receipt.schema.json) has no title — skip type registration
                continue

            _schemas[title] = schema
            logger.debug("Loaded ops receipt schema: %s", title)
        except Exception as e:
            logger.error("Failed to load schema %s: %s", schema_path.name, e)

    # Build referencing registry for $ref resolution
    _ref_registry = referencing.Registry().with_resources(ref_resources)

    _loaded = True
    logger.info("Receipt schema registry loaded: %d schemas", len(_schemas))
    return _schemas


def get_schema(receipt_type: str) -> dict[str, Any] | None:
    """Get schema for a receipt type. Returns None if not found."""
    if not _loaded:
        load_schemas()
    return _schemas.get(receipt_type)


def list_schemas() -> list[str]:
    """List all registered receipt schema types."""
    if not _loaded:
        load_schemas()
    return sorted(_schemas.keys())


def validate_receipt(receipt: dict[str, Any], receipt_type: str) -> ValidationResult:
    """Validate a receipt dict against its schema.

    In "warn" mode (default): logs errors but returns valid=True.
    In "strict" mode: returns valid=False on schema violations.
    """
    if not _loaded:
        load_schemas()

    schema = _schemas.get(receipt_type)
    if schema is None:
        # No schema for this type — pass through (not all types have ops schemas)
        return ValidationResult(valid=True, receipt_type=receipt_type)

    mode = _get_validation_mode()
    errors: list[str] = []

    try:
        # Use referencing registry for $ref resolution (base receipt.schema.json)
        validator = jsonschema.Draft202012Validator(schema, registry=_ref_registry or referencing.Registry())

        for error in sorted(validator.iter_errors(receipt), key=lambda e: list(e.path)):
            path = ".".join(str(p) for p in error.absolute_path) or "<root>"
            errors.append(f"{path}: {error.message}")
    except Exception as e:
        errors.append(f"Schema validation error: {e}")

    if errors:
        if mode == "strict":
            logger.warning(
                "Receipt schema validation FAILED (strict) for %s: %d errors — %s",
                receipt_type, len(errors), "; ".join(errors[:3]),
            )
            return ValidationResult(valid=False, errors=errors, receipt_type=receipt_type)
        else:
            logger.warning(
                "Receipt schema validation WARN for %s: %d errors — %s",
                receipt_type, len(errors), "; ".join(errors[:3]),
            )
            return ValidationResult(valid=True, errors=errors, receipt_type=receipt_type)

    return ValidationResult(valid=True, receipt_type=receipt_type)


def reset_registry() -> None:
    """Reset loaded schemas. Testing only."""
    global _schemas, _ref_registry, _loaded
    _schemas = {}
    _ref_registry = None
    _loaded = False
