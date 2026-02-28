# Aspire-Infra Plugin Development Roadmap

**Purpose:** Custom Claude Code plugin for operational automation and verification workflows

**Created:** 2026-01-04

**Confidence:** 92% (Based on Sequential Thinking analysis)

---

## 🎯 EXECUTIVE SUMMARY

The aspire-infra plugin fills the **operational automation gap** in Aspire's governance model:

- **Existing Governance (CLAUDE.md, 8 agents, 3 skills):** Design-time governance, phase-gate reviews, architectural validation
- **aspire-infra Plugin (NEW):** PR-level automation, foot-gun blocking, fast feedback loops, "seatbelt" enforcement

**These are complementary layers, not duplicates.**

---

## 🧠 TRIPLE-MEMORY STRATEGY (Knowledge Graph + Serena + Reflection)

**INSTALLED:** 2026-01-10 | **Status:** ✅ ACTIVE

Aspire uses **three complementary memory systems** to eliminate cross-session amnesia and ensure continuous learning:

### 1. Knowledge Graph MCP (`mcp__memory__*`)
- **Purpose:** Persistent cross-session solution caching
- **Storage:** Structured entities (debugging solutions, code patterns) + Relations
- **Lifespan:** Permanent (survives sessions)
- **Use Case:** "How did we solve the RLS bug last time?" → Query Knowledge Graph → Retrieve verified solution
- **Example:** Entity type="debugging_solution" with observations=["Set RLS context before DB queries", "Use SET LOCAL for transaction scope"]

### 2. Serena Memory (Autonomous MCP)
- **Purpose:** Session-based code navigation, symbol tracking, working memory
- **Storage:** File changes, symbol references, operation history within current session
- **Lifespan:** Single session (ephemeral, cleared on session end)
- **Use Case:** "Which files did I modify?" → Query Serena → Get list of changed files + symbols
- **Example:** Session context = {files: ["receipts.py", "handlers.py"], symbols: ["generate_receipt", "RLSContext"], patterns: ["added RLS validation"]}

### 3. Session Reflection Pack (NEW - INSTALLED 2026-01-10)
- **Purpose:** Capture corrections → Generate governance rule proposals → Update skill files
- **Storage:** `.claude/session-notes.md` (working), `proposed/reflect-*/` (diffs), `skills/` (canonical rules)
- **Lifespan:** Permanent after manual review/merge
- **Use Case:** "Never forget to include correlationId" → Write to session-notes.md → Auto-generates STYLE.md proposal → Review & merge
- **Example:** Correction="Always include correlationId in logs" → STYLE.md changelog entry + Knowledge Graph governance_rule entity

### Integration Workflow (Triple-Write Pattern)

```
┌─────────────────────────────────────────────────────────────────┐
│ ACTIVE SESSION                                                  │
│ ├─ Claude uses Serena for all code operations                  │
│ │  └─ Serena stores: file changes, symbols, patterns           │
│ └─ Claude writes corrections to .claude/session-notes.md       │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ SESSION STOP (.claude/hooks/stop.sh auto-triggers)             │
│ ├─ scripts/reflect.py runs automatically                       │
│ │  ├─ Reads .claude/session-notes.md (manual entries)          │
│ │  ├─ Queries Serena memory (automatic session context)        │
│ │  └─ Extracts: Corrections, Approvals, Patterns, Nevers       │
│ └─ Generates proposals in proposed/reflect-TIMESTAMP/          │
│    ├─ STYLE.md.diff (corrections + patterns)                   │
│    ├─ DEBUGGING.md.diff (approvals + patterns)                 │
│    ├─ SAFETY.md.diff (nevers - high-risk, manual only)         │
│    ├─ manifest.json                                            │
│    └─ reflection-receipt.json (Aspire Law #2 compliant)        │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ TRIPLE-WRITE STORAGE                                            │
│ ├─ proposed/ (diffs for manual review)                         │
│ ├─ Knowledge Graph MCP (governance_rule entities)              │
│ │  └─ Stores rules with type, confidence, timestamp            │
│ └─ reflection-receipt.json (immutable audit trail)             │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ MANUAL REVIEW & MERGE                                           │
│ ├─ User reviews proposed/*.diff files                          │
│ ├─ Low/medium risk: Auto-apply or quick approve                │
│ ├─ High-risk (SAFETY.md): Manual approval required             │
│ └─ Commits to Git with reflection-receipt.json reference       │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│ NEXT SESSION STARTS                                             │
│ ├─ Claude queries Knowledge Graph for governance_rule entities │
│ ├─ Claude reads skills/ changelogs (updated rules)             │
│ └─ Claude avoids repeating previous mistakes (CLOSED LOOP!)    │
└─────────────────────────────────────────────────────────────────┘
```

