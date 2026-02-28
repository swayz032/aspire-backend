# External Ecosystem Architecture (Platform Contracts & 10-Year Evolution)

**Extracted from:** `Aspire-Production-Roadmap.md` (lines 1536-1730)
**Philosophy:** "Stable Nucleus, Evolving Capabilities"

---

## The Non-Negotiable Rule

> **No partner may execute reality independently.**
> All execution flows through Aspire's Single Brain governance.

---

## Public Platform Contracts

### 1. Intent Ingest API (Inbound)
```json
{
  "suite_id": "uuid",
  "office_id": "uuid",
  "intent_type": "voice_command|text_input|webhook_event",
  "risk_tier": "green|yellow|red",
  "source": "mobile_app|email|calendar|external_webhook",
  "timestamp": "ISO8601",
  "payload": {}
}
```
> **NOTE:** Uses canonical `green/yellow/red` per `plan/schemas/risk-tiers.enum.yaml`

### 2. Capability Provider API (Outbound)
```json
{
  "tool_name": "stripe_invoice_create",
  "scopes": ["invoice.write", "customer.read"],
  "idempotency_key": true,
  "timeout_ms": 5000,
  "retry_policy": "exponential_backoff_3x"
}
```
Signed Capability Tokens required (<60s expiry, HMAC-SHA256).

### 3. Receipt + Evidence API (Audit)
See `plan/schemas/receipts.schema.v1.yaml` for the complete 15+ field schema.

---

## Certification Layer

### Provider Certification
- Token Scope Enforcement
- Webhook Signing (HMAC-SHA256)
- Idempotency (safe retries)
- Rate Limits (circuit breaker compliance)
- Failure Semantics (graceful degradation)
- Quarantine Power (disable without breaking core)

### Skill Pack Certification
- TC-01: Bounded Authority
- TC-02: Receipt Integrity
- TC-03: PII Redaction

---

## Evolution Doctrine (10-Year Horizon)

### FROZEN FOREVER
1. Single Brain Authority (LangGraph)
2. No Direct Tool Execution
3. Receipts for All Actions
4. Explicit Approvals
5. Identity Isolation (Suite/Office)

### CONTINUOUS EXPANSION
1. Skill Packs (new workflows via factory)
2. Discovery Sources (new data APIs)
3. Integrations (third-party via MCP)
4. Industry Workflows (Real Estate, Medical, Legal)
5. UI Surfaces (Mobile, Desktop, Voice, AR/VR)

---

## Ecosystem Roles

| Role | Who | Governance |
|------|-----|-----------|
| Operators | Founders, Teams | Approval gates, final decisions |
| AvAs | AI interfaces | Cannot execute alone |
| Providers | APIs, Tools | Capability tokens, receipts |
| Partners | Certified extensions (FUTURE) | Skill Pack certification |

---

## 10-Year Outcomes
- Vendor swap power (replace without breaking)
- Enterprise trust (audit-ready receipts)
- AI growth resilience (survive model churn)
- Similar to: AWS, Salesforce, SAP evolution models

---

## Platform Layer Architecture (from Ecosystem Zip v12.7)

The Aspire platform is organized into **4 core layers** plus **4 supporting layers**, mapped from the ecosystem zip directory structure (`platform/`).

### Core Layers

```
┌──────────────────────────────────────────────────────────────┐
│  4. CONTROL PLANE                                            │
│     Agent registry, skill pack management, rollouts,         │
│     certification (eval suites), agent studio                │
│     Lifecycle: Draft → Validate → Evals → Approve → Rollout │
├──────────────────────────────────────────────────────────────┤
│  3. BRAIN (Intelligence Layer)                               │
│     LangGraph orchestrator (Single Brain, Law #1),           │
│     LLM router (cost/quality), agent persona management,     │
│     state machines, routing decisions                        │
├──────────────────────────────────────────────────────────────┤
│  2. GATEWAY (Safety Boundary)                                │
│     Policy enforcement, capability token validation,         │
│     tool proxy, NeMo Guardrails, Presidio PII redaction,     │
│     rate limiting, propose/approve/deny workflow             │
├──────────────────────────────────────────────────────────────┤
│  1. TRUST SPINE (Governance Substrate)                       │
│     Receipts (append-only, hash-chained), approvals,         │
│     policies, outbox (durable execution), inbox (ingest),    │
│     A2A messaging, RLS enforcement                           │
│     Trust Spine canonical migrations (see CANONICAL_PATHS.md)│
├──────────────────────────────────────────────────────────────┤
│  0. SUPABASE (State Layer)                                   │
│     Postgres + Edge Functions + Realtime + Auth              │
│     Suite/Office identity, RLS tenant isolation              │
└──────────────────────────────────────────────────────────────┘
```

### Supporting Layers

| Layer | Purpose | Zip Path |
|-------|---------|----------|
| **Contracts** | Shared binding interfaces (events, receipts, capabilities, provider adapter) | `platform/contracts/` |
| **Ingest** | Intent ingest API — standardized inbound event schema | `platform/ingest/` |
| **Observability** | Monitoring, tracing, correlation IDs, PII redaction in logs | `platform/observability/` |
| **Trust Center** | Partner-facing security & governance documentation | `platform/trust-center/` |

### n8n Clarification

n8n is **NOT** a platform layer. It is an automation tool used for:
- Webhooks, timers, scheduled jobs
- Retry orchestration, batch processing
- Request-only plumbing

**n8n MUST NOT decide.** All decisions flow through the Brain (LangGraph). Per Law #1 (Single Brain) and Law #7 (Tools Are Hands), n8n is nervous system wiring — it triggers the brain, it does not replace it.

### Phase-to-Layer Build Order

| Phase | Layers Built |
|-------|-------------|
| **0B** | Trust Spine (canonical migrations per CANONICAL_PATHS.md, 5 core + 3 A2A Edge Functions, Go verifier) |
| **1** | Brain (LangGraph orchestrator) + Gateway (safety, capability tokens) |
| **2** | Control Plane (skill pack registry, certification) + Provider integrations |
| **3** | UI wiring to all layers (mobile → Brain → Gateway → Trust Spine) |
| **4** | Hardening across all layers (evil tests, circuit breakers, SLO) |
| **5** | End-to-end validation (1,000+ receipts flowing through all layers) |
| **6** | Scale (cloud migration, multi-region, marketplace) |

### Layer-to-Law Mapping

| Layer | Primary Laws Enforced |
|-------|----------------------|
| **Trust Spine** | Law #2 (Receipts), Law #3 (Fail Closed), Law #6 (Tenant Isolation) |
| **Gateway** | Law #4 (Risk Tiers), Law #5 (Capability Tokens), Law #9 (Security) |
| **Brain** | Law #1 (Single Brain), Law #7 (Tools Are Hands) |
| **Control Plane** | Law #4 (Risk Tiers via certification), Law #7 (bounded skill packs) |

---

**End of Ecosystem Architecture**
