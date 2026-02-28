---
phase: "0A"
name: "Laptop-Compatible Prep"
status: "complete"
status_date: "2026-02-04"
blocking_phase: null
blocks_phases: ["0B"]
duration_estimate: "2-3 days (accelerated via handoff - was 2 weeks)"
gates_satisfied: []
priority: "high"
hardware_required: "HP Laptop (current interim machine)"
cost: "$0/mo (all free tiers during development)"
handoff_provides: "24 production database migrations (933 lines SQL) + Trust Manual + receipts verifier + Trust Spine substrate reference"
api_services_verified: 19
---

# PHASE 0A: Laptop-Compatible Prep - ✅ COMPLETE (2026-02-04)

## Objective

Complete all cloud, planning, and design work on HP laptop while waiting for Skytech Tower hardware arrival.

**✅ PHASE COMPLETE**: All 19 cloud accounts operational and API keys verified. Ready for Phase 0B.

**⚠️ HARDWARE NOTE**: This phase is designed to maximize productivity during the Skytech Tower wait period. All tasks can be completed on the HP laptop without requiring high-performance hardware.

---

## Dependencies

**Requires (Blocking):**
- None (can start immediately)

**Blocks (Downstream):**
- Phase 0B: Skytech Tower Setup (needs cloud accounts + schemas ready)

---


## Handoff Package Integration

**Status:** ✅ PRODUCTION-READY SCHEMAS AVAILABLE

**What's Included:**
- 24 Supabase migrations (933 lines SQL)

---

## Trust Spine Ecosystem Reference (MINIMAL)

**📚 Trust Spine substrate will be deployed in Phase 0B (not Phase 0A):**

**Note:** Trust Spine substrate will be deployed in Phase 0B: apply Trust Spine canonical migrations per MIGRATION_ORDER_ADDON.md (see CANONICAL_PATHS.md for exact paths and counts), deploy 5 core Edge Functions + optional A2A addon (7 migrations + 3 Edge Functions), Go verification service. Phase 0A provides the cloud foundation (Supabase project) that Trust Spine will use.

**For Trust Spine documentation, see:**
- `Trust-Spine-Package-Navigation.md` (in this plan directory) - Complete navigation guide
- Phase 0B documentation for Trust Spine deployment procedures

---
- Trust Manual documentation  
- Receipts verifier (Go implementation)

**Time Saved:** 2 weeks → 2-3 days

---

## Tasks

### 1. Cloud Infrastructure Setup (High Priority - ✅ ALL 19 SERVICES VERIFIED)

**Status:** ✅ COMPLETE - All 19 cloud accounts operational and API keys verified (2026-02-04)

#### Core Infrastructure (4 Services)

- [x] `PHASE0A-TASK-001` **Supabase Project** ✅ READY
  - Database, Auth, Edge Functions
  - Connection string configured
  - RLS enabled
  - **Purpose:** Trust Spine substrate, state persistence

- [x] `PHASE0A-TASK-002` **Upstash Redis** ✅ READY
  - Redis queues for outbox pattern
  - REST API endpoint configured
  - **Purpose:** Job queues, caching

- [x] `PHASE0A-TASK-003` **AWS S3 Bucket** ✅ READY
  - Receipts storage configured
  - CORS policy set
  - IAM credentials generated
  - **Purpose:** Receipt artifacts, blob storage

- [x] `PHASE0A-TASK-004-A` **OpenAI API** ✅ READY
  - GPT-5 API key configured
  - Spending limits set
  - **Purpose:** LLM orchestration, Brain

#### LLM & AI Services (4 Services)

- [x] `PHASE0A-TASK-004-B` **Anthropic/Claude** ✅ READY
  - API key configured
  - **Purpose:** Meeting of Minds, Council decisions

- [x] `PHASE0A-TASK-004-C` **Google Gemini** ✅ READY
  - API key configured
  - **Purpose:** Meeting of Minds, multi-LLM council