### Installed Files (2026-01-10)

```
✅ .claude/session-notes.md         # Working memory for session corrections
✅ .claude/hooks/stop.sh             # Auto-trigger reflection on session end
✅ scripts/reflect.py                # Generate proposals + reflection receipts
✅ skills/global/STYLE.md            # Coding conventions + patterns
✅ skills/global/SAFETY.md           # Hard safety guardrails ("Never: ...")
✅ skills/aspire/DEBUGGING.md        # Workflow rules + approvals
✅ skills/aspire/RECEIPTS.md         # Receipt-specific governance
✅ docs/reflection/                  # Reflection pack documentation
```

### Success Metrics (Triple-Memory)

- **Week 10:** 100% session reflection proposals generated automatically
- **Week 14:** 80% reduction in repeated mistakes (measured via session logs)
- **Week 24:** Zero cross-session amnesia (all corrections preserved)
- **Ongoing:** Knowledge Graph grows with 50+ governance_rule entities

### Risk Mitigation

- **Fragmentation:** Clear hierarchy = Skills/ (canonical rules) > Knowledge Graph (solution cache) > Serena (session context)
- **Proposal Noise:** Only generate proposals if session-notes.md has entries OR Serena detected meaningful patterns
- **High-Risk Protection:** SAFETY.md and RECEIPTS.md NEVER auto-applied (manual approval required)

---

## 📅 IMPLEMENTATION TIMELINE

### **NOW (Week 3-4) - Plugin Structure Creation** ✅ CURRENT

**What:** Create plugin directory structure and command/hook stubs
**Why:** Plugin evolves alongside development, ready for incremental implementation
**Effort:** 2-3 hours

**Deliverables:**
```
aspire-infra/
├── commands/           # 7 command stubs created
├── hooks/hooks.json    # Hook definitions (minimal implementation)
├── agents/             # 3 agent definitions (stubs)
├── scripts/            # Script placeholders
├── validators/         # Aspire-specific validator stubs
├── templates/          # Empty templates directory
├── integrations/       # MCP config placeholders
├── session-logs/       # Log template
└── docs/README.md      # Plugin documentation
```

---

### **Week 10 (After Phase 1) - v0.1 Plugin Implementation** 🎯 PRIORITY

**What:** Implement core automation workflows
**Why:** Phase 1 patterns established (receipts, capability tokens, RLS), ready to codify
**Effort:** 1 week (5-7 days)

**Deliverables:**

#### Commands (3 implemented):
1. **`/aspire-infra:shipcheck`**
   - Runs: format → lint → typecheck → unit tests
   - Fast feedback loop (<60s execution)
   - Fails loudly with actionable next steps

2. **`/aspire-infra:receipt-verify`** (ASPIRE-SPECIFIC)
   - Validates receipt hash-chain integrity
   - Checks 14+ mandatory fields present
   - Verifies Presidio DLP redaction

3. **`/aspire-infra:tab-check`** (ASPIRE-SPECIFIC)
   - Validates 4-tab navigation invariant (Inbox/Quarter/Workbench/Office)
   - Prevents mobile UI changes that violate constraint
   - Critical for Phase 3 preparation

