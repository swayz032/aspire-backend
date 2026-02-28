# Skill Pack Index (Ecosystem v12.7)

**Purpose:** Standardized directory for all Aspire Skill Pack specifications from Ecosystem v12.7.

**Last Updated:** 2026-02-04
**Source of Truth:** `platform/control-plane/registry/skillpacks.external.json`

---

## 📦 What is a Skill Pack?

A **Skill Pack** is a governed capability bundle that enables Aspire to perform specific business workflows while maintaining strict approval gates and receipt generation.

**Key Characteristics:**
- **Bounded Authority:** Explicit permission/denial lists (cannot exceed defined scope)
- **Approval Gates:** Yellow/Red tier actions require user confirmation
- **Receipt Coverage:** 100% of actions generate immutable receipts
- **State Machine:** LangGraph sub-graph (Trigger → Prepare → Approve → Execute → Receipt)
- **Certification Required:** Must pass TC-01 (Bounded Authority), TC-02 (Receipt Integrity), TC-03 (PII Redaction)

---

## 📋 Skill Pack Inventory (11 External Skill Packs + 4 Internal Skill Packs)

### Channel Skill Packs (6)

| # | Pack ID | Name | Channel | Primary Integration | Risk Level | Approval Required |
|---|---------|------|---------|---------------------|------------|-------------------|
| 1 | `sarah_front_desk` | Sarah | telephony | LiveKit/Twilio | YELLOW | Call routing to external |
| 2 | `eli_inbox` | Eli | mail | PolarisM | YELLOW | Email send |
| 3 | `quinn_invoices` | Quinn | invoicing | Stripe Connect | YELLOW/RED | Invoice send, Payment charge |
| 4 | `nora_conference` | Nora | conference | LiveKit, Deepgram, ElevenLabs | YELLOW | Email followup, action proposals |
| 5 | `adam_research` | Adam | research | Exa/Brave Search | GREEN | None (read-only research) |
| 6 | `tec_docs` | Tec | documents | Chromium PDF | YELLOW | Document release |

### Finance Office Skill Packs (3)

| # | Pack ID | Name | Channel | Primary Integration | Risk Level | Approval Required |
|---|---------|------|---------|---------------------|------------|-------------------|
| 7 | `finn_money_desk` | Finn | money_movement | Moov/Plaid | RED | All transfers |
| 8 | `milo_payroll` | Milo | payroll | Gusto | RED | Payroll submit |
| 9 | `teressa_books` | Teressa | books | QuickBooks Online | YELLOW | Write operations |

### Legal Skill Pack (1)

| # | Pack ID | Name | Channel | Primary Integration | Risk Level | Approval Required |
|---|---------|------|---------|---------------------|------------|-------------------|
| 10 | `clara_legal` | Clara | legal | PandaDoc | RED | All legal actions |

### Internal Admin Skill Pack (1)

| # | Pack ID | Name | Channel | Primary Integration | Risk Level | Approval Required |
|---|---------|------|---------|---------------------|------------|-------------------|
| 11 | `mail_ops_desk` | Mail Ops | admin | PolarisM | YELLOW | Domain/mailbox changes |

### Internal Ops Skill Packs (4) - Phase 4

| # | Pack ID | Owner | Profile | Risk Level | Approval Required |
|---|---------|-------|---------|------------|-------------------|
| 12 | `sre_triage` | admin | internal_ops | GREEN | Incident operations |
| 13 | `qa_evals` | admin | internal_ops | GREEN | Test operations |
| 14 | `security_review` | admin | internal_security | RED | Security operations |
| 15 | `release_manager` | admin | internal_ops | RED | Deployment operations |

---

## 🎯 Skill Pack Specifications

### 1. Sarah — Front Desk (Telephony)

**Location:** `agent_kits/agent_persona_kit/brain/agents/sarah/`

**Capabilities:**
- Answer inbound calls (GREEN - autonomous)
- Triage and classify caller intent (GREEN - autonomous)
- Route calls to owner/team extensions (YELLOW - requires confirmation for external)
- Create voicemail summaries (GREEN - autonomous)

**Permissions:**
```json
{
  "allow": ["call.answer", "call.route_internal", "voicemail.create", "voicemail.transcribe"],
  "deny": ["call.external_dial", "call.route_external_unconfirmed"]
}
```

**Hard Rules:**
- No unsanctioned external actions
- Uses proposals + approvals via Trust Spine
- Readback for anything that may trigger approvals

**Reference:** `platform/brain/router/rules/sarah_frontdesk_router.yaml`