- [x] `PHASE0A-TASK-004-D` **Brave Search** ✅ READY
  - API key configured
  - **Purpose:** Adam Research, web search

- [x] `PHASE0A-TASK-004-E` **Tavily** ✅ READY
  - API key configured
  - **Purpose:** Adam Research, web search fallback

#### Financial Services (4 Services)

- [x] `PHASE0A-TASK-005` **Stripe** ✅ READY
  - Test mode activated
  - Invoice API ready
  - **Purpose:** Quinn Invoicing, Finn Money Desk
  - **Skill Packs:** Quinn, Finn

- [x] `PHASE0A-TASK-005-B` **Plaid** ✅ READY
  - Bank connections configured
  - **Purpose:** Finn Money Desk, bank linking
  - **Skill Pack:** Finn

- [x] `PHASE0A-TASK-005-C` **Moov** ✅ READY
  - Money transfer API configured
  - **Purpose:** Finn Money Desk, transfers
  - **Skill Pack:** Finn

- [x] `PHASE0A-TASK-005-D` **QuickBooks** ✅ READY
  - OAuth configured
  - **Purpose:** Teressa Books, accounting sync
  - **Skill Pack:** Teressa

#### Communication Services (4 Services)

- [x] `PHASE0A-TASK-006` **LiveKit Cloud** ✅ READY
  - Video/voice telephony configured
  - Phone numbers feature ready
  - **Purpose:** Nora Conference, Sarah Front Desk
  - **Skill Packs:** Nora, Sarah

- [x] `PHASE0A-TASK-006-B` **Twilio** ✅ READY
  - Telephony, SMS configured
  - **Purpose:** Sarah Front Desk, call routing
  - **Skill Pack:** Sarah

- [x] `PHASE0A-TASK-006-C` **Deepgram** ✅ READY
  - Speech-to-text configured
  - **Purpose:** Nora Conference, transcription
  - **Skill Pack:** Nora

- [x] `PHASE0A-TASK-006-D` **ElevenLabs** ✅ READY
  - Text-to-speech configured
  - **Purpose:** Ava voice, responses
  - **Skill Pack:** Ava

#### Specialized Services (4 Services)

- [x] `PHASE0A-TASK-007` **PolarisM Mail** ✅ READY (was Zoho research)
  - White-label email configured
  - Domain management ready
  - **Purpose:** Eli Inbox, mail_ops_desk
  - **Skill Packs:** Eli, mail_ops_desk

- [x] `PHASE0A-TASK-007-B` **Anam** ✅ READY
  - Avatar integration configured
  - **Purpose:** Ava avatar, video presence
  - **Skill Pack:** Ava

- [x] `PHASE0A-TASK-007-C` **PandaDoc** ✅ READY
  - Contract/signature API configured
  - **Purpose:** Clara Legal, contracts
  - **Skill Pack:** Clara

- [x] `PHASE0A-TASK-007-D` **Gusto** ✅ READY
  - Payroll integration configured
  - **Purpose:** Milo Payroll
  - **Skill Pack:** Milo

---

### 2. Repository & Project Structure (High Priority)

- [ ] `PHASE0A-TASK-008` **Git Repository Initialization**
  - Create local repository: `~/Projects/aspire`
  - Initialize git: `git init`
  - Create `.gitignore` (Node, Python, .env, OS files)
  - Create initial README.md
  - Create LICENSE file (choose appropriate license)
  - Initial commit: "Initial commit: Aspire production roadmap"

- [ ] `PHASE0A-TASK-009` **Monorepo Structure**
  - Create directory structure:
    ```
    aspire/
    ├── apps/              # Applications (mobile, web)
    ├── backend/           # Backend services
    │   ├── orchestrator/  # LangGraph Brain
    │   ├── skill-packs/   # Skill pack implementations
    │   └── mcp-servers/   # MCP tool servers
    ├── packages/          # Shared packages
    ├── infra/             # Infrastructure as code
    │   ├── schemas/       # Database schemas
    │   └── terraform/     # Cloud infrastructure
    ├── docs/              # Documentation
    │   ├── architecture/  # System design
    │   ├── invariants/    # Immutable laws
    │   └── skill-packs/   # Skill pack specs
    └── tests/             # Test suites
        ├── unit/
        ├── integration/
        └── evil/          # RLS isolation tests
    ```
  - Create placeholder README.md in each directory
  - Commit structure: "Add monorepo directory structure"