#### Hooks (2 implemented):
1. **Block Foot-Guns Hook**
   - Blocks writes to: `.env`, `secrets/`, `prod-configs/`, `.terraform/`
   - Requires explicit approval for destructive DB operations
   - Logs all approvals to session-log

2. **End-of-Session Summary Hook**
   - Generates `session-logs/session-YYYYMMDD-HHMM.json`
   - Contents: files changed, checks passed/failed, receipts generated, todos remaining
   - References Aspire receipt_id for state-changing actions

#### Scripts (1 implemented):
- `scripts/shipcheck.sh` - Orchestrates format/lint/typecheck/test pipeline
- `validators/receipt_verify.py` - Hash-chain validation logic
- `validators/tab_check.py` - Navigation invariant validator

**Success Criteria:**
- ✅ `/shipcheck` runs in <60s and catches formatting/type errors
- ✅ Foot-gun hook blocks accidental `.env` commits
- ✅ End-of-session summary generated automatically

---

### **Week 14 (Mid Phase 2) - v0.2 Plugin Expansion** 🔧 ENHANCEMENT

**What:** Add skill pack scaffolding and RLS validation
**Why:** 3-4 skill packs implemented, patterns established for templates
**Effort:** 3-4 days

**Deliverables:**

#### Commands (2 new):
4. **`/aspire-infra:service-new <name>`**
   - Scaffolds Aspire-compliant skill pack
   - Generates: `__init__.py`, `handlers.py`, `[service]_integration.py`, `manifest.json`
   - Pre-populates: receipt generation, capability token enforcement, RLS context setting

5. **`/aspire-infra:rls-check`** (ASPIRE-SPECIFIC)
   - Quick tenant isolation smoke test
   - Validates: Suite A cannot read Suite B data, Office 1 cannot read Office 2 data
   - Runs in <30s (subset of full RLS isolation tests)

#### Templates (1 implemented):
- `templates/skill_pack_skeleton/` - Complete Aspire skill pack template
  - Includes: LangGraph handler structure, integration contract pattern, manifest.json with risk tier
  - Example: Invoice Desk structure as reference

#### Validators (2 new):
- `validators/rls_check.py` - Fast tenant isolation validator
- `validators/governance_loop.py` - Prime Directive flow validator

**Success Criteria:**
- ✅ `/service-new email-assistant` generates working skill pack skeleton
- ✅ `/rls-check` catches cross-tenant data leakage in <30s

---

### **Week 24 (Phase 4) - v1.0 Plugin Completion** 🚀 PRODUCTION-READY

**What:** Add PR workflows, operational agents, and MCP reality checks
**Why:** Production hardening phase - need comprehensive automation for release readiness
**Effort:** 1 week (5-7 days)

**Deliverables:**

#### Commands (2 new):
6. **`/aspire-infra:pr-ready`**
   - Generates PR description from diff
   - Includes: summary, risk level (Green/Yellow/Red), rollout plan, test evidence, "how to revert", checklist
   - References: Aspire receipt_id for state changes, security-reviewer findings

7. **`/aspire-infra:threatcheck`**
   - Quick security review (5-pillar framework)
   - Checks: auth boundaries, secrets handling, least privilege, data exposure, input validation
   - Lighter weight than full security-reviewer agent (runs in <2 min vs. 30+ min)

#### Agents (3 new):
1. **migration-planner.md** (Sonnet, Purple)
   - Database migration planning
   - Schema changes, backward compatibility, staged rollout, data backfills
   - Generates migration receipts

2. **perf-cop.md** (Sonnet, Magenta)
   - Performance validation
   - Latency budgets, query plans, cache strategy, load test prompts
   - SLO compliance checks

3. **security-gatekeeper.md** (Sonnet, Orange)
   - PR-level security checks
   - Threat models, least privilege, prompt-injection paranoia
   - Complements security-reviewer agent (PR vs. Phase-gate)

#### MCP Integration (3 servers):
- `integrations/github_mcp.json` - GitHub MCP (read-only: PRs, issues, commits, CI status)
- `integrations/sentry_mcp.json` - Sentry MCP (read-only: error rates, performance metrics)
- `integrations/postgres_mcp.json` - Postgres MCP (read-only: schema inspection, query explain)

