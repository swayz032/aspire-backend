---
phase: "2"
name: "Founder Quarter MVP - 10 Skill Packs"
status: "not_started"
blocking_phase: "1"
blocks_phases: ["3", "4"]
duration_estimate: "8-10 weeks (accelerated via Ecosystem v12.7 scaffolds + pre-built APIs)"
gates_targeted: [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]
priority: "high"
hardware_required: "Skytech Shadow"
cost: "$40-80/mo (API usage: Stripe, PandaDoc, Gusto, QuickBooks, Exa, LiveKit, OpenAI)"
handoff_provides: "GetSequence spec + Trust Spine outbox pattern + Ecosystem v12.7 skill pack scaffolds"
---

# PHASE 2: Founder Quarter MVP - 11 Skill Packs

## 🔗 API WIRING MATRIX (v4.2)

**This phase connects all 11 external skill packs to their APIs:**

| Skill Pack | API Dependencies | Risk Tier | Wiring Tasks |
|------------|------------------|-----------|--------------|
| **Sarah** | Twilio | YELLOW | Telephony routing, call handling |
| **Eli** | PolarisM | YELLOW | IMAP/SMTP integration, email triage |
| **Quinn** | Stripe | YELLOW/RED | Invoice creation, Stripe Connect |
| **Nora** | LiveKit, Deepgram | GREEN | Video meetings, transcription |
| **Adam** | Brave, Tavily | GREEN | Web search, research queries |
| **Tec** | Internal | GREEN/YELLOW | PDF generation, document QC |
| **Finn** | Stripe, Plaid, Moov | RED | Money transfers, bank linking |
| **Milo** | Gusto | RED | Payroll processing |
| **Teressa** | QuickBooks | YELLOW | Accounting sync |
| **Clara** | PandaDoc | RED | Contract signing |
| **mail_ops_desk** | PolarisM | YELLOW | Domain/mailbox management |

**Ava (Orchestrator) Wiring:**
- **OpenAI/Anthropic/Gemini** → Meeting of Minds (multi-model council)
- **ElevenLabs** → Voice synthesis (Ava voice)
- **Anam** → Avatar rendering (Ava presence)

**Implementation Order (Weeks 10-19):**
- **Week 10-12:** Core packs (Sarah, Eli, Quinn) + integration tests
- **Week 13-15:** Communication packs (Nora, Ava voice/avatar) + latency tests
- **Week 16-17:** Financial packs (Finn, Milo, Teressa) + RED tier flows
- **Week 18-19:** Remaining packs (Adam, Tec, Clara, mail_ops_desk) + A2A

**Gates to Satisfy:**
- All skill packs generate receipts (100% coverage)
- A2A routing works between skill packs
- Skill pack tests green (TC-01, TC-02, TC-03)

---

## Objective

Ship **11 Skill Packs** from Aspire Ecosystem v12.7 with real API integrations:

**Channel Skill Packs (6) - Weeks 8-14:**
1. **Sarah** - Front Desk (inbound calls, routing, telephony)
2. **Eli** - Inbox (email handling, mail triage, PolarisM)
3. **Quinn** - Invoicing (Stripe Connect, billing, subscriptions)
4. **Nora** - Conference (meetings, LiveKit video, room booking)
5. **Adam** - Research (vendor discovery, Exa/Brave search, RFQ)
6. **Tec** - Documents (PDF generation, templates, QC workflow)

**Finance Office Skill Packs (3) - Weeks 15-18:**
7. **Finn** - Money Desk (business transfers, owner draws, reconciliation)
8. **Milo** - Payroll Desk (Gusto integration, payroll snapshots, exceptions)
9. **Teressa** - Books Desk (QuickBooks, accounting, categorization)

**Legal Skill Pack (1) - Weeks 19-20:**
10. **Clara** - Legal Desk (PandaDoc contracts, compliance, e-signatures)

**Internal Admin Skill Pack (1) - Week 20:**
11. **mail_ops_desk** - Mail Operations Admin (PolarisM, domain management, internal only)

**⚠️ CRITICAL**: This phase validates bounded authority + receipts in production with real external APIs.

---

## Ecosystem v12.7 Skill Pack Sources

**Registry:** `platform/control-plane/registry/skillpacks.external.json`

**Canonical Locations in Aspire_Ecosystem.zip:**

| Skill Pack | Provider | Ecosystem Location |
|------------|----------|-------------------|
| Sarah | telephony_front_desk | `providers/telephony_front_desk/` |
| Eli | polarismail_inbox | `providers/polarismail_inbox/` |
| Quinn | stripe_invoicing_connect | `providers/stripe_invoicing_connect/` |
| Nora | LiveKit | `skillpacks/nora-conference/` |
| Adam | brave_search_api, tavily | `providers/brave_search_api/`, `providers/tavily_search_api/` |
| Tec | PDF generation | `skillpacks/tec-docs/` |
| Finn | Stripe transfers | `platform/finance-office/money-movement/` |
| Milo | gusto_payroll | `providers/gusto_payroll/`, `skillpacks/milo-payroll/` |
| Teressa | QuickBooks | `platform/finance-office/books/` |
| Clara | pandadoc_legal | `providers/pandadoc_legal/` |
| mail_ops_desk | internal_admin | `platform/control-plane/registry/agents/mail_ops_desk.json` |

**Agent Persona Kits:** `agent_kits/agent_persona_kit/agents/`
- Contains prompt templates, router configs, and personality definitions for each skill pack