---

### 3. Database Schema Design (High Priority)

**Location:** `infra/schemas/`

- [ ] `PHASE0A-TASK-010` **Receipts Schema** (`receipts.sql`)
  - Immutable audit trail (NO UPDATE/DELETE privileges)
  - Hash-chained integrity (SHA-256)
  - 14+ mandatory fields (correlation_id, suite_id, office_id, action_type, etc.)
  - RLS policies for multi-tenant isolation
  - PII redaction integration points (Presidio DLP)
  - Auto-calculate hash trigger
  - Verification functions (hash chain validation)
  - **Reference:** `plan/artifacts/receipts-schema.sql` (already created)

- [ ] `PHASE0A-TASK-011` **Checkpoints Schema** (`checkpoints.sql`)
  - LangGraph state persistence
  - Thread-based workflow tracking
  - Pause/resume support (approval gates)
  - RLS policies (multi-tenant isolation)
  - Auto-expiration (7 days default)
  - Cleanup functions (expired checkpoints)
  - **Reference:** `plan/artifacts/checkpoints-schema.sql` (already created)

- [ ] `PHASE0A-TASK-012` **Identity & RLS Schema** (`identity.sql`)
  - Suites table (organizations/tenants)
  - Offices table (individuals within suites)
  - RLS policies (zero cross-tenant leakage)
  - Session context helpers
  - Evil test suite (cross-tenant SELECT tests)
  - Soft delete support (preserve receipts)
  - **Reference:** `plan/artifacts/identity-rls-schema.sql` (already created)

- [ ] `PHASE0A-TASK-013` **Capability Tokens Schema** (`capability-tokens.sql`)
  - Short-lived tokens (<60s expiry enforced)
  - HMAC-SHA256 signatures
  - Server-side validation
  - Token minting functions
  - Revocation support (individual + emergency kill switch)
  - Usage tracking & analytics
  - **Reference:** `plan/artifacts/capability-tokens-schema.sql` (already created)

---

### 4. Architecture Documentation (Medium Priority)

**Location:** `docs/`

- [ ] `PHASE0A-TASK-014` **7 Immutable Laws** (`docs/invariants/7-immutable-laws.md`)
  - Document Aspire Laws 1-7 (constitution)
  - Aspire Law #1: Single Brain Authority
  - Aspire Law #2: No Action Without Receipt
  - Aspire Law #3: Fail Closed (Default Deny)
  - Aspire Law #4: Green/Yellow/Red Risk Tiers
  - Aspire Law #5: Capability Tokens (Least Privilege)
  - Aspire Law #6: Tenant Isolation (Suite/Office Zoning)
  - Aspire Law #7: Tools Are Hands (Not Brains)
  - Include examples and violation scenarios
  - **Reference:** `CLAUDE.md` (already exists)

- [ ] `PHASE0A-TASK-015` **System Architecture Overview** (`docs/architecture/system-overview.md`)
  - High-level architecture diagram (ASCII art + Mermaid)
  - Single Brain principle explanation
  - Hub & Spoke pattern (all integrations through Brain)
  - Operator → Ava → Brain → Tools → Receipt flow
  - Component responsibilities
  - Technology stack overview

- [ ] `PHASE0A-TASK-016` **Development Workflow** (`docs/development-workflow.md`)
  - Git branching strategy (feature branches, main = production)
  - Commit message format (conventional commits)
  - Pull request process
  - Testing requirements (unit, integration, evil tests)
  - Code review checklist
  - Deployment process

---

### 5. Skill Pack Manifest Design (Medium Priority)

**Location:** `docs/skill-packs/`

