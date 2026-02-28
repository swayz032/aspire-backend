# Agent Output Directory Structure
**Purpose:** Organized storage for all custom agent outputs throughout Aspire development.

**Created:** 2026-01-04

---

## 📁 Directory Structure

```
docs/agents/
├── README.md                           # This file - directory guide
├── agent-log.md                        # Central tracking log for all agent invocations
├── architecture/                       # aspire-system-architect outputs
│   ├── phase-1-brain-design.md        # Phase 1 LangGraph orchestrator architecture
│   ├── phase-3-mobile-ui-design.md    # Phase 3 mobile app architecture
│   └── phase-6-multi-operator.md      # Phase 6 multi-operator architecture
├── security/                           # security-reviewer outputs
│   ├── phase-1-initial-audit.md       # Phase 1 end security audit
│   ├── phase-2-integration-audit.md   # Phase 2 integration security review
│   ├── phase-3-mobile-audit.md        # Phase 3 mobile security audit
│   ├── phase-4-comprehensive-audit.md # Phase 4 5-pillar security review
│   └── phase-6-final-audit.md         # Phase 6 final pre-launch audit
├── testing/                            # aspire-test-engineer outputs
│   ├── phase-1-orchestrator-tests.md  # Phase 1 comprehensive test suite
│   ├── phase-2-invoice-desk-tests.md  # Invoice Desk RLS + evil tests
│   ├── phase-2-support-tests.md       # Support Switchboard tests
│   ├── phase-2-scheduling-tests.md    # Scheduling Agent tests
│   ├── phase-2-esignature-tests.md    # E-Signature Desk tests
│   ├── phase-2-discovery-tests.md     # Business Discovery tests
│   ├── phase-2-documents-tests.md     # Document Creation tests
│   ├── phase-3-mobile-tests.md        # Mobile app performance + degradation tests
│   ├── phase-4-production-gates.md    # Final production gate testing results
│   └── phase-6-*.md                   # Phase 6 skill pack tests
├── audit/                              # receipt-ledger-auditor outputs
│   ├── phase-1-receipt-system.md      # Phase 1 receipt system audit
│   ├── phase-2-invoice-receipts.md    # Invoice Desk receipt verification
│   ├── phase-2-support-receipts.md    # Support Switchboard receipt verification
│   ├── phase-2-scheduling-receipts.md # Scheduling Agent receipt verification
│   ├── phase-3-mobile-receipts.md     # Mobile app receipt generation audit
│   └── phase-5-continuous-audit.md    # Phase 5 ongoing audit findings
├── integrations/                       # mcp-toolsmith + policy-gate-engineer outputs
│   ├── stripe-tool-contract.md        # Stripe API tool contract + risk tier
│   ├── quickbooks-tool-contract.md    # QuickBooks API tool contract + risk tier
│   ├── livekit-tool-contract.md       # LiveKit Cloud tool contract + risk tier
│   ├── gmail-tool-contract.md         # Gmail API tool contract + risk tier
│   ├── gcal-tool-contract.md          # Google Calendar API tool contract + risk tier
│   ├── docusign-tool-contract.md      # DocuSign API tool contract + risk tier
│   ├── places-tool-contract.md        # Yelp/Google Places tool contract + risk tier
│   ├── puppeteer-tool-contract.md     # Puppeteer tool contract + risk tier
│   ├── nemo-guardrails-contract.md    # NeMo Guardrails tool contract (if MCP)
│   ├── presidio-contract.md           # Presidio DLP tool contract (if MCP)
│   └── capability-token-architecture.md # Phase 1 capability token design (policy-gate-engineer)
├── operations/                         # release-sre outputs
│   ├── phase-4-production-readiness.md # Phase 4 comprehensive SRE review
│   ├── phase-4-slo-dashboards.md      # SLO dashboard definitions
│   ├── phase-4-incident-runbooks.md   # Incident response runbooks
│   ├── phase-4-soak-plan.md           # 24-hour production soak test plan
│   ├── phase-5-monitoring-setup.md    # Phase 5 production monitoring config
│   └── phase-6-cloud-migration.md     # Phase 6 cloud deployment strategy
└── documentation/                      # proof-artifacts-builder outputs
    ├── 10-10-bundle/                  # Phase 4 complete 10/10 Bundle
    │   ├── 1-system-constitution.md   # 1-page system constitution
    │   ├── 2-stride-threat-model.md   # STRIDE threat analysis + mitigations
    │   ├── 3-capability-spec.md       # Capability inventory + permission boundaries
    │   ├── 4-rls-isolation-tests.md   # Automated RLS test suite definitions
    │   ├── 5-replay-demo.md           # Step-by-step replay demonstrations
    │   ├── 6-slo-dashboard.md         # SLO metrics + measurement methods
    │   ├── 7-incident-runbooks.md     # Operational procedures + templates
    │   └── 8-compliance-pack.md       # Data classification + retention policies
    └── uncertainty-declarations.md    # Knowledge gaps + confidence labels
```

