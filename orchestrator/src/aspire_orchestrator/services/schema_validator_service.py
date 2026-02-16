"""Schema Validator Service — validates all Aspire contracts against JSON schemas.

Covers:
- Receipts (48 ecosystem + 20+ ops = 68+ total receipt schemas)
- Events (3 A2A/outbox event schemas)
- Capabilities (1 capability token schema)
- Evidence (1 evidence pack schema)
- Learning (5 learning object schemas)

Total: 78+ schemas (58 ecosystem + 20+ ops)

Integration points:
- receipt_write_node: validate every receipt before persistence
- a2a_service: validate A2A events
- Export scripts: validate evidence packs

Fail-closed (Law #3): invalid schema in strict mode -> deny + receipt.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

logger = logging.getLogger(__name__)

# Schema directories — ecosystem contracts + ops receipts
_SCHEMAS_DIR = Path(__file__).parent.parent / "schemas"
_CONTRACTS_DIR = _SCHEMAS_DIR / "contracts"
_OPS_RECEIPTS_DIR = _SCHEMAS_DIR / "ops_receipts"

# Categories map to subdirectories under contracts/
_ECOSYSTEM_CATEGORIES = ("receipts", "events", "capabilities", "evidence", "learning")

# Validation mode: "warn" (default) logs but passes, "strict" blocks on invalid
VALIDATION_MODE_ENV = "ASPIRE_SCHEMA_VALIDATION_MODE"


@dataclass(frozen=True)
class SchemaValidationResult:
    """Result of JSON schema validation."""

    valid: bool
    errors: list[str]
    schema_name: str = ""
    category: str = ""

    @property
    def error_count(self) -> int:
        return len(self.errors)


class SchemaValidatorService:
    """Centralized schema validation for all Aspire contract types.

    Auto-loads schemas from:
      - schemas/contracts/{receipts,events,capabilities,evidence,learning}/
      - schemas/ops_receipts/

    Handles $ref resolution for schemas that extend receipt.schema.json.
    """

    def __init__(self) -> None:
        self._schemas: dict[str, dict[str, dict[str, Any]]] = {}
        self._ref_store: dict[str, dict[str, Any]] = {}
        self._load_all_schemas()

    def _load_all_schemas(self) -> None:
        """Discover and load all JSON schemas from known directories."""
        # Load ecosystem contract schemas
        for category in _ECOSYSTEM_CATEGORIES:
            category_dir = _CONTRACTS_DIR / category
            if category_dir.is_dir():
                self._schemas[category] = {}
                for schema_file in sorted(category_dir.glob("*.json")):
                    try:
                        with open(schema_file, encoding="utf-8") as f:
                            schema_data = json.load(f)
                        name = schema_file.stem  # e.g. "authority.item.approved.schema"
                        self._schemas[category][name] = schema_data
                        # Build ref store keyed by filename for $ref resolution
                        self._ref_store[schema_file.name] = schema_data
                    except (json.JSONDecodeError, OSError) as exc:
                        logger.error(
                            "Failed to load schema %s: %s", schema_file, exc
                        )

        # Load ops receipt schemas
        if _OPS_RECEIPTS_DIR.is_dir():
            if "ops_receipts" not in self._schemas:
                self._schemas["ops_receipts"] = {}
            for schema_file in sorted(_OPS_RECEIPTS_DIR.glob("*.json")):
                try:
                    with open(schema_file, encoding="utf-8") as f:
                        schema_data = json.load(f)
                    name = schema_file.stem
                    self._schemas["ops_receipts"][name] = schema_data
                    self._ref_store[schema_file.name] = schema_data
                except (json.JSONDecodeError, OSError) as exc:
                    logger.error(
                        "Failed to load ops schema %s: %s", schema_file, exc
                    )

        total = sum(len(v) for v in self._schemas.values())
        logger.info(
            "SchemaValidatorService loaded %d schemas across %d categories",
            total,
            len(self._schemas),
        )

    def _get_validation_mode(self) -> str:
        """Get validation mode from environment. Default: warn."""
        return os.environ.get(VALIDATION_MODE_ENV, "warn").lower()

    def _build_registry(self, category: str) -> Registry:
        """Build a referencing.Registry for $ref resolution.

        Receipt schemas use $ref: ./receipt.schema.json which needs resolution
        against the receipts directory.
        """
        resources: list[tuple[str, Resource]] = []

        if category in ("receipts", "ops_receipts"):
            # Add all receipt schemas for $ref resolution
            receipts_dir = _CONTRACTS_DIR / "receipts"
            if receipts_dir.is_dir():
                for sf in receipts_dir.glob("*.json"):
                    try:
                        with open(sf, encoding="utf-8") as f:
                            schema_data = json.load(f)
                        uri = sf.name  # e.g. "receipt.schema.json"
                        resources.append((uri, Resource.from_contents(schema_data, default_specification=DRAFT202012)))
                        # Also register with ./ prefix for relative refs
                        resources.append((f"./{uri}", Resource.from_contents(schema_data, default_specification=DRAFT202012)))
                    except (json.JSONDecodeError, OSError):
                        pass

            # Also add ops receipts
            if _OPS_RECEIPTS_DIR.is_dir():
                for sf in _OPS_RECEIPTS_DIR.glob("*.json"):
                    try:
                        with open(sf, encoding="utf-8") as f:
                            schema_data = json.load(f)
                        uri = sf.name
                        resources.append((uri, Resource.from_contents(schema_data, default_specification=DRAFT202012)))
                    except (json.JSONDecodeError, OSError):
                        pass

        return Registry().with_resources(resources)

    def _validate(
        self,
        data: dict[str, Any],
        schema: dict[str, Any],
        category: str,
        schema_name: str,
    ) -> SchemaValidationResult:
        """Run JSON schema validation with $ref resolution."""
        registry = self._build_registry(category)
        validator = Draft202012Validator(schema, registry=registry)

        errors: list[str] = []
        for error in sorted(validator.iter_errors(data), key=lambda e: list(e.path)):
            path = ".".join(str(p) for p in error.absolute_path) or "<root>"
            errors.append(f"{path}: {error.message}")

        mode = self._get_validation_mode()

        if errors:
            if mode == "strict":
                logger.warning(
                    "Schema validation FAILED [strict] (%s/%s): %d errors — %s",
                    category,
                    schema_name,
                    len(errors),
                    "; ".join(errors[:3]),
                )
            else:
                logger.info(
                    "Schema validation WARN (%s/%s): %d errors — %s",
                    category,
                    schema_name,
                    len(errors),
                    "; ".join(errors[:3]),
                )
        else:
            logger.debug("Schema validation PASSED (%s/%s)", category, schema_name)

        return SchemaValidationResult(
            valid=len(errors) == 0,
            errors=errors,
            schema_name=schema_name,
            category=category,
        )

    def _resolve_schema(
        self, category: str, data: dict[str, Any], schema_name: str | None
    ) -> tuple[dict[str, Any], str] | None:
        """Resolve the correct schema for validation.

        If schema_name is given, use it directly.
        Otherwise, infer from receipt_type or event_type field in data.
        """
        schemas = self._schemas.get(category, {})

        if schema_name:
            # Try exact match, then with .schema suffix
            for candidate in (schema_name, f"{schema_name}.schema"):
                if candidate in schemas:
                    return schemas[candidate], candidate
            return None

        # Auto-detect from type fields
        type_field = data.get("receipt_type") or data.get("event_type") or ""
        if type_field:
            for candidate in (f"{type_field}.schema", type_field):
                if candidate in schemas:
                    return schemas[candidate], candidate
        return None

    def validate_receipt(
        self,
        receipt: dict[str, Any],
        schema_name: str | None = None,
    ) -> SchemaValidationResult:
        """Validate a receipt against its type-specific schema.

        Auto-detects schema from receipt_type field if schema_name not given.
        Falls back to base receipt.schema if type-specific not found.
        Checks both ecosystem receipts and ops_receipts.
        """
        # Try ecosystem receipts first
        result = self._resolve_schema("receipts", receipt, schema_name)
        if result is None:
            # Try ops_receipts
            result = self._resolve_schema("ops_receipts", receipt, schema_name)
        if result is None:
            # Fall back to base receipt schema
            base = self._schemas.get("receipts", {}).get("receipt.schema")
            if base:
                return self._validate(receipt, base, "receipts", "receipt.schema")
            return SchemaValidationResult(
                valid=False,
                errors=["No matching receipt schema found"],
                schema_name=schema_name or "unknown",
                category="receipts",
            )

        schema, resolved_name = result
        category = "receipts" if resolved_name in self._schemas.get("receipts", {}) else "ops_receipts"
        return self._validate(receipt, schema, category, resolved_name)

    def validate_event(
        self,
        event: dict[str, Any],
        schema_name: str | None = None,
    ) -> SchemaValidationResult:
        """Validate an A2A/outbox event against its schema.

        Auto-detects from event_type field if schema_name not given.
        """
        result = self._resolve_schema("events", event, schema_name)
        if result is None:
            return SchemaValidationResult(
                valid=False,
                errors=["No matching event schema found"],
                schema_name=schema_name or "unknown",
                category="events",
            )
        schema, resolved_name = result
        return self._validate(event, schema, "events", resolved_name)

    def validate_capability(
        self,
        token: dict[str, Any],
    ) -> SchemaValidationResult:
        """Validate a capability token against capability.schema.json."""
        schemas = self._schemas.get("capabilities", {})
        schema = schemas.get("capability.schema")
        if not schema:
            return SchemaValidationResult(
                valid=False,
                errors=["Capability schema not loaded"],
                schema_name="capability.schema",
                category="capabilities",
            )
        return self._validate(token, schema, "capabilities", "capability.schema")

    def validate_evidence_pack(
        self,
        pack: dict[str, Any],
    ) -> SchemaValidationResult:
        """Validate an evidence pack against evidence.pack.schema.json."""
        schemas = self._schemas.get("evidence", {})
        schema = schemas.get("evidence.pack.schema")
        if not schema:
            return SchemaValidationResult(
                valid=False,
                errors=["Evidence pack schema not loaded"],
                schema_name="evidence.pack.schema",
                category="evidence",
            )
        return self._validate(pack, schema, "evidence", "evidence.pack.schema")

    def validate_learning_object(
        self,
        obj: dict[str, Any],
        schema_name: str | None = None,
    ) -> SchemaValidationResult:
        """Validate a learning object against its specific schema.

        schema_name should match one of: change_proposal, eval_case,
        incident_summary, robot_assertion, runbook.
        """
        schemas = self._schemas.get("learning", {})
        if schema_name:
            for candidate in (schema_name, f"{schema_name}.schema"):
                if candidate in schemas:
                    return self._validate(obj, schemas[candidate], "learning", candidate)
        # Try inferring from 'kind' field
        kind = obj.get("kind", "")
        if kind:
            for candidate in (f"{kind}.schema", kind):
                if candidate in schemas:
                    return self._validate(obj, schemas[candidate], "learning", candidate)
        return SchemaValidationResult(
            valid=False,
            errors=[f"No matching learning schema found for: {schema_name or kind or 'unknown'}"],
            schema_name=schema_name or "unknown",
            category="learning",
        )

    def get_schema(
        self, category: str, name: str
    ) -> dict[str, Any] | None:
        """Get a specific schema by category and name."""
        schemas = self._schemas.get(category, {})
        return schemas.get(name) or schemas.get(f"{name}.schema")

    def list_schemas(self) -> dict[str, list[str]]:
        """List all loaded schemas grouped by category."""
        return {
            category: sorted(schemas.keys())
            for category, schemas in self._schemas.items()
        }

    def stats(self) -> dict[str, Any]:
        """Return statistics about loaded schemas."""
        by_category = {cat: len(schemas) for cat, schemas in self._schemas.items()}
        return {
            "total": sum(by_category.values()),
            "by_category": by_category,
            "categories": list(self._schemas.keys()),
        }


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------

_instance: SchemaValidatorService | None = None


def get_schema_validator() -> SchemaValidatorService:
    """Get the singleton SchemaValidatorService instance."""
    global _instance
    if _instance is None:
        _instance = SchemaValidatorService()
    return _instance


def reset_schema_validator() -> None:
    """Reset singleton. Testing only."""
    global _instance
    _instance = None