- [ ] `PHASE0A-TASK-017` **Quinn Invoicing Manifest** (`quinn-invoicing-manifest.json`)
  - Pack ID: `quinn_invoices`
  - Channel: invoicing
  - Permissions: `allow` (invoice.create, quote.create, email.draft), `deny` (payments.charge_unlimited)
  - Approval gates: YELLOW (invoice.send), RED (payments.charge, new_client_onboard, invoice>$5k)
  - Required receipt fields
  - Certification tests (TC-01, TC-02, TC-03)
  - Stripe Connect integration
  - **Reference:** `providers/stripe_invoicing_connect/skillpacks/quinn_invoicing/`

- [ ] `PHASE0A-TASK-018` **Sarah Front Desk Manifest** (`sarah-frontdesk-manifest.json`)
  - Pack ID: `sarah_front_desk`
  - Channel: telephony
  - Permissions: `allow` (call.answer, call.route, voicemail.create), `deny` (call.external_dial)
  - Approval gates: YELLOW (call.route to external)
  - LiveKit/Twilio integration
  - **Reference:** `platform/control-plane/registry/skillpacks.external.json`

- [ ] `PHASE0A-TASK-019` **Eli Inbox Manifest** (`eli-inbox-manifest.json`)
  - Pack ID: `eli_inbox`
  - Channel: mail
  - Permissions: `allow` (email.read, email.draft, email.classify), `deny` (email.send without approval)
  - Approval gates: YELLOW (email.send)
  - Zoho Mail/Gmail integration
  - **Reference:** `providers/zoho_mail_inbox/skillpacks/eli_inbox/`

- [ ] `PHASE0A-TASK-020` **Nora Conference Manifest** (`nora-conference-manifest.json`)
  - Pack ID: `nora_conference`
  - Channel: conference
  - Permissions: `allow` (meeting.recap, meeting.transcript, action.propose), `deny` (action.execute)
  - Approval gates: YELLOW (email.followup), RED (money.movement)
  - LiveKit video integration, Deepgram STT, ElevenLabs TTS
  - **Reference:** `skillpacks/nora-conference/manifest.json`

- [ ] `PHASE0A-TASK-020B` **Adam Research Manifest** (`adam-research-manifest.json`)
  - Pack ID: `adam_research`
  - Channel: research
  - Permissions: `allow` (search.web, search.vendor, evidence.capture), `deny` (external.action)
  - Approval gates: GREEN (research only)
  - Exa/Brave search integration
  - **Reference:** `agent_kits/agent_persona_kit/brain/agents/adam/`

- [ ] `PHASE0A-TASK-020C` **Tec Documents Manifest** (`tec-docs-manifest.json`)
  - Pack ID: `tec_docs`
  - Channel: documents
  - Permissions: `allow` (doc.draft, doc.preview), `deny` (doc.release without approval)
  - Approval gates: YELLOW (doc.release)
  - PDF generation via Chromium
  - **Reference:** `docs/SPEC_Tec_Document_Production.md`

---

### 6. Learning & Research (Ongoing, Low Pressure)

- [ ] `PHASE0A-TASK-021` **LangGraph Documentation**
  - Study state machines, checkpoints
  - Understand StateGraph vs MessageGraph
  - Review checkpoint/state persistence patterns
  - Explore approval node patterns (wait for human input)
  - Documentation: Notes → `docs/research/langgraph-notes.md`

- [ ] `PHASE0A-TASK-022` **MCP Protocol Specification**
  - Study MCP tool invocation protocol
  - Understand capability token integration
  - Review MCP server patterns
  - Explore error handling best practices
  - Documentation: Notes → `docs/research/mcp-notes.md`

- [ ] `PHASE0A-TASK-023` **OpenAI API (GPT-5)**
  - Study function calling patterns
  - Understand system prompts engineering
  - Review streaming responses
  - Explore token optimization strategies
  - Documentation: Notes → `docs/research/openai-api-notes.md`

