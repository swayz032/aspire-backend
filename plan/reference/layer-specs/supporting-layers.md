# Supporting Layers Specification

**Purpose:** All non-core layers that support the 4-layer platform stack.
**These layers are built incrementally across phases, not as a single delivery.**

---

## 1. n8n Integration (Nervous System)

**Stage:** "soon" (NOT v1) — explicitly deprioritized for launch
**Zip Path:** `platform/integrations/n8n/` (10 files)
**Build Phase:** Post-v1

**What It Does:** Background automation — webhooks, timers, scheduled jobs, retry orchestration, batch processing. Request-only plumbing.

**What It DOES NOT Do:** Make decisions. Per Law #1 (Single Brain) and Law #7 (Tools Are Hands), n8n triggers the Brain but never replaces it.

**Pre-Built Assets:**

| File | Contents | Status |
|------|----------|--------|
| `README.md` | "n8n internal orchestration only" | Designed |
| `SECURITY_MODEL.md` | "Request only model" — no autonomous decisions | Designed |
| `MCP_CLAUDE_WORKFLOW.md` | Claude ↔ n8n MCP bridge pattern | Designed |
| `SETUP_SELF_HOSTED.md` | Docker-based self-hosted n8n setup | Designed |
| `templates/WORKFLOW_HARDENING_CHECKLIST.md` | Production hardening checklist | Designed |
| `templates/workflows/finance/FIN_DAILY_SYNC.json` | Empty shell `{}` | Placeholder |
| `templates/workflows/mail/MAIL_*.json` (4 files) | Empty shells | Placeholder |

**Integration Points:**
- Mail gateway IMAP sync (scheduled by n8n → calls Brain for classification)
- Daily finance reconciliation (n8n timer → Brain for verification)
- Retry orchestration (n8n retries → Brain decides whether to re-execute)

**V1 Decision:** Without n8n, the orchestrator handles scheduling and retries directly. This is simpler but less scalable. Acceptable for single-tenant dogfood.

---

## 2. Observability Layer

**Zip Path:** `platform/observability/` (7 files)
**Build Phase:** Specs used from Phase 1, dashboards built Phase 4

**What It Does:** Translates Trust Spine receipts into operational metrics. Defines SLO dashboards, alerting, and PII redaction in logs.

| File | Contents |
|------|----------|
| `README.md` | Observability strategy: receipts → metrics |
| `metrics/metrics_definitions.md` | Core metrics: provider health (by provider, by suite_id), ingestion success/failure rate, time-to-approval (Authority Queue), exception open/close rate, backfill run outcomes |
| `alerts/` | Alert definitions and escalation thresholds |
| `PII_REDACTION.md` | Log redaction policies (Presidio integration) |

**Recommendation:** Reference `metrics_definitions.md` in Phase 1 acceptance criteria. All Brain/Gateway operations should emit `correlation_id`, `latency_ms`, and `outcome` from day one.

---

## 3. Contracts Layer

**Zip Path:** `platform/contracts/` (59 files)
**Build Phase:** 1 (shared interfaces referenced by all layers)

**What It Does:** Shared binding interfaces that all layers reference. Single source of truth for cross-layer data contracts.

| Subdirectory | Contents |
|-------------|----------|
| `capabilities/` | Capability token format, scope definitions, validation rules |
| `events/` | Event schemas: intent, approval, receipt, state-change, A2A |
| `evidence/` | Evidence capture format, attachment schemas |
| `learning/` | Learning loop data contracts (behavior flywheel) |
| `providers/` | Provider adapter interface (standardized input/output) |
| `receipts/` | Receipt format contracts (maps to `plan/schemas/receipts.schema.v1.yaml`) |

**Integration:** These contracts are imported by Brain, Gateway, Trust Spine, and Control Plane. Changing a contract requires a Control Plane proposal + approval.

---

## 4. Providers Layer

**Zip Path:** `platform/providers/` (63 files) + root `providers/` (232 files)
**Build Phase:** 2 (skill pack implementation)

**What It Does:** Provider adapter implementations for external API integrations.

| Provider | Platform Files | Root Files | Status | Key Operations |
|----------|---------------|------------|--------|---------------|
| **Gusto** | ~15 | ~60 | **Scaffolded** | OAuth 2.0 flow, payroll CRUD, company status, replay/dedup |
| **Moov** | ~10 | ~45 | Designed | ACH transfers, wallets, balance management |
| **Plaid** | ~10 | ~40 | Designed | Bank connections, transactions, identity verification |
| **QuickBooks (QBO)** | ~10 | ~40 | Designed | Accounting sync, invoicing, chart of accounts |
| **Square** | ~8 | ~25 | Designed | POS, payments, catalog |
| **Stripe** | ~10 | ~22 | Designed | Invoicing, payments, customers |

**Test Fixtures:** `platform/fixtures/` — gusto, moov, plaid, qbo (mock data for testing)

**Recommendation:** Lead Phase 2 with Gusto-backed skill packs (Milo/Payroll, Finn/Money). Use Gusto as the adapter template, then replicate for Stripe, QBO, etc.

---

## 5. Services Layer

**Zip Path:** `platform/services/` (7 files)
**Build Phase:** 2 (mail gateway is Eli skill pack dependency)

**Mail Gateway Service:**

| File | Contents |
|------|----------|
| `ARCHITECTURE.md` | Service architecture overview |
| `api_contract.inbox_v2.md` | Inbox API contract (IMAP → normalized format) |
| `inbox_sync_worker.spec.md` | IMAP sync worker (scheduled by n8n or cron) |
| `outbox_smtp_executor.spec.md` | SMTP sending (outbox pattern) |
| `credential_vault.spec.md` | Email credential storage (encrypted) |
| `attachment_pipeline.spec.md` | File handling: receive → scan → store → reference |