---

### 2. Eli — Inbox (Mail)

**Location:** `providers/polarismail_inbox/skillpacks/eli_inbox/`

**Capabilities:**
- Read/summarize/classify emails (GREEN - autonomous)
- Create/update drafts (GREEN - autonomous)
- Propose email replies (YELLOW - requires approval)
- Risk scoring (green/yellow/red classification)

**Permissions:**
```json
{
  "allow": ["email.read", "email.draft", "email.classify", "email.summarize"],
  "deny": ["email.send", "email.delete_all"]
}
```

**Hard Rules:**
- **Cannot send email without approval**
- All sends require Trust Spine approval + executor

**Reference:** `providers/polarismail_inbox/skillpacks/eli_inbox/README.md`

---

### 3. Quinn — Invoicing (Stripe Connect)

**Location:** `providers/stripe_invoicing_connect/skillpacks/quinn_invoicing/`

**Capabilities:**
- Draft invoices/quotes (GREEN - autonomous)
- Schedule follow-ups (GREEN - autonomous)
- Send invoices (YELLOW - requires approval)
- Process payments (RED - requires explicit authority)

**Permissions:**
```json
{
  "allow": ["invoice.create", "quote.create", "email.draft", "followup.schedule"],
  "deny": ["payments.charge_unlimited", "invoice.send_unconfirmed"]
}
```

**Hard Rules:**
- **Never calls Stripe directly** - produces proposals via Trust Spine
- NEW invoice client onboarding => Ava video required
- Invoice send > $5k => Ava video required
- Every step writes a receipt

**Reference:** `providers/stripe_invoicing_connect/skillpacks/quinn_invoicing/README.md`

---

### 4. Nora — Conference (LiveKit Video)

**Location:** `skillpacks/nora-conference/`

**Capabilities:**
- Silent Scribe mode: captures meeting structure (GREEN - autonomous)
- Produce Recap Packets (GREEN - autonomous draft)
- Create Proposed Actions (YELLOW - requires approval)
- Route to specialists via Ava (e.g., ask Eli for email)

**Permissions:**
```json
{
  "allow": ["meeting.recap", "meeting.transcript", "action.propose", "specialist.request"],
  "deny": ["action.execute", "email.send", "calendar.write", "money.move"]
}
```

**Operating Modes:**
- Silent Scribe (default): no interruptions
- Active Assistant: when directly addressed ("Nora")
- Risk Alert: governance-critical warnings only

**Audio Profiles:**
- STT: Deepgram Flux
- TTS: ElevenLabs Flash v2.5

**Reference:** `skillpacks/nora-conference/manifest.json`

---

### 5. Adam — Research (Exa/Brave Search)

**Location:** `agent_kits/agent_persona_kit/brain/agents/adam/`

**Capabilities:**
- Web research with evidence capture (GREEN - autonomous)
- Vendor discovery (GREEN - autonomous)
- Generate ResearchPackets (GREEN - autonomous)
- Generate VendorOutreachPackets (GREEN - draft only)

**Permissions:**
```json
{
  "allow": ["search.web", "search.vendor", "evidence.capture", "packet.generate"],
  "deny": ["external.action", "email.send", "money.move"]
}
```

**Persona:** Crisp, practical research analyst
- Prefers primary sources
- Clear about uncertainty
- Writes for decision-making

**Reference:** `agent_kits/agent_persona_kit/brain/agents/adam/`

---

### 6. Tec — Documents (PDF Production)

**Location:** `docs/SPEC_Tec_Document_Production.md`

**Capabilities:**
- Generate structured DocumentPacket (GREEN - autonomous)
- Create previews (GREEN - autonomous)
- Release final PDF (YELLOW - requires Authority Queue approval)

**Permissions:**
```json
{
  "allow": ["doc.draft", "doc.preview", "template.use"],
  "deny": ["doc.release_unconfirmed", "doc.external_send"]
}
```

**Hard Rules:**
- **LLM writes content; renderer owns layout**
- **Preview-first; release requires approval**
- **Fail-closed:** Missing fields, unknown claims → clarification request
- **Receipts everywhere:** Draft, preview, approval, release all receipted

**Architecture:**
1. Tec Skill Pack: Inputs DocumentRequest → Outputs DocumentPacket
2. Document Renderer Service: Deterministic templates + headless Chromium
3. Authority Queue: Approve → capability token → PDF render → download link

**Reference:** `docs/SPEC_Tec_Document_Production.md`

---

### 7. Finn — Money Desk (Moov/Plaid)

