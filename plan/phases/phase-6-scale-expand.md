---
phase: "6"
name: "Scale & Expand (Cloud Migration + Advanced Features)"
status: "not_started"
blocking_phase: "5"
blocks_phases: []
duration_estimate: "10-12 weeks (Meeting of Minds + multi-tenant scaling)"
gates_satisfied: []
priority: "medium"
hardware_required: "Skytech Shadow (development) + Cloud (production)"
cost: "$14/mo target (cloud migration)"
---

# PHASE 6: Scale & Expand

## Objective
Migrate to managed cloud, implement Admin Portal with Meeting of Minds (multi-LLM council for ops/dev team), and achieve $14/mo operational cost target (90% savings).

**Note:** All 10 skill packs from Ecosystem v12.7 are in Phase 2. Phase 6 focuses on scaling infrastructure and Admin Portal features.

---

## Trust Spine Production Scaling

**Scaling Strategies:**
- Outbox executor horizontal scaling (add workers as load increases)
- Edge Functions auto-scaling (Supabase handles load distribution)
- Go verification service containerized (Docker deployment for scaling)
- Receipt ledger partitioning strategy (optimize query performance at scale)
- A2A Router load balancing (distribute agent-to-agent tasks)

---

## Trust Spine Ecosystem Documentation (IMPORTANT)

**📚 Scaling and advanced features documentation exists in the Trust Spine package:**

### Scaling Resources
- **Navigation Guide:** See `Trust-Spine-Package-Navigation.md` (in this plan directory) for scaling workflow
- **Outbox Horizontal Scaling:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/08_CLAUDE_JOB_KIT/PHASE_6_POST_LAUNCH_OPERATIONS/RUNBOOKS/` for horizontal scaling procedures (PHASE_8 does not exist in ecosystem; scaling docs live under PHASE_6_POST_LAUNCH_OPERATIONS)
- **Edge Functions Auto-Scaling:** Supabase handles auto-scaling automatically (no configuration needed)
- **Go Service Containerization:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/01_ORIGINAL_INPUTS/` for Go service source code (no `go/` subdirectory exists under A2A_INBOX_V6)
- **Receipt Ledger Partitioning:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/06_ADDONS/ops/` for database optimization strategies

### Advanced Features Resources
- **Meeting of Minds:** See `platform/CLAUDE_HANDOFF/02_MEETING_OF_MINDS.md` for multi-LLM council workflow
- **Council Deliberation:** See `platform/brain/workflows/council_deliberation.workflow.ts` for decision framework
- **LLM Router:** See `platform/brain/llm/call_llm.ts` for multi-model routing

### Testing Resources
- **Load Tests:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/14_STRESS_TESTS/k6/` for scaling load tests (target: 10x Phase 5 load)

**⭐ Start Here:** Read `Trust-Spine-Package-Navigation.md` for scaling procedures.

---

## Cloud Migration (Weeks 39-42)

- [ ] **PHASE6-TASK-CM-001** Render Deployment
  - Migrate backend services to Render
  - Configure auto-scaling (1-10 instances)
  - Setup health checks and monitoring
  - Test: Zero-downtime migration

- [ ] **PHASE6-TASK-CM-002** Database Optimization
  - Enable connection pooling (PgBouncer)
  - Configure read replicas for analytics
  - Receipt ledger partitioning by date
  - Test: Query performance <50ms p95

- [ ] **PHASE6-TASK-CM-003** Cost Optimization
  - Analyze usage patterns
  - Right-size compute resources
  - Configure reserved instances
  - Target: $14/mo operational cost

---

## Advanced Features (Weeks 43-46)

### Admin Portal: Meeting of Minds (Multi-LLM Council)

**Purpose:** Internal ops/dev team feature for high-severity incident triage and complex decisions

**Source:** `plan/Aspire_Robots_Ops_v2_Council_Learning/docs/MEETING_OF_MINDS_RUNBOOK.md`

**Council Members:**
| Provider | Role | Model | Responsibility |
|----------|------|-------|----------------|
| OpenAI | architect | GPT-5.2 | Architecture/system design critique |
| Google | research | Gemini-2.5 | Research cross-check, alternative hypotheses |
| Anthropic | implementer | Claude-3.5 | Implementation realism, code-level planning |

**Council Workflow:**
```
Robots Failure → incident.opened receipt
       ↓
Ava spawns council thread
       ↓
Each advisor proposes actions (citations required)
       ↓
Ava adjudicates with rubric → council.decision receipt
       ↓
Claude builds patch → Robot retests
       ↓
Canary deploy (if pass) → deploy.canary.deployed receipt
```

**Hard Rules:**
- ✅ Advisory only: no provider calls, no deployments, no secrets access
- ✅ Must cite evidence pack items (receipt/log/robot_run/internal_doc)
- ✅ Ava adjudicates with rubric and emits `council.decision` receipt
- ✅ Max 2 deliberation rounds per incident
- ✅ All council decisions generate immutable receipts

**Triggers:**
- severity >= S1
- unclear root cause after first triage
- repeated incident signature (3+ similar failures)
- red-tier workflows (money movement, permissions, identity)