**Integration Pattern:**
Each Ecosystem skill pack provides:
- Provider adapter with gateway boundary
- Manifest template (permissions, risk tiers)
- Router rules (intent → action mapping)
- Test fixtures and evals

---

## Trust Spine Outbox Integration (NEW)

**Timeline Savings:** Trust Spine's durable outbox eliminates 1 week of retry/queue design work

**Outbox Pattern Pre-Built:**
- Provider adapters plug into existing outbox executor (deployed in Phase 0B)
- Pattern: Orchestrator enqueues jobs → Outbox worker claims jobs → Provider executes → Receipt logged
- No need to design retry/idempotency from scratch (Trust Spine handles this)

---

## Trust Spine Ecosystem Documentation (IMPORTANT)

**📚 Outbox pattern and provider adapter documentation exists in the Trust Spine package:**

### Outbox Pattern Resources
- **Navigation Guide:** See `Trust-Spine-Package-Navigation.md` (in this plan directory) for outbox workflow
- **ADR-0004:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/08_CLAUDE_JOB_KIT/ADR/ADR-0004-durable-execution-outbox.md` for outbox design decisions
- **ADR-0006:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/08_CLAUDE_JOB_KIT/ADR/ADR-0006-provider-plugins.md` for provider adapter pattern
- **Outbox Runbook:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/08_CLAUDE_JOB_KIT/PHASE_4_RELIABILITY_SCALE/RUNBOOKS/` for provider implementation guide

### Testing Resources
- **Idempotency Tests:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/13_E2E_TESTS/sql/idempotency_replay.sql` for duplicate request testing
- **Outbox Concurrency Tests:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/13_E2E_TESTS/sql/outbox_concurrency.sql` for job claim testing
- **Test Execution Guide:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/13_E2E_TESTS/README.md` for test order

### Troubleshooting Resources
- **Outbox Stuck Runbook:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/06_ADDONS/ops/runbooks/OUTBOX_STUCK.md` for debugging failed/stuck jobs
- **Provider Integration Issues:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/06_ADDONS/ops/runbooks/` for common provider errors

**⭐ Start Here:** Read `Trust-Spine-Package-Navigation.md` → then ADR-0004 and ADR-0006 for outbox + provider patterns.

---

## Dependencies

**Requires (Blocking):**
- Phase 0C: Domain Rail Foundation (mail tables + Domain Rail service for Eli Inbox / mail_ops_desk)
- Phase 1: Core Orchestrator (LangGraph + Ava + receipts working + mail tool registration)
- Manifests designed in Phase 0A (ready to implement)

**Blocks (Downstream):**
- Phase 3: Desktop + Mobile App (needs skill packs to demonstrate value, mail UI depends on Eli)
- Phase 4: Production Hardening (needs base functionality to harden, mail production gates)

---


## Handoff Package Reference Material

**Status:** 📚 DOCUMENTATION AVAILABLE

The handoff package provides detailed provider integration documentation and test fixtures.

**What's Included:**
- **Provider Documentation** (phase2_integrations/providers/)
  - Gmail API integration (OAuth scopes, webhooks, rate limits)
  - Google Calendar API integration
  - QuickBooks API integration (NEW - not in original roadmap)
  - Shopify API integration (NEW - not in original roadmap)
  - Stripe API integration
  - Token storage security rules
  
- **Golden Run Fixtures** (phase2_integrations/fixtures/)
  - Invoice creation flow (end-to-end)
  - Payment processing flow
  - Meeting booking flow
  - Follow-up email flow

**Integration Path:**
\
**Use:** Reference these docs when building skill pack integrations. Golden run fixtures provide expected receipt flows for testing.

---

## Trust Spine Integration Tasks

### Provider Adapters (Week 8-12)

All 10 skill packs use the Trust Spine outbox pattern. Provider adapters are pre-scaffolded in Ecosystem v12.7.

- [ ] **PHASE2-TASK-TS-001** Telephony Provider Adapter (Sarah)
  - Merge `providers/telephony_front_desk/` into `platform/providers/`
  - Configure inbound call handling, routing rules
  - Test: Idempotency + receipt generation for call events
  - **Verification:** Sarah can route calls to correct skill packs

- [ ] **PHASE2-TASK-TS-002** Mail Provider Adapter (Eli)
  - Merge `providers/polarismail_inbox/`
  - Configure OAuth, webhooks for email ingestion
  - Test: Email triage + draft responses
  - **Verification:** Eli can read/draft emails with receipts

- [ ] **PHASE2-TASK-TS-003** Stripe Provider Adapter (Quinn)
  - Merge `providers/stripe_invoicing_connect/`
  - Configure connected accounts, webhooks
  - Test: Invoice creation, subscription billing
  - **Verification:** Quinn invoicing flow end-to-end

- [ ] **PHASE2-TASK-TS-004** Conference Provider Adapter (Nora)
  - Configure LiveKit integration
  - Room creation, participant management
  - Test: Video call setup + meeting summaries
  - **Verification:** Nora conference booking works

- [ ] **PHASE2-TASK-TS-005** Search Provider Adapters (Adam)
  - Merge `providers/brave_search_api/` and `providers/tavily_search_api/`
  - Configure vendor discovery, RFQ generation
  - Test: Search results + comparison tables
  - **Verification:** Adam research queries work

- [ ] **PHASE2-TASK-TS-006** Document Provider Adapter (Tec)
  - Configure PDF generation (Puppeteer)
  - Template system, QC workflow
  - Test: Document generation + S3 storage
  - **Verification:** Tec PDF generation works