---

## 6. Finance Office

**Zip Path:** `platform/finance-office/` (42 files)
**Build Phase:** 2 (Finn, Milo, Teressa skill pack dependencies)

**What It Does:** Domain-specific financial operations module with specialized business logic.

| Module | Files | Contents |
|--------|-------|----------|
| `books/` | ~8 | Categorization rules, vendor matching, snapshot builders, proposal builders |
| `money-movement/` | ~5 | Transfer operations, Moov/Plaid integration specs |
| `payroll/` | ~3 | Gusto payroll integration specs |
| `cash_buffer/` | ~5 | Cash buffer policy, risk exceptions |
| `evidence/` | ~5 | Mobile receipt capture, matching rules, receipt inbox |
| `reconciliation/` | ~3 | Bank reconciliation logic |
| `accountant_mode/` | ~5 | External accountant role schema, permissions, session management |
| `money_rules/` | ~5 | Business rules for money operations (thresholds, limits) |
| `micro_lessons/` | ~3 | Financial literacy content for operators |

---

## 7. Ingest Layer

**Zip Path:** `platform/ingest/` (3 files)
**Build Phase:** 1 (intent ingest is the entry point for all operations)

**What It Does:** Standardized intent ingest API — the front door for all operations entering Aspire.

| File | Contents |
|------|----------|
| Intent API schema | Standardized inbound event: `{ suite_id, office_id, intent_type, risk_tier, source, payload }` |
| `replay/` | Replay harness for deterministic testing |

**Canonical contract:** See `plan/reference/ecosystem-architecture.md` → Intent Ingest API section.

---

## 8. Trust Center

**Zip Path:** `platform/trust-center/` (9 files)
**Build Phase:** 5 (Beta Launch — needed for enterprise customers)

**What It Does:** Partner-facing security & governance documentation. Used for enterprise due diligence and compliance questionnaires.

| File | Contents |
|------|----------|
| `docs/` | Security questionnaire templates, compliance documentation |
| Partner onboarding materials | How partners certify against Aspire's governance model |

---

## 9. Security Layer

**Zip Path:** `platform/security/` (5 files)
**Build Phase:** 0B+ (progressive hardening)

| File | Contents |
|------|----------|
| `SECRETS_AND_ENVIRONMENTS.md` | Credential management patterns: no hardcoded keys, short-lived tokens, rotation |
| `posture/` | Security posture assessment documents |

---

## 10. Retention Layer

**Zip Path:** `platform/retention/` (3 files)
**Build Phase:** 4 (Hardening)

| File | Contents |
|------|----------|
| `deletion_workflow.md` | GDPR deletion procedures — how to handle data deletion requests while preserving receipt integrity |
| `export_workflow.md` | Data export procedures — portable format for user data |

**Key Constraint:** Receipts are immutable (Law #2). GDPR deletion anonymizes PII in receipts but never deletes the receipt itself.

---

## 11. Billing Layer

**Zip Path:** `platform/billing/` (1 file)
**Build Phase:** 3 (Mobile App — in-app purchase flow)

| File | Contents |
|------|----------|
| `USAGE_METERING_INTEGRATION.md` | How to meter usage for suite subscription + per-seat billing |

---

## 12. Additional Directories

| Directory | Files | Contents | Phase |
|-----------|-------|----------|-------|
| `platform/admin/` | ~8 | Admin portal contracts + docs | 3 |
| `platform/dev/` | ~3 | Development environment configs | 0B |
| `platform/storage/` | ~3 | S3 blob storage patterns | 0B |
| `platform/qa/robots/` | ~5 | Automated QA test robots | 4 |
| `platform/schemas/` | ~15 | Domain schemas: communications, docs, finance, qa, research | 1-2 |
| `platform/tests/` | ~30 | Test suites: books, finance-office, gateway, gateway_enforcement, gusto, money_movement, payroll, qbo | 2-4 |
| `platform/fixtures/` | ~12 | Mock data: gusto, moov, plaid, qbo | 2 |
| `platform/CLAUDE_HANDOFF/` | ~10 | Implementation handoff docs (Meeting of Minds, Learning Loop) | 2+ |

---

## Non-Platform Supporting Directories

| Directory | Files | Contents | Phase |
|-----------|-------|----------|-------|
| `agent_kits/` | 106 | Agent persona definitions (system prompts, constraints, tools, fewshots) | 1-2 |
| `skillpacks/` | 112 | Skill pack manifests (adapters, policies, prompts per pack) | 2 |
| `docs/` | 31 | Master architecture docs, ADRs, integration guides | Reference |
| `handoff/` | 15 | Claude Handoff: Meeting of Minds, Learning Loop | 2+ |
| `providers/` (root) | 232 | Detailed provider integration packages (API clients, auth, mapping) | 2 |
| `reference/` | 126 | Architecture references, decision records | Reference |
| `RUNBOOKS/` | 5 | Operational runbooks | 4 |
| `scripts/` | 6 | Automation scripts | 0B |
| `ui_handoff/` | 9 | Mobile UI handoff specs | 3 |
| `config/` | 3 | Global configuration | 0B |
| `DNS_TEMPLATES/` | 4 | DNS configuration templates | 0B |
| `ENVIRONMENTS/` | 4 | Environment definitions (dev, staging, prod) | 0B |

---

**End of Supporting Layers Specification**