#### Hooks (1 new):
3. **Auto-Run Checks on File Write** (configurable)
   - Runs format + lint + typecheck after file edits
   - Can disable during rapid iteration, enable during Phase 4+
   - Performance: <10s for incremental checks

**Success Criteria:**
- ✅ `/pr-ready` generates complete PR description with rollout plan
- ✅ `/threatcheck` catches auth bypass attempts in <2 min
- ✅ migration-planner agent produces backward-compatible migration plan
- ✅ MCP integration allows querying GitHub PR status from Claude

---

## 🏗️ PLUGIN STRUCTURE (Final)

```
aspire-infra/
├── commands/                           # 7 slash commands
│   ├── shipcheck.md                    ✅ v0.1 (Week 10)
│   ├── receipt-verify.md               ✅ v0.1 (Week 10) - ASPIRE-SPECIFIC
│   ├── tab-check.md                    ✅ v0.1 (Week 10) - ASPIRE-SPECIFIC
│   ├── service-new.md                  ✅ v0.2 (Week 14)
│   ├── rls-check.md                    ✅ v0.2 (Week 14) - ASPIRE-SPECIFIC
│   ├── pr-ready.md                     ✅ v1.0 (Week 24)
│   └── threatcheck.md                  ✅ v1.0 (Week 24)
│
├── hooks/
│   └── hooks.json                      # 3 hook types
│       ├── Block foot-guns             ✅ v0.1 (Week 10)
│       ├── End-of-session summary      ✅ v0.1 (Week 10)
│       └── Auto-run checks on write    ✅ v1.0 (Week 24)
│
├── agents/                             # 3 operational agents
│   ├── migration-planner.md            ✅ v1.0 (Week 24)
│   ├── perf-cop.md                     ✅ v1.0 (Week 24)
│   └── security-gatekeeper.md          ✅ v1.0 (Week 24)
│
├── scripts/                            # Automation executables
│   ├── shipcheck.sh                    ✅ v0.1 (Week 10)
│   ├── format.sh
│   ├── lint.sh
│   ├── typecheck.sh
│   └── test.sh
│
├── validators/                         # Aspire-specific validation (NEW CATEGORY)
│   ├── receipt_verify.py               ✅ v0.1 (Week 10)
│   ├── tab_check.py                    ✅ v0.1 (Week 10)
│   ├── rls_check.py                    ✅ v0.2 (Week 14)
│   └── governance_loop.py              ✅ v0.2 (Week 14)
│
├── templates/                          # Scaffolding templates
│   ├── pr_template.md                  ✅ v1.0 (Week 24)
│   └── skill_pack_skeleton/            ✅ v0.2 (Week 14)
│       ├── __init__.py
│       ├── handlers.py
│       ├── [service]_integration.py
│       └── manifest.json
│
├── integrations/                       # MCP server configs
│   ├── github_mcp.json                 ✅ v1.0 (Week 24)
│   ├── sentry_mcp.json                 ✅ v1.0 (Week 24)
│   └── postgres_mcp.json               ✅ v1.0 (Week 24)
│
├── session-logs/                       # Session audit trail templates
│   └── template.json                   ✅ v0.1 (Week 10)
│
└── docs/
    ├── README.md                       # Plugin overview
    ├── usage.md                        # Command reference
    └── aspire-specifics.md             # Aspire governance integration guide
```

---

## 🎯 ASPIRE-SPECIFIC MODIFICATIONS (vs. Generic Plugin)

The generic Claude Code plugin spec is **70% correct** for Aspire. We added **5 critical Aspire-specific elements**:

### 1. **4-Tab Navigation Validation** (`/tab-check`)
- **Why:** Generic plugins don't know about Inbox/Quarter/Workbench/Office invariant
- **Impact:** Prevents Phase 3 mobile UI changes from violating core UX constraint
- **Implementation:** `validators/tab_check.py` parses React Native navigation structure