- [ ] **PHASE2-TASK-TS-007** Money Movement Provider (Finn)
  - Configure Stripe transfers, owner draws
  - Reconciliation workflow
  - Test: Transfer approval flow (RED tier)
  - **Verification:** Finn money movement with dual approval

- [ ] **PHASE2-TASK-TS-008** Payroll Provider Adapter (Milo)
  - Merge `providers/gusto_payroll/`
  - Configure payroll sync, snapshot generation
  - Test: Payroll approval workflow (RED tier)
  - **Verification:** Milo payroll snapshots work

- [ ] **PHASE2-TASK-TS-009** Accounting Provider Adapter (Teressa)
  - Configure QuickBooks integration
  - Transaction categorization, reports
  - Test: Accounting sync + categorization
  - **Verification:** Teressa books sync works

- [ ] **PHASE2-TASK-TS-010** Legal Provider Adapter (Clara)
  - Merge `providers/pandadoc_legal/`
  - Configure contract templates, e-signatures
  - Test: Document signing flow (RED tier)
  - **Verification:** Clara contract signing works

### Outbox Job Submission (Week 12-14)

- [ ] **PHASE2-TASK-TS-011** Wire All Skill Packs to Outbox
  - All 10 skill packs → outbox job submission pattern
  - Provider gateway boundary enforcement
  - Test: 100% job submission success rate
  - **Verification:** All skill packs enqueue jobs correctly

---

## Skill Packs to Build (10 Ecosystem v12.7 Packs)

### 1. Sarah - Front Desk (Telephony)

**Source:** `providers/telephony_front_desk/`

- [ ] `PHASE2-TASK-001` **Inbound Call Handling**
  - Configure telephony provider (LiveKit/Twilio)
  - Call routing based on intent classification
  - Warm interaction mode (voice-first, non-social)
  - Test: Route 10 sample calls correctly

- [ ] `PHASE2-TASK-002` **Call Intent Classification**
  - Classify: Support, sales, scheduling, billing, other
  - Route to appropriate skill pack (Eli, Quinn, Nora, etc.)
  - Escalation to human for unclassified

- [ ] `PHASE2-TASK-003` **Visitor/Contact Logging**
  - Log all inbound contacts with receipt
  - Track caller history, preferences
  - Link to CRM data if available

- [ ] `PHASE2-TASK-004` **Manifest Implementation (Sarah)**
  - Allow: `call.route`, `call.transfer`, `visitor.log`
  - Deny: `call.record_without_consent`
  - Approval gates: `call.transfer_external` (YELLOW)

---

### 2. Eli - Inbox (Mail Handling) — EXPANDED (PolarisMail + Domains Integration)

**Source:** `providers/polarismail_inbox/` + Phase 0C Domain Rail + Handoff Package

**⚠️ NOTE:** We use **PolarisM / EmailArray** (NOT Zoho whitelabel). Server-side credential vaulting ensures users never manage passwords directly. Domain purchase via ResellerClub through Domain Rail (Railway, static IP).

#### Core Eli Tasks (Original)

- [ ] `PHASE2-TASK-005` **Mail Provider Integration**
  - PolarisM IMAP/SMTP setup (server-side credential vaulting)
  - Webhook for new email ingestion
  - Scope: read, draft, send

- [ ] `PHASE2-TASK-006` **Email Triage & Classification**
  - Classify: Support, sales, billing, spam
  - Extract: Sender, subject, urgency, intent
  - Route to appropriate handler

- [ ] `PHASE2-TASK-007` **Draft Response Generation**
  - RAG-based FAQ matching (pgvector)
  - AI-generated draft responses
  - Human review before sending (YELLOW)

- [ ] `PHASE2-TASK-008` **Manifest Implementation (Eli)**
  - Allow: `email.read`, `email.draft`, `email.send`
  - Deny: `email.delete_all`, `email.forward_bulk`
  - Approval gates: `email.send` (YELLOW)

#### Mail/Domain Expansion Tasks (NEW — Weeks 14-15, within existing Eli allocation)

**Dependencies:** Phase 0C (Domain Rail + mail tables), Phase 1 (orchestrator + mail tool registration)

- [ ] `PHASE2-TASK-2M-001` **BYOD Onboarding State Machine (Full 13 States)**
  - Bring-Your-Own-Domain onboarding flow:
    ```
    INIT → DOMAIN_INPUT → DOMAIN_CHECK → OWNERSHIP_VERIFY →
    DNS_CONFIG → DNS_PROPAGATION_WAIT → MX_VERIFY → SPF_VERIFY →
    DKIM_VERIFY → FINAL_VERIFY → PROVISIONING → ACTIVE → ERROR
    ```
  - GPT handoff only implemented 6 states — must build all 13
  - Each state transition produces a receipt
  - Timeout handling for DNS_PROPAGATION_WAIT (up to 48h)
  - **Verification:** All 13 states reachable, transitions logged

- [ ] `PHASE2-TASK-2M-002` **Buy Domain State Machine (Full 13 States)**
  - Purchase domain + auto-configure flow:
    ```
    INIT → DOMAIN_INPUT → DOMAIN_CHECK → OWNERSHIP_VERIFY →
    DNS_CONFIG → DNS_PROPAGATION_WAIT → MX_VERIFY → SPF_VERIFY →
    DKIM_VERIFY → FINAL_VERIFY → PROVISIONING → ACTIVE → ERROR
    ```
  - Domain purchase is RED tier (requires explicit authority UI)
  - Routes through Domain Rail → ResellerClub API (IP-whitelisted)
  - Idempotency key for domain purchase (no duplicate registrations)
  - **Verification:** Purchase + auto-DNS-config end-to-end