- [ ] **PHASE6-TASK-MOM-001** Deploy Council Router Configuration
  - Deploy `brain/router/council_router.yaml`:
    ```yaml
    members:
      - provider: openai, role: architect, model: gpt-5.2
      - provider: google, role: research, model: gemini-2.5
      - provider: anthropic, role: implementer, model: claude-3.5
    rules:
      require_citations: true
      max_rounds: 2
      deny_tool_execution: true
    ```
  - Deploy `brain/router/internal_learning_router.yaml`:
    - Summarizer model selection
    - Evaluator model selection
    - Learning Loop routing rules
  - Test: Council router selects correct models per role

- [ ] **PHASE6-TASK-MOM-002** Council Deliberation Workflow
  - Multiple LLMs analyze same problem (severity >= S1)
  - Synthesis agent combines perspectives
  - Confidence scoring for consensus
  - Trigger: Unclear root cause, repeated incident signature, red-tier workflows
  - Test: 3-model deliberation on complex decision

- [ ] **PHASE6-TASK-MOM-003** Admin Portal Integration
  - Council UI in Admin Portal (internal users only)
  - Council recommendations → Authority Queue (admin review)
  - Receipt logging for all deliberations (audit trail)
  - Test: Council decision generates `council.decision` receipt

### LLM Observability

- [ ] **PHASE6-TASK-LLM-001** Multi-Model Metrics
  - Track latency, cost, quality per model
  - A/B testing framework for model comparison
  - Dashboard for model performance
  - Test: Metrics collection working

---

## White-Label Email Upgrade (Weeks 47-48) — NEW

**Decision Point:** Upgrade from standard PolarisMail to private-label email.

### Context
- Phase 0C deploys standard EmailArray integration (PolarisMail)
- Private label = $600 one-time fee to PolarisMail
- Enables custom webmail/IMAP/SMTP hostnames under Aspire branding
- Required for `6-SC-003` (white-label email operational)

### White-Label Tasks

- [ ] **PHASE6-TASK-WL-001** Private Label Decision + Payment
  - Evaluate usage metrics from Phase 5 beta
  - If mail adoption > 30% of pilot users → proceed with private label
  - Pay $600 one-time fee to PolarisMail
  - **Verification:** Private label account activated

- [ ] **PHASE6-TASK-WL-002** Custom Hostname Configuration
  - Configure webmail hostname: `mail.aspireos.app` (or per-suite subdomain)
  - Configure IMAP/SMTP hostnames: `imap.aspireos.app`, `smtp.aspireos.app`
  - Update DNS records for new hostnames
  - **Verification:** Custom hostnames resolving correctly

- [ ] **PHASE6-TASK-WL-003** Customer Migration Plan
  - Migrate existing pilot users from standard to private-label endpoints
  - Update Desktop + Mobile app mail configurations
  - Email clients (Outlook, Apple Mail) updated with new server hostnames
  - Zero-downtime migration (both old and new hostnames work during transition)
  - **Verification:** All pilot users migrated, zero service interruption

- [ ] **PHASE6-TASK-WL-004** Branded Webmail UI
  - Apply Aspire branding to webmail interface
  - Custom login page, logo, color scheme
  - **Verification:** Webmail shows Aspire branding

### White-Label Success Criteria
- [ ] `6-SC-WL-001` Private label activated with PolarisMail
- [ ] `6-SC-WL-002` Custom hostnames operational (mail/imap/smtp.aspireos.app)
- [ ] `6-SC-WL-003` Pilot users migrated with zero downtime
- [ ] `6-SC-WL-004` Webmail shows Aspire branding

---

## Customer Beta (Weeks 49-52)

- [ ] **PHASE6-TASK-BETA-001** Pilot User Onboarding
  - Onboard 10 pilot users
  - Configure per-user suite/office
  - Enable all 10 skill packs
  - Test: User can execute full workflow

- [ ] **PHASE6-TASK-BETA-002** Feedback Collection
  - In-app feedback mechanism
  - Weekly user interviews
  - Usage analytics dashboard
  - Test: Feedback loop working

- [ ] **PHASE6-TASK-BETA-003** Iteration Cycle
  - Prioritize beta feedback
  - Weekly releases with fixes
  - Learning Loop integration (incidents → improvements)
  - Test: Feedback → fix cycle <1 week

---

## Success Criteria

- [ ] `6-SC-001` Cloud migration complete (<1% error rate in 48hr soak)
- [ ] `6-SC-002` $14/mo cloud cost achieved (90% savings)
- [ ] `6-SC-003` White-label email operational (private label activated, custom hostnames, branded webmail)
- [ ] `6-SC-004` Meeting of Minds deliberation working
- [ ] `6-SC-005` Customer beta testing begins (10 pilot users)
- [ ] `6-SC-006` 10x load capacity vs Phase 5 baseline

## Memory System Success Criteria
- [ ] `6-MEM-001` Knowledge Graph scales to 300+ entities (multi-operator + ecosystem)
- [ ] `6-MEM-002` Skills/ changelogs become external developer documentation
- [ ] `6-MEM-003` Session reflection supports multi-developer teams (shared governance rules)

## Duration: 8-12 weeks

## Related Artifacts
- Cloud infrastructure: Render + Supabase + Upstash (managed services)
- White-label email: PolarisM (server-side credential vaulting)
- Multi-LLM routing: Claude, GPT-4, Gemini integration
- Customer beta: 10 pilot users onboarded