### 2. **Receipt Hash-Chain Verification** (`/receipt-verify`)
- **Why:** Generic "receipts" are just audit logs, not Aspire-compliant governance receipts
- **Impact:** Ensures continuous Law #2 compliance (Receipt for All Actions)
- **Implementation:** `validators/receipt_verify.py` validates 14+ fields, SHA-256 hash-chaining

### 3. **Capability Token Enforcement Hook**
- **Why:** Generic foot-gun blocking doesn't enforce Aspire Law #5 (Capability Tokens)
- **Impact:** Prevents tool execution without valid <60s scoped tokens
- **Implementation:** Hook intercepts tool calls, validates capability_token_id present

### 4. **RLS Smoke Tests** (`/rls-check`)
- **Why:** Generic security checks don't validate tenant isolation (Aspire Law #6)
- **Impact:** Fast feedback on cross-tenant data leakage (<30s vs. full test suite)
- **Implementation:** `validators/rls_check.py` runs subset of RLS isolation tests

### 5. **Governance Loop Validation**
- **Why:** Generic plugins don't validate Intent → Context → Plan → Policy → Approval → Execute → Receipt → Summary flow
- **Impact:** Ensures Prime Directive enforcement at PR level
- **Implementation:** `validators/governance_loop.py` checks code for missing approval gates

---

## 🔒 SECURITY POSTURE

### MCP Server Security (Read-Only by Default)

**Principle:** Consult reality, don't mutate reality (without receipts)

#### GitHub MCP (Read-Only):
- **Allowed:** `gh pr list`, `gh issue view`, `gh run view` (CI status)
- **Blocked:** `gh pr merge`, `gh issue close`, `gh release create`
- **Token:** Personal access token with `repo:read` scope only

#### Sentry MCP (Read-Only):
- **Allowed:** Query error rates, performance metrics, issue details
- **Blocked:** Issue resolution, release marking, alert rule changes
- **Token:** Auth token with read-only permissions

#### Postgres MCP (Read-Only):
- **Allowed:** `EXPLAIN` queries, schema inspection, `SELECT` (non-sensitive tables)
- **Blocked:** `INSERT`, `UPDATE`, `DELETE`, `CREATE`, `ALTER`
- **Connection:** Read-replica with `SELECT` grants only

### State Mutation Gating

**ANY** MCP call that mutates state MUST:
1. Require explicit `/prod-approve` command first
2. Generate Aspire receipt (14+ fields, hash-chained)
3. Log to session-log with correlation_id
4. Include rollback procedure in PR description

**Example:**
```python
# BLOCKED by hook:
await postgres_mcp.execute("UPDATE users SET role = 'admin'")

# ALLOWED after approval + receipt:
await request_approval(risk_tier="red", action="UPDATE users")
receipt_id = generate_receipt(...)
await postgres_mcp.execute("UPDATE users SET role = 'admin'")
log_session_action(receipt_id=receipt_id)
```

---

## 📊 SUCCESS METRICS

### v0.1 (Week 10):
- ✅ `/shipcheck` catches 80%+ of formatting/type errors before commit
- ✅ Foot-gun hook prevents 100% of accidental secret commits
- ✅ Session-log generated for 100% of sessions

### v0.2 (Week 14):
- ✅ `/service-new` scaffolds working skill pack in <5 min
- ✅ `/rls-check` catches cross-tenant leakage in <30s
- ✅ Skill pack template used for 5+ Phase 2 skill packs

### v1.0 (Week 24):
- ✅ `/pr-ready` used for 100% of PRs
- ✅ `/threatcheck` catches auth bypass attempts (evil tests pass)
- ✅ MCP integration reduces "Claude, check GitHub for CI status" manual lookups by 90%
- ✅ migration-planner agent produces zero-downtime migration plans

---

## 🚨 RISKS & MITIGATIONS

### Risk 1: Hook Reliability Issues
- **Problem:** Some Claude Code versions report PreToolUse/PostToolUse hooks don't fire reliably
- **Mitigation:** Fallback to manual `/shipcheck` + Stop hook summary (always fires)
- **Confidence:** MEDIUM (60%)