- [ ] `PHASE2-TASK-2M-003` **Gmail OAuth Adapter (BYO Accounts)**
  - OAuth 2.0 for existing Gmail/Google Workspace accounts
  - Uses existing `oauth_tokens` table (Phase 0B desktop_tables)
  - Scopes: `gmail.readonly`, `gmail.send`, `gmail.compose`
  - Token refresh + revocation handling
  - **Verification:** Gmail read/send via OAuth working

- [ ] `PHASE2-TASK-2M-004` **Inbox Send State Machine (Draft → Approve → Send)**
  - Draft creation (GREEN — autonomous)
  - User approval for sending (YELLOW — requires confirmation)
  - Send execution via EmailArray/Gmail adapter
  - Failure handling + retry via outbox pattern
  - Receipt on success AND failure
  - **Verification:** Draft → approve → send → receipt end-to-end

- [ ] `PHASE2-TASK-2M-005` **EmailArray Provider Adapter (TypeScript)**
  - Port from PHP `Polarismail.php` patterns to TypeScript
  - Methods: `createMailbox()`, `deleteMailbox()`, `changePassword()`, `listMailboxes()`
  - All operations route through Domain Rail service
  - Circuit breaker + timeout enforcement
  - **Verification:** Mailbox CRUD operations working via adapter

- [ ] `PHASE2-TASK-2M-006` **16 Mail Receipt Types Integrated into Trust Spine**
  - Integrate 16 mail-specific receipt types:
    - `mail.domain.purchased`, `mail.domain.verified`, `mail.domain.dns.configured`
    - `mail.domain.deleted`, `mail.domain.transferred`
    - `mail.account.created`, `mail.account.deleted`, `mail.account.suspended`
    - `mail.send.drafted`, `mail.send.approved`, `mail.send.executed`, `mail.send.failed`
    - `mail.receive.ingested`, `mail.receive.classified`
    - `mail.onboard.byod.completed`, `mail.onboard.purchase.completed`
  - All use existing `receipts` table (envelope/payload pattern)
  - **Verification:** All 16 receipt types emit correctly