- [ ] `PHASE0A-TASK-024` **LiveKit Phone Numbers + Agents**
  - Research telephony integration
  - Understand voice-to-text (Whisper)
  - Review text-to-speech options
  - Explore call state management
  - Documentation: Notes → `docs/research/livekit-notes.md`

- [ ] `PHASE0A-TASK-025` **Stripe Invoice API**
  - Study OAuth flow for connected accounts
  - Understand test mode vs production
  - Review webhook handling (invoice.sent, payment.succeeded)
  - Explore idempotency keys (prevent duplicate charges)
  - Documentation: Notes → `docs/research/stripe-notes.md`

- [ ] `PHASE0A-TASK-026` **PolarisM Mail White-Label**
  - Research reseller program details
  - Understand domain verification process
  - Review SMTP configuration
  - Explore API capabilities (programmatic email)
  - Documentation: Findings → `docs/integrations/polarism-mail.md`

---

### 7. Code Scaffolding (Optional, Low Priority)

**Location:** `backend/orchestrator/`

- [ ] `PHASE0A-TASK-027` **LangGraph Orchestrator Skeleton** (`brain.py`)
  - Basic StateGraph setup
  - Node stubs (intake, validate, plan, approve, execute, receipt)
  - Checkpoint configuration (Postgres)
  - Entry point function
  - **Note:** Implementation in Phase 1, scaffolding only here

- [ ] `PHASE0A-TASK-028` **Ava Integration Skeleton** (`ava.py`)
  - OpenAI client initialization
  - System prompt template
  - Completion function stub
  - Error handling placeholder
  - **Note:** Implementation in Phase 1, scaffolding only here

- [ ] `PHASE0A-TASK-029` **Receipt Generator Skeleton** (`receipts.py`)
  - Receipt model (Pydantic schema)
  - Hash calculation function stub
  - Database insert function stub
  - PII redaction placeholder (Presidio integration point)
  - **Note:** Implementation in Phase 1, scaffolding only here

---

## Success Criteria

### Implementation Success Criteria

- [x] `0A-SC-001` **All 19 cloud accounts operational and tested** ✅ COMPLETE (2026-02-04)
  - Core Infrastructure: Supabase, Upstash, AWS S3, OpenAI (4)
  - LLM/AI Services: Anthropic/Claude, Google Gemini, Brave Search, Tavily (4)
  - Financial Services: Stripe, Plaid, Moov, QuickBooks (4)
  - Communication Services: LiveKit, Twilio, Deepgram, ElevenLabs (4)
  - Specialized Services: PolarisM, Anam, PandaDoc, Gusto (4)
  - **TOTAL: 19 services verified**
- [ ] `0A-SC-002` Repository initialized with monorepo structure
- [ ] `0A-SC-003` All core database schemas designed (SQL files ready)
- [x] `0A-SC-004` **All 11 Skill Pack manifests designed** (was 4, now 11: Sarah, Eli, Quinn, Nora, Adam, Tec, Finn, Milo, Teressa, Clara, mail_ops_desk)
- [ ] `0A-SC-005` System Invariants documented (1-page constitution)
- [ ] `0A-SC-006` Architecture diagram created
- [ ] `0A-SC-007` LangGraph, MCP, OpenAI API understanding achieved
- [x] `0A-SC-008` **LiveKit + PolarisM white-label research complete** ✅ API keys verified

### Memory System Success Criteria

- [ ] `0A-MEM-001` 10+ Knowledge Graph entities created (infrastructure setup patterns)
- [ ] `0A-MEM-002` 5+ STYLE.md rules documented (coding standards established)
- [ ] `0A-MEM-003` 3+ SAFETY.md rules documented (security baselines set)
- [ ] `0A-MEM-004` Session reflection generates proposals automatically on session end

**Source:** `plan/00-success-criteria-index.md`

---

## Related Artifacts