**Location:** `platform/finance-office/money-movement/`

**Capabilities:**
- Propose business transfers (RED - requires approval)
- Propose owner draw transfers (RED - requires approval)
- Generate money movement snapshots (GREEN - autonomous)
- Raise exceptions (cash buffer risk, new destination review)

**Permissions:**
```json
{
  "allow": ["snapshot.generate", "exception.raise", "transfer.propose"],
  "deny": ["transfer.execute", "account.create"]
}
```

**Hard Rules:**
- Execution is always gated via Ava approvals + Trust Spine receipts + Gateway
- Refunds, tax-reserve automation, vendor/AP automation out of scope for v1

**Reference:** `platform/finance-office/money-movement/README.md`

---

### 8. Milo — Payroll Desk (Gusto)

**Location:** `skillpacks/milo-payroll/` + `platform/finance-office/payroll/`

**Capabilities:**
- Generate payroll snapshots (GREEN - autonomous)
- Raise payroll exceptions (GREEN - autonomous)
- Create payroll proposals (RED - requires approval)

**Permissions:**
```json
{
  "allow": ["read_payroll_status", "raise_exceptions", "draft_proposals"],
  "deny": ["payroll_submit", "comp_changes", "tax_settings", "bank_funding"]
}
```

**Hard Rules:**
- **Milo never submits payroll directly**
- Produces evidence-backed proposals → Authority Queue → Ava approval → Outbox execution → receipts
- Users do not chat with Milo directly; Ava orchestrates

**High Risk Triggers:** payroll_submit, comp_changes, tax_settings, bank_funding

**Reference:** `skillpacks/milo-payroll/manifest.json`

---

### 9. Teressa — Books Desk (QuickBooks Online)

**Location:** `platform/finance-office/books/`

**Capabilities:**
- Ingest QBO changes via webhooks (GREEN - autonomous)
- Generate plain-English exceptions (GREEN - autonomous)
- Draft proposals to fix issues (YELLOW - requires approval)

**Permissions:**
```json
{
  "allow": ["qbo.read", "exception.generate", "proposal.draft"],
  "deny": ["qbo.write_direct", "categorize.auto_execute"]
}
```

**Hard Rules:**
- **Teressa never performs direct writes to QBO**
- Only approved proposals can be executed by the Gateway
- Routes proposals to Authority Queue for Ava approval
- Emits Trust Spine receipts for every state transition

**Reference:** `platform/finance-office/books/README.md`

---

### 10. Clara — Legal Desk (PandaDoc)

**Location:** `platform/trust-spine/06_ADDONS/LEGAL_DESK_CLARA_V1/`

**Capabilities:**
- Create document from template (RED - requires approval)
- Assign signer roles (RED - requires approval)
- Send for signature (RED - requires approval)
- Void/cancel documents (RED - requires approval)
- Track statuses via webhooks (GREEN - autonomous)

**Permissions:**
```json
{
  "allow": ["intake.analyze", "draft.create", "status.track", "webhook.handle"],
  "deny": ["signature.send_unconfirmed", "document.void_unconfirmed"]
}
```

**Hard Rules:**
- **Clara is back-of-house** (users do not chat directly)
- **Template allow-list + versioning enforced**
- **Ava Video approvals for ALL legal actions**
- **Receipts for every lifecycle transition**
- **No direct side-effects without approval**

**What Counts as Legal Action:**
- create draft document
- send for signature
- void/cancel
- change recipients/roles
- resend reminders
- export final PDFs
- publish template versions

**Reference:** `platform/trust-spine/06_ADDONS/LEGAL_DESK_CLARA_V1/README.md`

---

### 11. mail_ops_desk — Internal Mail Admin (PolarisM)

**Location:** `platform/control-plane/registry/agents/mail_ops_desk.json`
**State Machine:** `platform/brain/state_machines/mail_ops_triage.yaml`

**⚠️ INTERNAL ADMIN ONLY** - NOT user-facing. Handles PolarisM mail infrastructure.

**Capabilities:**
- Add domain to suite (YELLOW - requires approval)
- Verify domain DNS (SPF/DKIM/DMARC) (GREEN - autonomous)
- Create mailbox for office (YELLOW - requires approval)
- Rotate mailbox password (YELLOW - requires approval, secret never returned)
- Suspend mailbox (YELLOW - requires approval)
- Open incidents for mail delivery failures (GREEN - autonomous)

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

**Reference:** `platform/control-plane/registry/agents/mail_ops_desk.json`

---

## 🔧 Internal Skill Packs (Admin/Ops) - Phase 4