---

## 📝 Usage Guidelines

### 1. **Saving Agent Outputs**
When an agent completes a task:
1. Save the full output to the appropriate directory
2. Use descriptive filenames following the pattern: `phase-X-feature-name.md`
3. Update `agent-log.md` with invocation details and output location

**Example:**
```markdown
# After aspire-system-architect designs Phase 1 architecture
Save to: docs/agents/architecture/phase-1-brain-design.md
Update: docs/agents/agent-log.md with entry for this invocation
```

### 2. **File Naming Conventions**
- **Phase-specific:** `phase-1-brain-design.md`, `phase-2-stripe-contract.md`
- **Feature-specific:** `invoice-desk-tests.md`, `mobile-ui-design.md`
- **Always lowercase with hyphens:** Use `phase-1-design.md`, NOT `Phase1Design.md`
- **Include phase number:** Helps chronological organization

### 3. **Output Template**
Each agent output file should follow this structure:

```markdown
# [Agent Name] Output: [Task Name]
**Agent:** [agent-name]
**Phase:** Phase X, Week Y
**Date:** YYYY-MM-DD
**Invocation ID:** [Link to agent-log.md entry]

---

## Context
[Brief description of what the agent was asked to do and why]

## Agent Output
[Full agent output here - preserve original formatting]

## Implementation Notes
[Any clarifications, modifications, or follow-up actions needed]

## Cross-References
- Related agent outputs: [Links to other relevant outputs]
- Implementation files: [Links to actual code files that implement this design]
- Tests: [Links to test files validating this work]
```

### 4. **Cross-Referencing**
Link related agent outputs together:
- Architecture designs → Implementation contracts → Test suites → Security audits
- Create a "paper trail" showing how governance model was enforced

**Example:**
```markdown
# In architecture/phase-1-brain-design.md
## Related Outputs
- Implementation: integrations/capability-token-architecture.md (policy-gate-engineer)
- Testing: testing/phase-1-orchestrator-tests.md (aspire-test-engineer)
- Audit: audit/phase-1-receipt-system.md (receipt-ledger-auditor)
- Security: security/phase-1-initial-audit.md (security-reviewer)
```

---

## 🎯 Quick Reference: Agent → Directory Mapping

| Agent | Primary Directory | Secondary Directories |
|-------|-------------------|----------------------|
| **aspire-system-architect** | `architecture/` | - |
| **receipt-ledger-auditor** | `audit/` | - |
| **policy-gate-engineer** | `integrations/` | `operations/` (for policy changes) |
| **mcp-toolsmith** | `integrations/` | - |
| **aspire-test-engineer** | `testing/` | - |
| **security-reviewer** | `security/` | - |
| **release-sre** | `operations/` | - |
| **proof-artifacts-builder** | `documentation/` | - |

---

## 🔄 Workflow Example: Phase 2 Invoice Desk Integration

**Complete agent workflow with outputs:**

```
1. mcp-toolsmith designs Stripe tool contract
   → Save to: integrations/stripe-tool-contract.md

2. [Implementation happens]

3. policy-gate-engineer defines risk tier
   → Append to: integrations/stripe-tool-contract.md (same file)

4. receipt-ledger-auditor verifies receipts
   → Save to: audit/phase-2-invoice-receipts.md

5. aspire-test-engineer creates test suite
   → Save to: testing/phase-2-invoice-desk-tests.md

All 4 outputs cross-referenced to create complete paper trail
```

---

## 📊 Benefits of This Structure

1. **Chronological Organization:** Easy to find outputs by phase
2. **Specialization Clarity:** Each directory maps to specific agent expertise
3. **Cross-Validation:** Link related outputs to show governance enforcement
4. **Knowledge Base:** Future developers can understand design decisions
5. **Audit Trail:** Complete documentation for compliance/security reviews
6. **Efficiency:** Reuse patterns from previous agent outputs

---

## 🚀 Getting Started

**First time setup (already done):**
- ✅ Directory structure created
- ✅ agent-log.md initialized
- ✅ README.md (this file) created

**When invoking an agent:**
1. Check `agent-log.md` for similar past invocations (learn from patterns)
2. Invoke agent via Task tool
3. Save full agent output to appropriate directory
4. Update `agent-log.md` with invocation details
5. Cross-reference with related outputs

**Before starting Phase 1:**
- Review this structure
- Familiarize yourself with file naming conventions
- Prepare to save `/new-session` output (first agent invocation)

---

**Ready to build with organized, traceable, governance-enforced agent workflows.** 🚀