- [ ] `PHASE2-TASK-2M-007` **n8n Workflow Definitions (4 Mail Workflows)**
  - `mail-domain-onboard` — BYOD/purchase → DNS verify → provision
  - `mail-inbox-triage` — New email → classify → route to handler
  - `mail-outbox-send` — Approved draft → send via provider → receipt
  - `mail-dns-monitor` — Periodic DNS health check → alert on failures
  - All workflows are request-only plumbing (Law #7 — n8n never decides)
  - **Verification:** Workflows trigger correctly on events

- [ ] `PHASE2-TASK-2M-008` **RLS + Evil Tests for All Mail Operations**
  - RLS isolation for all mail state changes (mailbox CRUD, domain operations)
  - Evil tests:
    - Cross-tenant mailbox access attempt → denied
    - Expired capability token on domain purchase → denied
    - Send email without approval (YELLOW bypass attempt) → denied
    - Create mailbox exceeding suite quota → denied
  - **Verification:** Zero cross-tenant leakage, all evil tests pass

#### Mail State Machine Detail (What GPT Missed)
```
GPT built:  INIT → DOMAIN_CHECK → DNS_CONFIG → VERIFY → ACTIVE → ERROR  (6 states)
Required:   INIT → DOMAIN_INPUT → DOMAIN_CHECK → OWNERSHIP_VERIFY →
            DNS_CONFIG → DNS_PROPAGATION_WAIT → MX_VERIFY → SPF_VERIFY →
            DKIM_VERIFY → FINAL_VERIFY → PROVISIONING → ACTIVE → ERROR  (13 states)
```
The full 13-state machine handles real-world DNS propagation delays, individual record verification (MX, SPF, DKIM separately), and proper provisioning sequencing.

#### Mail Expansion Success Criteria
- [ ] `2-SC-MAIL-001` BYOD onboarding: all 13 states reachable and tested
- [ ] `2-SC-MAIL-002` Buy Domain: purchase + auto-DNS end-to-end working
- [ ] `2-SC-MAIL-003` Gmail OAuth: read/send via existing Google accounts
- [ ] `2-SC-MAIL-004` Send pipeline: draft → approve → send → receipt
- [ ] `2-SC-MAIL-005` EmailArray adapter: mailbox CRUD operational
- [ ] `2-SC-MAIL-006` All 16 mail receipt types emitting correctly
- [ ] `2-SC-MAIL-007` Zero cross-tenant leakage in mail evil tests

---

### 3. Quinn - Invoicing (Stripe Connect)

**Source:** `providers/stripe_invoicing_connect/`

- [ ] `PHASE2-TASK-009` **Stripe API Integration**
  - OAuth 2.0 connected accounts
  - Webhook: `/webhooks/stripe`
  - Events: `invoice.sent`, `payment_intent.succeeded`

- [ ] `PHASE2-TASK-010` **Invoice Creation**
  - Create Stripe invoice objects
  - Line items, tax calculation
  - Draft preview for approval (YELLOW)

- [ ] `PHASE2-TASK-011` **Subscription Billing**
  - Create/update subscriptions
  - Proration handling
  - Failed payment retry logic

- [ ] `PHASE2-TASK-012` **Manifest Implementation (Quinn)**
  - Allow: `invoice.create`, `invoice.send`, `subscription.create`
  - Deny: `refund.unlimited`, `subscription.cancel_all`
  - Approval gates: `invoice.send` (YELLOW), `refund.process` (RED)

---

### 4. Nora - Conference (Meetings)

**Source:** `skillpacks/nora-conference/`

- [ ] `PHASE2-TASK-013` **LiveKit Integration**
  - Configure video conferencing
  - Room creation, participant management
  - Recording permissions (RED tier)

- [ ] `PHASE2-TASK-014` **Meeting Scheduling**
  - Calendar integration (Google/Outlook)
  - Availability finder, conflict detection
  - Invite generation

- [ ] `PHASE2-TASK-015` **Meeting Summaries**
  - AI-generated post-meeting summaries
  - Action item extraction
  - Receipt with summary stored

- [ ] `PHASE2-TASK-016` **Manifest Implementation (Nora)**
  - Allow: `conference.create`, `conference.invite`, `meeting.summarize`
  - Deny: `conference.record_without_consent`
  - Approval gates: `conference.record` (RED)

---

### 5. Adam - Research (Vendor Discovery)

**Source:** `providers/brave_search_api/`, `providers/tavily_search_api/`, `providers/google_places_api/`, `providers/listings_fallbacks/`

**Additional Providers (P2 Gap Fix):**
- `providers/google_places_api/` - Google Places API for local business discovery
- `providers/listings_fallbacks/` - Fallback listings sources when primary providers fail

- [ ] `PHASE2-TASK-017` **Search Provider Integration**
  - Configure Exa/Brave/Tavily APIs
  - Industry-specific search filters
  - Result ranking and scoring

- [ ] `PHASE2-TASK-018` **Vendor Comparison**
  - Side-by-side comparison tables
  - Price, quality, delivery metrics
  - Recommendation ranking

- [ ] `PHASE2-TASK-019` **RFQ Generation**
  - Generate Request for Quote documents
  - Track RFQ status and responses
  - Vendor communication tracking

- [ ] `PHASE2-TASK-020` **Manifest Implementation (Adam)**
  - Allow: `vendor.search`, `rfq.generate`, `comparison.create`
  - Deny: `vendor.auto_contract`
  - Approval gates: `rfq.send` (YELLOW)

---

### 6. Tec - Documents (PDF Generation)

**Source:** `skillpacks/tec-docs/`

- [ ] `PHASE2-TASK-021` **Template Engine**
  - Puppeteer-based PDF generation
  - React Email templates
  - Variable substitution

- [ ] `PHASE2-TASK-022` **Document QC Workflow**
  - Automated validation (required fields)
  - Preview before finalization
  - Version tracking

- [ ] `PHASE2-TASK-023` **S3 Storage Integration**
  - Store documents: `s3://{suite_id}/documents/`
  - Retention policy (7 years legal)
  - Retrieval and sharing

- [ ] `PHASE2-TASK-024` **Manifest Implementation (Tec)**
  - Allow: `document.generate`, `document.preview`
  - Deny: `document.delete_final`
  - Approval gates: `document.share_external` (YELLOW)

---

### 7. Finn - Money Desk (Business Transfers)

**Source:** `platform/finance-office/money-movement/`

- [ ] `PHASE2-TASK-025` **Stripe Connect Transfers**
  - Configure connected accounts
  - Business transfers, payouts
  - Multi-currency support

- [ ] `PHASE2-TASK-026` **Owner Draw Processing**
  - Track owner draw requests
  - Validate against cash reserves
  - Dual approval (owner + accountant) - RED tier

- [ ] `PHASE2-TASK-027` **Payment Reconciliation**
  - Match payments to invoices
  - Flag discrepancies
  - Generate reconciliation receipts

- [ ] `PHASE2-TASK-028` **Manifest Implementation (Finn)**
  - Allow: `payment.draft`, `reconciliation.run`
  - Deny: `payment.send_unlimited`
  - Approval gates: `payment.send` (RED), `owner_draw.approve` (RED)

---

### 8. Milo - Payroll Desk (Gusto)

**Source:** `providers/gusto_payroll/`, `skillpacks/milo-payroll/`

- [ ] `PHASE2-TASK-029` **Gusto Integration**
  - OAuth 2.0 connected accounts
  - Sync employee data, schedules
  - Payroll triggers

- [ ] `PHASE2-TASK-030` **Payroll Snapshot Generation**
  - Pre-payroll snapshots (earnings, deductions, taxes)
  - Review before processing
  - Snapshot stored as receipt

- [ ] `PHASE2-TASK-031` **Payroll Approval Workflow**
  - Dual approval (HR + Finance) - RED tier
  - Deadline enforcement
  - Escalation for missed approvals

- [ ] `PHASE2-TASK-032` **Manifest Implementation (Milo)**
  - Allow: `payroll.snapshot`, `payroll.schedule`
  - Deny: `payroll.modify_history`
  - Approval gates: `payroll.process` (RED)

---

### 9. Teressa - Books Desk (Accounting)

**Source:** `platform/finance-office/books/`

- [ ] `PHASE2-TASK-033` **QuickBooks Integration**
  - OAuth 2.0 connected accounts
  - Sync chart of accounts, transactions
  - Category suggestions

- [ ] `PHASE2-TASK-034` **Transaction Categorization**
  - AI-powered category suggestions
  - Rule-based auto-categorization
  - Human review for uncertain items

- [ ] `PHASE2-TASK-035` **Financial Reports**
  - P&L, Balance Sheet, Cash Flow
  - Period selection (monthly, quarterly)
  - Export to PDF/CSV

- [ ] `PHASE2-TASK-036` **Manifest Implementation (Teressa)**
  - Allow: `transaction.categorize`, `report.generate`
  - Deny: `transaction.delete`, `books.close_without_review`
  - Approval gates: `books.close_period` (YELLOW)

---

### 10. Clara - Legal Desk (Contracts)

**Source:** `providers/pandadoc_legal/`

- [ ] `PHASE2-TASK-037` **PandaDoc Integration**
  - OAuth 2.0 connected accounts
  - Create documents from templates
  - Signature workflows

- [ ] `PHASE2-TASK-038` **Contract Templates**
  - NDA, MSA, SOW, Employment
  - Variable substitution
  - Version control

- [ ] `PHASE2-TASK-039` **Compliance Tracking**
  - Contract expiration tracking
  - Renewal reminders
  - Compliance calendar

- [ ] `PHASE2-TASK-040` **Manifest Implementation (Clara)**
  - Allow: `contract.create_draft`, `contract.send_for_signature`
  - Deny: `contract.sign_as_company`, `contract.delete_signed`
  - Approval gates: `contract.send` (YELLOW), `contract.sign` (RED)

---

### 11. mail_ops_desk - Internal Mail Admin (PolarisM) - NEW

**Source:** `platform/control-plane/registry/agents/mail_ops_desk.json`
**State Machine:** `platform/brain/state_machines/mail_ops_triage.yaml`

**⚠️ INTERNAL ADMIN ONLY** - This skill pack handles PolarisM mail infrastructure administration. NOT user-facing.

- [ ] `PHASE2-TASK-041` **Domain Management**
  - Add domains to suite
  - Verify domain DNS (SPF/DKIM/DMARC)
  - Track domain status
  - Test: Domain verification flow works

- [ ] `PHASE2-TASK-042` **Mailbox Administration**
  - Create mailbox for office
  - Rotate mailbox password (receipts only - never return secret)
  - Suspend/unsuspend mailbox
  - Test: Mailbox lifecycle operations work

- [ ] `PHASE2-TASK-043` **Incident Integration**
  - Open incidents for mail delivery failures
  - Escalate to Authority Queue for critical issues
  - Track incident resolution
  - Test: Incident creation and escalation work

- [ ] `PHASE2-TASK-044` **Manifest Implementation (mail_ops_desk)**

**Permissions:**
```json
{
  "allow": [
    "mail_admin.add_domain",
    "mail_admin.verify_domain",
    "mail_admin.create_mailbox",
    "mail_admin.rotate_password",
    "mail_admin.suspend_mailbox",
    "incidents.open",
    "authority_queue.propose"
  ],
  "deny": [
    "user_content_access",
    "sending_email",
    "reading_user_mail"
  ]
}
```

**Hard Rules:**
- **NO user content access** - Cannot read user emails
- **NO sending email** - Cannot send on behalf of users
- **100% receipted** - All actions generate receipts
- Uses **PolarisM** (NOT Zoho whitelabel)
- Credential secrets NEVER returned in responses

---

## Finance Office Supporting Systems (NEW)

**Source:** `platform/finance-office/`

Beyond the 3 Finance Office skill packs (Finn, Milo, Teressa), additional systems provide supporting infrastructure:

### Supporting Systems (Not User-Facing)

| System | Location | Purpose | Skill Pack Link |
|--------|----------|---------|-----------------|
| **cash_buffer/** | `finance-office/cash_buffer/` | Cash reserve forecasting + alerts | Finn (alerts) |
| **reconciliation/** | `finance-office/reconciliation/` | Bank statement reconciliation workflows | Teressa (input) |
| **accountant_mode/** | `finance-office/accountant_mode/` | Read-only interface for external auditors | Teressa (access) |
| **money_rules/** | `finance-office/money_rules/` | Policy configuration for transfers | Finn (enforcement) |
| **evidence/** | `finance-office/evidence/` | Evidence collection for financial proposals | All Finance |
| **exception_taxonomy.md** | `finance-office/` | Classification of financial exceptions | All Finance |

### Finance Office Integration Tasks

- [ ] **PHASE2-TASK-FO-001** Cash Buffer System
  - Deploy cash reserve forecasting
  - Configure alert thresholds
  - Wire alerts to Authority Queue
  - Test: Low cash buffer triggers alert
  - **Verification:** Cash buffer monitoring active

- [ ] **PHASE2-TASK-FO-002** Reconciliation Workflows
  - Deploy bank statement reconciliation
  - Configure matching rules
  - Generate discrepancy reports
  - Test: Reconciliation identifies mismatches
  - **Verification:** Reconciliation workflow operational

- [ ] **PHASE2-TASK-FO-003** Accountant Mode
  - Deploy read-only accountant interface
  - Configure access controls (view-only)
  - Audit trail for accountant access
  - Test: Accountant can view but not modify
  - **Verification:** Accountant mode working

- [ ] **PHASE2-TASK-FO-004** Money Rules Configuration
  - Deploy money movement policy engine
  - Configure transfer limits and approvals
  - Wire to Finn for enforcement
  - Test: Policy blocks over-limit transfer
  - **Verification:** Money rules enforced

- [ ] **PHASE2-TASK-FO-005** Evidence Collection
  - Deploy evidence collection system
  - Attach evidence to financial proposals
  - Store evidence with receipts
  - Test: Proposals include evidence attachments
  - **Verification:** Evidence collection working

### Finance Office Success Criteria

- [ ] `2-SC-FO-001` Cash buffer system monitoring active
- [ ] `2-SC-FO-002` Reconciliation workflow identifies discrepancies
- [ ] `2-SC-FO-003` Accountant mode provides read-only access
- [ ] `2-SC-FO-004` Money rules enforce transfer policies
- [ ] `2-SC-FO-005` Evidence collection attached to all financial proposals

---

## Legacy Migration Note

**OLD 4 Skill Packs (REMOVED - Replaced by Ecosystem v12.7):**
- ❌ Invoice & Quote Desk → Replaced by **Quinn** (Invoicing)
- ❌ Support Switchboard → Replaced by **Eli** (Inbox) + **Sarah** (Front Desk)
- ❌ Scheduling Agent → Folded into **Nora** (Conference)
- ❌ CRM Follow-up → Handled by **Sarah** (routing) + **Eli** (email follow-up)

**Why the change:**
- Ecosystem v12.7 provides complete provider adapters with gateway boundaries
- Pre-built agent personas with consistent governance patterns
- Aligned with platform/control-plane/registry/skillpacks.external.json

---

## Infrastructure

- [ ] `PHASE2-TASK-029` **Skill Pack Factory Implementation**
  - Process `manifest.json`: Load permissions, approvals
  - Validate: All required fields present
  - Register: Tool in LangGraph orchestrator

- [ ] `PHASE2-TASK-030` **Worker Queue Contract**
  - Redis-based task queue
  - Enqueue: Async job (email send, invoice create)
  - Dequeue: Worker processes job
  - Acknowledge: Job complete + receipt generated

- [ ] `PHASE2-TASK-031` **Failure Handling**
  - 3x retry with exponential backoff
  - Initial delay: 1s, max delay: 30s
  - After 3 failures → escalate to human
  - Generate receipt: `outcome = 'failed'`, `reason_code = 'max_retries_exceeded'`

- [ ] `PHASE2-TASK-032` **Audit Logs (Who/What/Cost Tracking)**
  - Log: Every API call (tool, action, cost estimate)
  - Store: `audit_logs` table
  - Query: Cost by suite, tool usage stats

---

## Compliance Architecture

- [ ] `PHASE2-TASK-033` **Subprocessor Compliance Mapping**
  - DocuSign: SOC 2 Type II, ISO 27001, ESIGN/UETA
  - Stripe: PCI DSS Level 1, SOC 2 Type II
  - Google: SOC 2, ISO 27001
  - **Aspire's Role:** Leverage audited partners + implement own controls

- [ ] `PHASE2-TASK-034` **Aspire-Owned Controls**
  - Access Control: RBAC (Suite/Office separation), MFA enforcement
  - Log Retention: 7-year minimum (legal docs), 90-day (operational)
  - Incident Handling: Runbooks, postmortem template
  - Vendor Management: Annual audits, SOC 2 reviews
  - Data Classification: PII identification, encryption at rest/transit
  - Change Management: Git-based, approval gates, rollback

- [ ] `PHASE2-TASK-035` **Evidence Pack**
  - Access control policies
  - Log retention configurations
  - Incident response evidence (postmortems from drills)
  - Vendor audit reports (annual SOC 2 reviews)
  - Control mapping spreadsheet (ISO 27001 Annex A)

---

## Testing

- [ ] `PHASE2-TASK-036` **Certification Suite**
  - TC-01: Bounded Authority (all 10 skill packs)
  - TC-02: Receipt Integrity (100% coverage)
  - TC-03: PII Redaction (zero leakage)

- [ ] `PHASE2-TASK-037` **Integration Tests with Real APIs**
  - Stripe (test mode): Create invoice, verify webhook
  - Gmail (test account): Send email, verify receipt
  - Google Calendar (test account): Create event, verify sync
  - **Pass Criteria:** All tests pass in sandbox mode

- [ ] `PHASE2-TASK-038` **Load Testing**
  - Simulate: 50+ parallel agent executions
  - Measure: p95 latency, error rate, queue depth
  - **Pass Criteria:** p95 <800ms, error rate <1%

---

## SPEC Document Cross-References (NEW - P2 Gap Fix)

**Source:** `docs/`

Detailed specifications for skill pack implementations and workflows:

| SPEC Document | Skill Pack | Purpose |
|---------------|------------|---------|
| `SPEC_Adam_Research_Desk.md` | Adam | Research desk architecture and capabilities |
| `SPEC_Adam_Vendor_Quotes_Workflow.md` | Adam | Vendor quote generation workflow |
| `SPEC_Tec_Document_Production_Agent.md` | Tec | Document production agent specification |
| `SPEC_Business_Google_War_Room.md` | Adam | Business Google war room feature |
| `SPEC_AuthorityQueue_Document_Preview_and_Download.md` | All | Authority Queue preview/download UX |
| `SPEC_HighRisk_Approvals_AvaVideo_Gateway_TrustSpine.md` | All | High-risk approval flow (RED tier) |

**Implementation Tasks:**

- [ ] **PHASE2-TASK-SPEC-001** Reference Adam Research Spec
  - Review `SPEC_Adam_Research_Desk.md` before implementing Adam
  - Follow architecture patterns for vendor discovery
  - Test against specification requirements

- [ ] **PHASE2-TASK-SPEC-002** Reference Tec Production Spec
  - Review `SPEC_Tec_Document_Production_Agent.md` before implementing Tec
  - Follow QC workflow patterns
  - Implement preview-first document generation

- [ ] **PHASE2-TASK-SPEC-003** Reference High-Risk Approval Spec
  - Review `SPEC_HighRisk_Approvals_AvaVideo_Gateway_TrustSpine.md`
  - Implement video escalation for RED tier actions
  - Validate against Gateway + Trust Spine flow

---

## Success Criteria

### Trust Spine Outbox Success Criteria
- [ ] `2-SC-TS-001` All 10 provider adapters pass idempotency tests
- [ ] `2-SC-TS-002` Outbox retry logic works (transient failures → exponential backoff)
- [ ] `2-SC-TS-003` DLQ captures permanent failures (invalid API keys → DLQ)
- [ ] `2-SC-TS-004` All 10 skill packs successfully enqueue jobs (100% success rate)

### Implementation Success Criteria - 10 Ecosystem Skill Packs

**Channel Skill Packs (6) - Weeks 8-14:**
- [ ] `2-SC-001` Sarah Front Desk: Call routing end-to-end working
- [ ] `2-SC-002` Eli Inbox: Email triage + draft responses working
- [ ] `2-SC-003` Quinn Invoicing: Stripe invoice creation working
- [ ] `2-SC-004` Nora Conference: LiveKit video call working
- [ ] `2-SC-005` Adam Research: Vendor search returning results
- [ ] `2-SC-006` Tec Documents: PDF generation working

**Finance Office Skill Packs (3) - Weeks 15-18:**
- [ ] `2-SC-007` Finn Money Desk: Stripe Connect transfer working
- [ ] `2-SC-008` Milo Payroll Desk: Gusto payroll sync working
- [ ] `2-SC-009` Teressa Books Desk: QuickBooks sync working

**Legal Skill Pack (1) - Weeks 19-20:**
- [ ] `2-SC-010` Clara Legal Desk: PandaDoc signature flow working

**Governance Criteria:**
- [ ] `2-SC-011` No tool can bypass approval gates
- [ ] `2-SC-012` All executions generate receipts
- [ ] `2-SC-013` All 10 Skill Packs pass certification tests (100%)

### Memory System Success Criteria

- [ ] `2-MEM-001` 80+ Knowledge Graph entities (integration patterns, API debugging)
- [ ] `2-MEM-002` 35+ skills/ changelog entries
- [ ] `2-MEM-003` Skill pack template reused 4 times without errors
- [ ] `2-MEM-004` Zero webhook signature verification failures

**Source:** `plan/00-success-criteria-index.md`

---

## Related Artifacts

**Created in This Phase:**
- 10 Skill Packs from Ecosystem v12.7:
  - Channel (6): Sarah, Eli, Quinn, Nora, Adam, Tec
  - Finance Office (3): Finn, Milo, Teressa
  - Legal (1): Clara
- Worker queue system (Redis-based)
- Provider adapters with gateway boundaries
- Compliance evidence pack
- Integration tests with real APIs

**Used in Later Phases:**
- Phase 3: Mobile app demonstrates all 10 skill packs
- Phase 4: Hardening includes skill pack evil tests
- Phase 5: Dogfooding uses all 10 skill packs

---

## Related Gates

**No new gates introduced in this phase**

**Existing Gates Validated:**
- Gate 6: Receipts Immutable (all skill packs generate receipts)
- Gate 7: RLS Isolation (contacts table has RLS)

---

## Estimated Duration

**12 weeks** (Week 8-20)

**Timeline - Channel Skill Packs (Weeks 8-14):**
- Weeks 8-9: Sarah (Front Desk) + Eli (Inbox)
- Weeks 10-11: Quinn (Invoicing) + Nora (Conference)
- Weeks 12-13: Adam (Research) + Tec (Documents)
- Week 14: Channel integration testing

**Timeline - Finance Office Skill Packs (Weeks 15-18):**
- Weeks 15-16: Finn (Money Desk) + Milo (Payroll)
- Weeks 17-18: Teressa (Books) + Finance Office integration testing

**Timeline - Legal Skill Pack (Weeks 19-20):**
- Week 19: Clara (Legal Desk) implementation
- Week 20: Final certification tests (all 10 skill packs)

**Note:** Ecosystem v12.7 provides pre-built provider adapters with gateway boundaries, reducing implementation time.

---

## Cost

**$40-80/mo** - API usage during development (10 skill packs)

**Provider Costs (Developer Sandboxes):**
- Stripe: $0/mo (test mode)
- PandaDoc: $0/mo (developer sandbox)
- Gusto: $0/mo (developer sandbox)
- QuickBooks: $0/mo (developer sandbox)
- LiveKit: $0/mo (free tier)
- Exa/Brave: $15/mo (Adam Research queries)
- Polarismail/Zoho: $0/mo (developer tier)

**Shared:**
- OpenAI: $30-50/mo (skill pack operations)
- Perplexity/Context7: $10/mo (documentation queries)

---

## Related Files

- **Main Roadmap:** [Aspire-Production-Roadmap.md](../Aspire-Production-Roadmap.md)
- **Success Criteria:** [00-success-criteria-index.md](../00-success-criteria-index.md)
- **Dependencies:** [00-dependencies.md](../00-dependencies.md)
- **Previous Phase:** [phase-1-orchestrator.md](phase-1-orchestrator.md)
- **Next Phase:** [phase-3-mobile-app.md](phase-3-mobile-app.md)
- **Skill Pack Specs:** [../skill-packs/README.md](../skill-packs/README.md)

---

**Last Updated:** 2026-02-12
**Status:** ⏳ NOT STARTED (waiting for Phase 0C + Phase 1 completion)