**Source:** `platform/control-plane/registry/skillpacks.internal.json`

These skill packs are for admin/operations teams, NOT user-facing.

### 12. sre_triage — SRE Incident Triage

**Capabilities:**
- Detect incidents from monitoring alerts (GREEN)
- Triage and classify incidents by severity (GREEN)
- Route incidents to appropriate responders (GREEN)
- Generate incident reports (GREEN)

**Permissions:**
```json
{
  "allow": ["incident.detect", "incident.triage", "incident.route", "report.generate"],
  "deny": ["production.modify", "data.access"]
}
```

### 13. qa_evals — QA Evaluation Runs

**Capabilities:**
- Execute eval suites on skill packs (GREEN)
- Generate quality reports (GREEN)
- Track eval trends over time (GREEN)
- Flag quality regressions (GREEN)

**Permissions:**
```json
{
  "allow": ["eval.execute", "report.generate", "trend.track", "regression.flag"],
  "deny": ["code.modify", "deploy.execute"]
}
```

### 14. security_review — Security Review Automation

**Capabilities:**
- Run security scans on code changes (GREEN)
- Generate security reports (GREEN)
- Flag security violations (RED - requires approval for blocking)
- Request human review for sensitive changes (RED)

**Permissions:**
```json
{
  "allow": ["scan.execute", "report.generate", "violation.flag", "review.request"],
  "deny": ["code.approve", "deploy.execute", "secrets.access"]
}
```

### 15. release_manager — Release Management

**Capabilities:**
- Track release readiness (GREEN)
- Enforce release checklists (GREEN)
- Coordinate deployment pipeline (RED - requires approval)
- Generate release notes (GREEN)

**Permissions:**
```json
{
  "allow": ["checklist.enforce", "pipeline.track", "notes.generate"],
  "deny": ["deploy.direct", "rollback.direct"]
}
```

---

## 🔒 Security & Governance Requirements

### Universal Requirements (All Skill Packs)

1. **Bounded Authority**
   - Explicit permission lists (whitelist approach)
   - Deny lists for dangerous actions
   - No permissions creep (cannot expand scope without manifest update)

2. **Approval Gates**
   - GREEN tier: Autonomous (read-only, safe writes)
   - YELLOW tier: User confirmation required
   - RED tier: Explicit authority UI with video confirmation

3. **Receipt Coverage**
   - 100% of state-changing actions generate receipts
   - Receipt includes: intent, plan, outcome, reason_code (if denied/failed)
   - PII redacted before logging (Presidio DLP integration)

4. **Failure Handling**
   - Tool failures: 3x retry with exponential backoff
   - Auth failures: Pause workflow, request re-auth via dashboard
   - Timeout enforcement: <5s for tool calls, <30s for orchestrator

5. **Testing**
   - Certification suite: TC-01, TC-02, TC-03 (all must pass)
   - Integration tests with real APIs (sandbox/test mode)
   - Load testing (50+ parallel executions)

---

## 🔗 Related Files

- **Registry:** `platform/control-plane/registry/skillpacks.external.json`
- **Staff Catalog:** `docs/STAFF_CATALOG_CURRENT.md`
- **Main Roadmap:** [Aspire-Production-Roadmap.md](../Aspire-Production-Roadmap.md)
- **Phase 2:** [phase-2-founder-mvp.md](../phases/phase-2-founder-mvp.md)

---

## ✅ Skill Pack Checklist (Manufacturing Process)

Use this checklist for each new skill pack:

### Planning
- [ ] Manifest definition complete (from ecosystem registry)
- [ ] Specification document created
- [ ] Permission lists defined (allow/deny)
- [ ] Approval gates identified (YELLOW/RED actions)
- [ ] Receipt fields specified

### Implementation
- [ ] LangGraph sub-graph implemented (state machine)
- [ ] Tool integrations complete (API clients)
- [ ] Failure handling implemented (retries, escalation)
- [ ] Receipt generation integrated (100% coverage)

### Testing
- [ ] TC-01: Bounded Authority test passes
- [ ] TC-02: Receipt Integrity test passes
- [ ] TC-03: PII Redaction test passes
- [ ] Integration tests with real API (sandbox mode)
- [ ] Load testing (50+ parallel executions)
- [ ] Latency targets verified (<800ms)

### Certification
- [ ] Security review completed
- [ ] Code review completed
- [ ] Documentation complete
- [ ] Deployment approved

---

**Last Verified:** 2026-02-04
**Source:** Aspire Ecosystem v12.7