### Risk 2: Plugin Maintenance Burden
- **Problem:** Two systems to maintain (governance model + plugin)
- **Mitigation:** Plugin REQUIRES Aspire governance files (tight coupling), version together
- **Confidence:** LOW (20%)

### Risk 3: MCP Prompt Injection
- **Problem:** Untrusted content from GitHub issues, Sentry errors could contain prompt injection
- **Mitigation:** Sanitize all MCP responses, read-only tokens, manual approval for state mutations
- **Confidence:** MEDIUM (50%)

---

## 🎓 USAGE EXAMPLES

### Example 1: Daily Development Workflow (Phase 2)

```bash
# Developer working on Stripe integration
$ /aspire-infra:service-new stripe-billing
✅ Created: backend/skill-packs/stripe-billing/
✅ Generated: handlers.py, stripe_integration.py, manifest.json
✅ Pre-populated: Receipt generation, capability token enforcement

# ... make changes ...

$ /aspire-infra:shipcheck
Running: format → lint → typecheck → unit tests
✅ format: PASSED (0 changes)
✅ lint: PASSED (0 issues)
✅ typecheck: PASSED (0 errors)
✅ tests: PASSED (47/47 tests)
🎉 All checks passed! Ready to commit.

# Try to commit .env file (accident)
$ git add .env
🚨 BLOCKED by aspire-infra foot-gun hook
❌ Cannot commit .env file (contains secrets)
ℹ️ Add to .gitignore or use vault storage

# End session
$ exit
✅ Session summary: session-logs/session-20260104-1423.json
   - Files changed: 5 (stripe_integration.py, handlers.py, ...)
   - Checks passed: shipcheck ✅, rls-check ✅
   - Receipts generated: 3 (receipt_abc123, receipt_def456, receipt_ghi789)
   - Next steps: Run /pr-ready before creating PR
```

### Example 2: PR Workflow (Phase 4)

```bash
# Developer ready to create PR
$ /aspire-infra:pr-ready
Analyzing diff... (3 files changed, 247 additions, 12 deletions)

Generated PR Description:
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
## Summary
Adds Stripe invoice creation to Invoice Desk skill pack

## Risk Level: YELLOW (requires user confirmation)

## Changes
- Added `stripe_integration.py` with invoice.create capability
- Updated `handlers.py` with approval flow (Yellow tier)
- Generated receipts for all invoice creation actions

## Test Evidence
- ✅ shipcheck passed (format/lint/typecheck/unit)
- ✅ rls-check passed (zero cross-tenant leakage)
- ✅ threatcheck passed (5-pillar security)
- ✅ receipt-verify passed (hash-chain integrity)

## Rollout Plan
1. Deploy to staging (Week 15)
2. Manual testing with test Stripe account
3. Production deploy with feature flag (Week 16)
4. Monitor error rates for 48h before full rollout

## How to Revert
```bash
git revert abc123def456
kubectl rollout undo deployment/invoice-desk
```

## Checklist
- [ ] Staging deployment successful
- [ ] Manual testing complete (test@example.com)
- [ ] Error rate <0.1% for 48h
- [ ] Feature flag enabled for production

## Related
- Receipts: receipt_abc123, receipt_def456, receipt_ghi789
- Sentry: https://sentry.io/aspire/issues/12345
- CI: All checks passed ✅
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

✅ PR description copied to clipboard
ℹ️ Paste into GitHub PR description
```

### Example 3: Database Migration (Phase 4)

```bash
# Developer needs to add column to receipts table
$ /aspire-infra:migration-planner
Launching migration-planner agent...

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Migration Plan: Add receipt_version column
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

## Proposed Change
Add `receipt_version INT DEFAULT 1` to receipts table

## Backward Compatibility: ✅ SAFE
- New column has DEFAULT value (no null constraint violations)
- Existing receipts auto-populate with version=1
- No code changes required before migration

## Staged Rollout Plan
1. **Phase 1:** Add column with DEFAULT 1 (zero downtime)
2. **Phase 2:** Backfill existing rows (already done via DEFAULT)
3. **Phase 3:** Deploy code that writes receipt_version
4. **Phase 4:** Remove DEFAULT constraint (optional)

## Migration SQL
```sql
-- Migration: 004_add_receipt_version.sql
ALTER TABLE receipts
ADD COLUMN receipt_version INT DEFAULT 1 NOT NULL;

