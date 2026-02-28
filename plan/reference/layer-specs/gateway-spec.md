# Gateway Layer Specification

**Purpose:** Enforcement plane — accepts proposals from Brain, evaluates policies, creates approvals, enqueues outbox jobs. NEVER performs provider side effects directly.
**Build Phase:** 1 (alongside Brain)
**Readiness:** Designed (15%) — Comprehensive policy YAMLs, safety guard stubs, tool implementations. No enforcement engine.
**Ecosystem Path:** `platform/gateway/`
**File Count:** 59 files

---

## Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                      GATEWAY LAYER                             │
│                                                                │
│  Brain Proposal                                                │
│       ↓                                                        │
│  ┌─────────────┐   ┌──────────────┐   ┌───────────────┐       │
│  │   Policy     │ → │  Approval    │ → │   Outbox      │       │
│  │   Evaluator  │   │  Decision    │   │   Enqueue     │       │
│  │             │   │              │   │               │       │
│  │ tools_catalog│   │ GREEN: auto  │   │ → Trust Spine │       │
│  │ capabilities │   │ YELLOW: user │   │   outbox-     │       │
│  │ rate_limits  │   │ RED: authority│   │   worker      │       │
│  └─────────────┘   └──────────────┘   └───────────────┘       │
│        ↑                                                       │
│  ┌─────────────┐   ┌──────────────┐   ┌───────────────┐       │
│  │   Safety    │   │    PII       │   │   Provider    │       │
│  │   Guards    │   │  Redaction   │   │  Integrations │       │
│  │            │   │              │   │               │       │
│  │ video_pres. │   │ redaction.ts │   │ search/STT/   │       │
│  │ high_risk   │   │ data_min.ts  │   │ TTS adapters  │       │
│  │ recording   │   │              │   │               │       │
│  └─────────────┘   └──────────────┘   └───────────────┘       │
└────────────────────────────────────────────────────────────────┘
```

**Laws Enforced:** #4 (Risk Tiers), #5 (Capability Tokens), #7 (Tools Are Hands), #9 (Security/PII)

---

## Policy Wiring Flow (from `docs/POLICY_WIRING.md`)

```
Proposal → Policy Evaluation → Decision
                                  ↓
                    ┌─────────────┼─────────────┐
                    ↓             ↓             ↓
                 ALLOW         REQUIRE       DENY
                 (GREEN)     APPROVAL       (blocked)
                    ↓        (YELLOW/RED)      ↓
              Enqueue          ↓           Receipt
              Outbox     Authority Queue   (denied)
                    ↓          ↓
              Execute     User Approves
                    ↓          ↓
              Receipt     Enqueue Outbox
                              ↓
                           Execute
                              ↓
                           Receipt