**Created in This Phase:**
- Cloud accounts: Supabase, Upstash, AWS S3, OpenAI, Stripe, LiveKit, PolarisM (research)
- Git repository: `~/Projects/aspire` (monorepo structure)
- Database schemas: `receipts.sql`, `checkpoints.sql`, `identity.sql`, `capability-tokens.sql`
- Skill pack manifests: Sarah (Front Desk), Eli (Inbox), Quinn (Invoicing), Nora (Conference), Adam (Research), Tec (Documents), Finn (Money), Milo (Payroll), Teressa (Books), Clara (Legal)
- Documentation: 7 Immutable Laws, System Architecture, Development Workflow

**Used in Later Phases:**
- Phase 0B: Cloud accounts, schemas (deployed to Postgres)
- Phase 1: Database schemas (full implementation), Skill pack manifests
- Phase 2: Skill pack manifests (integration work)

---

## Related Gates

**No gates required for Phase 0A** (planning/design phase only)

**Gates Prepared:**
- Gate 6: Receipts Immutable (schema designed, implemented in Phase 1)
- Gate 7: RLS Isolation (schema designed, implemented in Phase 1)

---

## Estimated Duration

**Part-time:** ~10-15 hours/week, approximately 2 weeks

**Timeline:**
- Week 1: Cloud setup, repository structure, schema design (15 hours)
- Week 2: Documentation, manifests, research (10 hours)

---

## Cost

**$0/mo** - All services use free tiers during development phase

**Breakdown (19 Services):**

**Core Infrastructure (4):**
- Supabase: $0/mo (free tier: 500MB database, 2GB egress)
- Upstash: $0/mo (free tier: 10k commands/day)
- AWS S3: $0/mo (free tier: 5GB storage, 20k GET requests)
- OpenAI: Pay-per-use (set $100/mo limit, actual usage ~$10-20 during development)

**LLM & AI (4):**
- Anthropic/Claude: Pay-per-use ($0-10/mo during development)
- Google Gemini: $0/mo (free tier)
- Brave Search: $0/mo (free tier: 2k/mo)
- Tavily: $0/mo (free tier)

**Financial Services (4):**
- Stripe: $0/mo (test mode, no charges)
- Plaid: $0/mo (sandbox, 100 items)
- Moov: $0/mo (sandbox)
- QuickBooks: $0/mo (developer sandbox)

**Communication Services (4):**
- LiveKit: $0/mo (free tier: 10k minutes/month)
- Twilio: $0/mo (trial credits)
- Deepgram: $0/mo (free tier: $200 credit)
- ElevenLabs: $0/mo (free tier)

**Specialized Services (4):**
- PolarisM: $0/mo (research only)
- Anam: $0/mo (developer tier)
- PandaDoc: $0/mo (developer sandbox)
- Gusto: $0/mo (developer sandbox)

---

## Trust Spine Integration Reference

**Note:** Trust Spine substrate will be deployed in Phase 0B: apply Trust Spine canonical migrations per MIGRATION_ORDER_ADDON.md (see CANONICAL_PATHS.md for exact paths and counts), deploy 5 core Edge Functions + optional A2A addon (7 migrations + 3 Edge Functions), Go verification service. Phase 0A provides the cloud foundation (Supabase project) that Trust Spine will use.

---

## Notes

**Hardware Context:**
- This phase uses HP laptop (interim machine)
- Skytech Tower arrives during Phase 0B
- All tasks designed to run on low-spec hardware
- No GPU/CUDA required for planning work

**Blocking Notes:**
- Phase 0B cannot start until cloud accounts are ready
- Database schemas must be designed before deploying to Postgres in Phase 0B
- Skill pack manifests guide Phase 2 implementation

---

## Related Files

- **Main Roadmap:** [Aspire-Production-Roadmap.md](../Aspire-Production-Roadmap.md)
- **Success Criteria:** [00-success-criteria-index.md](../00-success-criteria-index.md)
- **Dependencies:** [00-dependencies.md](../00-dependencies.md)
- **Next Phase:** [phase-0b-tower-setup.md](phase-0b-tower-setup.md)

---

**Last Updated:** 2026-02-04
**Status:** ✅ COMPLETE (All 19 API services verified)