CREATE INDEX idx_receipts_version ON receipts(receipt_version);
```

## Rollback Procedure
```sql
-- Rollback: 004_add_receipt_version_rollback.sql
ALTER TABLE receipts DROP COLUMN receipt_version;
```

## Testing Required
- [ ] Run migration on staging
- [ ] Verify existing receipts have version=1
- [ ] Verify new receipts generate with version=1
- [ ] Verify rollback works without data loss

## Risk Assessment: LOW
- No data loss risk (DEFAULT handles all cases)
- No downtime required
- Rollback is simple DROP COLUMN

✅ Migration plan saved to: backend/services/database/migrations/004_add_receipt_version.sql
✅ Rollback saved to: backend/services/database/migrations/004_rollback.sql
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

---

## 📚 RELATED DOCUMENTATION

- **Agent Orchestration Plan:** `.claude/plans/temporal-riding-mountain.md`
- **Aspire Laws:** `CLAUDE.md`
- **Existing Agents:** `.claude/agents/*.md` (8 agents)
- **Skills:** `.claude/skills/{receipts,governance,tenant-isolation}/SKILL.md`
- **Roadmap:** `plan/Aspire-Production-Roadmap.md`

---

## ✅ DECISION LOG

**2026-01-04 - Plugin Architecture Approved**
- **Decision:** Build aspire-infra as separate Claude Code plugin (not extend .claude/ folder)
- **Rationale:** Clean separation of concerns (governance vs. automation), independent versioning, team distribution
- **Confidence:** 92% (Sequential Thinking analysis)

**2026-01-04 - Implementation Timeline Approved**
- **Decision:** Create structure NOW (Week 3-4), implement v0.1 Week 10, v0.2 Week 14, v1.0 Week 24
- **Rationale:** Plugin evolves alongside development, patterns established before codification
- **Confidence:** 90%

**2026-01-04 - Aspire-Specific Modifications Approved**
- **Decision:** Add 5 Aspire-specific elements (tab-check, receipt-verify, rls-check, capability-token hook, governance-loop validator)
- **Rationale:** Generic plugin spec is 70% correct for Aspire, needs domain-specific enforcement
- **Confidence:** 95%

**2026-01-10 - Triple-Memory Strategy Installed** ✅
- **Decision:** Install Session Reflection Pack + integrate with Knowledge Graph MCP + Serena Memory
- **Rationale:** Eliminates cross-session amnesia, creates durable governance rules, completes learning loop
- **Implementation:** Installed `.claude/session-notes.md`, `.claude/hooks/stop.sh`, `scripts/reflect.py`, `skills/global/`, `skills/aspire/`
- **Integration:** Reflection pack queries Serena memory + writes to Knowledge Graph + generates receipts
- **Confidence:** 95% (Based on 20-thought Sequential Thinking analysis)
- **Files Installed:**
  - ✅ `.claude/session-notes.md` (working memory)
  - ✅ `.claude/hooks/stop.sh` (auto-trigger on session end)
  - ✅ `scripts/reflect.py` (proposal generator + receipt emitter)
  - ✅ `skills/global/STYLE.md` (coding conventions)
  - ✅ `skills/global/SAFETY.md` (hard safety guardrails)
  - ✅ `skills/aspire/DEBUGGING.md` (workflow rules)
  - ✅ `skills/aspire/RECEIPTS.md` (receipt governance)
  - ✅ `docs/reflection/` (documentation)

---

**Status:** 📋 PLAN APPROVED | ✅ TRIPLE-MEMORY INSTALLED (2026-01-10)