```

**Key outputs per evaluation:**
- `policy_decision_id` — Traceable decision reference
- `approval_required` — Boolean
- `risk_tier` — green / yellow / red
- `execution_controls` — Whether executor can proceed

**Enforcement:** Executor REFUSES to execute if `execution_controls` denies execution.

---

## Component Inventory

### 1. Tools Catalog (35 Tools)

**Zip Path:** `platform/gateway/policies/tools_catalog.yaml`

Every tool in Aspire is registered with `name`, `kind` (read/write), `boundary`, and `description`.

| Boundary | Tools | Kind |
|----------|-------|------|
| **trust-spine** | `create_proposal`, `emit_receipt`, `request_approval`, `consume_capability` | write |
| **policy-engine** | `evaluate` | read |
| **brain** | `router.select_model` | read |
| **finance-office** | `money_events.read` | read |
| **gateway** | `transfer.create`, `transfer.submit`, `transfer.status.get`, `transfer.events.list` | mixed |
| **providers/gusto** | `read_payrolls`, `read_company_status`, `payroll.submit` | mixed |
| **providers/qbo** | `read_company`, `read_transactions`, `read_accounts` | read |
| **providers/email** | `email.send` | write |
| **providers/calendar** | `calendar.write` | write |
| **providers/legal** | `contract.create` | write |
| **docs** | `render_preview`, `render_pdf`, `preflight` | mixed |
| **storage** | `put_object`, `get_signed_url` | mixed |
| **providers/search** | `brave_search`, `tavily_search` | read |
| **providers/places** | `google_places`, `tomtom_search`, `here_search`, `foursquare_places`, `osm_overpass` | read |
| **providers/geo** | `mapbox_geocoding`, `osm_nominatim` | read |
| **forbidden** | `provider.execute_direct` | **BLOCKED** — calling providers without outbox is explicitly forbidden |

### 2. Per-Agent Capability Policies

**Zip Path:** `platform/gateway/policies/`

| Agent | Capabilities File | Key Capabilities |
|-------|------------------|------------------|
| **Adam** | `adam_capabilities.yaml` | Research tools (search, evidence capture) |
| **Finn** | `finn_capabilities.yaml` | `read_money_events`, `propose_transfer` — cannot execute transfers directly |
| **Milo** | `milo_capabilities.yaml` | Gusto read + payroll operations |
| **Nora** | `nora_capabilities.yaml` | Conference tools, audio, recording |
| **TEC** | `tec_capabilities.yaml` | Document rendering, storage |
| **Teressa** | `teressa_capabilities.yaml` | QBO accounting operations |
| **Council** | `council_capabilities.yaml` | Multi-agent deliberation |

### 3. Tool Allowlists (Per-Agent)

**Zip Path:** `platform/gateway/policies/tool_allowlists/`

| Agent | Allowlist File | Tools Allowed |
|-------|---------------|---------------|
| `adam.yaml` | Research + evidence tools | brave_search, tavily_search, places, evidence capture |
| `finn.yaml` | Finance read + propose | money_events.read, transfer.create (propose only) |
| `milo.yaml` | Payroll read + submit | gusto.read_payrolls, gusto.read_company_status |
| `nora.yaml` | Conference tools | audio stream, events, recording consent |
| `teressa.yaml` | Accounting read | qbo.read_transactions, qbo.read_accounts |
| `ava_money_movement.yaml` | Ava's money delegation scope | transfer tools (delegated to Finn) |

### 4. Safety Guards (3 TypeScript Guards)

**Zip Path:** `platform/gateway/src/guards/`

| Guard | File | Trigger | Action |
|-------|------|---------|--------|
| `video_presence` | `video_presence.guard.ts` | RED-tier operations require video presence | Blocks execution unless operator is on camera (Hot state) |
| `high_risk_interrupt` | `high_risk_interrupt.guard.ts` | HIGH risk detected mid-operation | Interrupts workflow, forces Authority Queue |
| `recording_consent` | `recording_consent.guard.ts` | Conference recording requested | Requires all participants' consent before recording |

**Test:** `platform/gateway/tests/guards/video_presence.guard.test.ts`

### 5. PII Redaction

**Zip Path:** `platform/gateway/safety/`

| File | Purpose |
|------|---------|
| `redaction.ts` | Core PII redaction (SSN, CC, email, phone, address) |
| `data_minimizer.ts` | Strip unnecessary PII from API responses before logging |

**Conference Redaction:** `policies/redaction/conference.yaml` — additional rules for meeting transcripts

**Redaction Rules (from CLAUDE.md Law #9):**
- SSN → `<SSN_REDACTED>`
- Credit card → `<CC_REDACTED>`
- Email → `<EMAIL_REDACTED>` (unless business email)
- Phone → `<PHONE_REDACTED>`
- Address → `<ADDRESS_REDACTED>`

### 6. Rate Limiting

**Zip Path:** `platform/gateway/policies/rate_limits/conference.yaml`
- Per-endpoint limits
- Per-suite limits
- Conference-specific limits (audio stream, transcript requests)

### 7. Webhook Standards

**Zip Path:** `platform/gateway/docs/WEBHOOKS.md`
- HMAC-SHA256 signature verification on all incoming webhooks
- Idempotency keys to prevent duplicate processing
- Retry semantics with exponential backoff

### 8. Provider Integrations (Pre-Built)

**Zip Path:** `platform/gateway/src/providers/`

| Category | Providers | Files |
|----------|-----------|-------|
| **Search** | Brave, Tavily + `search_router.ts` | `src/providers/search/` |
| **STT** | Deepgram (config, flux stream, types) | `src/providers/stt/` |
| **TTS** | ElevenLabs (config, voice cache, flash v2.5) | `src/providers/tts/` |

**Tests:**
- `tests/providers/deepgram_flux_stream.test.ts`
- `tests/providers/elevenlabs_flash_v2_5.test.ts`
- `tests/tools/talk_interrupt_policy.test.ts`

### 9. Gateway Tools (Pre-Built)

**Zip Path:** `platform/gateway/src/tools/`

| Category | Tools |
|----------|-------|
| **Conference** | `ingest_audio_stream.tool.ts`, `read_events.tool.ts` |
| **Money Movement** | `create_transfer.tool.ts`, `get_transfer_status.tool.ts`, `list_transfer_events.tool.ts` |
| **Payroll** | `gusto_prepare_payroll.tool.ts`, `gusto_read_company_status.tool.ts`, `gusto_read_payrolls.tool.ts`, `gusto_submit_payroll.tool.ts` |
| **Research** | `search.tool.ts` |
| **STT** | `stream_transcribe.tool.ts` |
| **Trust Spine** | `create_proposal.tool.ts`, `emit_receipt.tool.ts` |
| **Voice** | `tts_speak.tool.ts` |

### 10. Additional Policies

| Policy | File | Purpose |
|--------|------|---------|
| Audio Limits | `policies/audio_limits.yaml` | Conference audio stream constraints |
| Learning Data | `policies/learning_data_policy.yaml` | What data can feed the learning loop |
| Multi-Provider Data | `policies/multi_provider_data_policy.yaml` | Cross-provider data handling rules |
| PandaDoc IP Allowlist | `security/pandadoc_ip_allowlist.yaml` | Legal provider IP restrictions |

---

## Co-Founder Recommendations

1. **Load YAML policies AS-IS** — Don't simplify the capability matrices. They're comprehensive and correct. The policy evaluator is just a YAML parser + rule matcher.
2. **Implement guards early** — `video_presence` and `high_risk_interrupt` are what make RED-tier operations safe. These are differentiators.
3. **The `forbidden` tool** — `provider.execute_direct` is explicitly blocked. This is a deliberate architectural constraint that prevents shadow execution paths.
4. **Pre-built tool stubs** — 15+ TypeScript tool implementations already exist. These need completion, not rewriting.

---

## Implementation Readiness: 15%

| Component | Status | What's Needed |
|-----------|--------|---------------|
| Policy YAMLs | **Ready** | Load and parse at runtime |
| Tools Catalog | **Ready** | Wire into enforcement engine |
| Tool Allowlists | **Ready** | Enforce per-agent at request time |
| Safety Guards | **Scaffolded** | Complete TS implementations |
| PII Redaction | **Scaffolded** | Integrate Presidio, complete rules |
| Provider Integrations | **Scaffolded** | Complete STT/TTS/Search adapters |
| Gateway Tools | **Scaffolded** | Complete tool implementations |
| Enforcement Engine | **Not Started** | Build the core policy evaluator |
| OpenAPI | **Designed** | Implement endpoints per `openapi.yaml` |

---

**End of Gateway Layer Specification**
