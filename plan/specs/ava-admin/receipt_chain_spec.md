# Receipt Hash Chain Specification (Verifier-Ready)

**Source:** Ava Admin Enterprise Handoff v2
**Objective:** receipts are append-only and tamper-evident via hash chaining.
This document is a deterministic spec for computing `receipt_hash` and validating the chain.

## Definitions
- `chain_id`: stable identifier for a receipt chain. Recommended: `suite_id` (one chain per suite).
  - For global/admin receipts, use `chain_id = "global"`.
- `sequence`: monotonically increasing integer within a chain.
- `prev_hash`: hash of the previous receipt in the same chain (genesis uses fixed value).
- `receipt_hash`: SHA-256 hash computed over canonical receipt content + `prev_hash`.

## Canonicalization
Receipts must be serialized into canonical JSON before hashing:
1. UTF-8
2. Object keys sorted lexicographically
3. No insignificant whitespace
4. Arrays preserved in order
5. Exclude fields that are derived:
   - `receipt_hash`
   - `computed_fields.*` (if any)
Canonical JSON string is referred to as `canonical_receipt`.

## Hash algorithm
- `genesis_prev_hash = "0000000000000000000000000000000000000000000000000000000000000000"`
- `receipt_hash = sha256_hex( prev_hash + "\n" + canonical_receipt )`

Where:
- `prev_hash` is the **previous** receipt's `receipt_hash` in the same chain.
- `canonical_receipt` includes at minimum:
  - `receipt_id`, `correlation_id`
  - `chain_id`, `sequence`
  - `suite_id`, `office_id` (nullable for global)
  - `action_type`, `risk_tier`, `tool_used`
  - `created_at`, `executed_at`, `approved_at` (nullable)
  - `outcome`
  - `redaction_applied`, `redaction_ruleset_version`

## Storage requirements
- The receipts table must store: `chain_id`, `sequence`, `prev_hash`, `receipt_hash`.
- Insert must be atomic:
  - Compute `prev_hash` by reading latest receipt for the chain under transaction lock.
  - Assign `sequence = last_sequence + 1`.
  - Compute `receipt_hash` and store.

## Verifier job
A scheduled verifier (e.g., every 5 minutes) must:
1. For each `chain_id`, scan new receipts since last verified `sequence`.
2. Recompute each `receipt_hash` and ensure:
   - `prev_hash` matches prior recomputed hash
   - recomputed `receipt_hash` equals stored
3. On mismatch:
   - emit `OpsExceptionCard` with `severity=sev1` and `class=receipt_chain_integrity`
   - open an incident (no auto-repair)
   - require approval for any remediation steps

## Optional anchoring (recommended later)
Once daily, compute an anchor hash:
- `anchor = sha256_hex( chain_id + "\n" + last_receipt_hash + "\n" + date_utc )`
Store anchor in an immutable store (object storage with WORM retention).
Anchoring increases confidence but is not required for v1 production.

## Cross-reference
- Canonical receipts schema: `plan/schemas/receipts.schema.v1.yaml`
- Hash chain columns (chain_id, sequence, prev_hash, receipt_hash) are additive to canonical schema
- Implementation target: Phase 1A (Migration #50)
