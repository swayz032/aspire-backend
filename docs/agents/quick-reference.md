# Agent Invocation Quick Reference
**Purpose:** Fast lookup guide for when to invoke each custom agent.

**Created:** 2026-01-04

---

## 🚦 DECISION FLOWCHART

```
Starting a new task?
  ↓
Does it involve NEW architecture or subsystems?
  YES → aspire-system-architect
  NO ↓

Does it involve NEW MCP tool integration?
  YES → mcp-toolsmith → policy-gate-engineer → implement → receipt-ledger-auditor → aspire-test-engineer
  NO ↓

Did I just implement RECEIPTS or state-changing logic?
  YES → receipt-ledger-auditor
  NO ↓

Did I just complete a FEATURE?
  YES → aspire-test-engineer
  NO ↓

Am I approaching a PHASE MILESTONE?
  YES → security-reviewer (end of Phase 1, 2, 3, 4, 6)
  NO ↓

Am I in PHASE 4 (Production Hardening)?
  YES → proof-artifacts-builder → security-reviewer → release-sre → aspire-test-engineer (SEQUENTIAL)
  NO ↓

Continue implementation (no agent needed)
```

---

## 📋 AGENT TRIGGER CHEATSHEET

### aspire-system-architect (Opus, Red)
**INVOKE WHEN:**
- ✅ Starting a new phase with architectural decisions
- ✅ Designing a new subsystem (multi-operator, new governance layer)
- ✅ Major refactoring that affects core architecture
- ✅ Need to validate design against 7 Immutable Laws

**DON'T INVOKE FOR:**
- ❌ Small feature additions (new button, simple API endpoint)
- ❌ Bug fixes
- ❌ UI tweaks that don't affect architecture

**TYPICAL INVOCATIONS:**
- Phase 1, Day 1: Overall architecture design
- Phase 3, Day 1: Mobile UI architecture
- Phase 6, Week 45: Multi-operator architecture

---

### receipt-ledger-auditor (Sonnet, Red)
**INVOKE WHEN:**
- ✅ After implementing receipt system (Phase 1)
- ✅ After EVERY new skill pack integration
- ✅ After ANY code that generates receipts
- ✅ After workflow modifications
- ✅ After state-changing operations added
- ✅ Periodic audits during Phase 5 (continuous monitoring)

**DON'T INVOKE FOR:**
- ❌ Read-only operations
- ❌ UI-only changes
- ❌ Changes that don't touch state/receipts

**TYPICAL INVOCATIONS:**
- End of Phase 1: Receipt system audit
- After EVERY Phase 2 skill pack (7 times)
- After Phase 3: Mobile receipt generation
- Ongoing Phase 5: Continuous audit

---

### policy-gate-engineer (Sonnet, Blue)
**INVOKE WHEN:**
- ✅ After mcp-toolsmith designs a new tool contract
- ✅ When defining risk tiers for new tools
- ✅ When execution rules or staged autonomy changes
- ✅ After security incidents requiring policy updates
- ✅ When implementing capability tokens

**DON'T INVOKE FOR:**
- ❌ Features that don't involve tool execution
- ❌ UI-only changes
- ❌ Read-only operations

**TYPICAL INVOCATIONS:**
- Phase 1: Initial capability token architecture
- Phase 2: Risk tier for EACH new tool (7 times)
- Phase 6: Risk tier for Phase 2 skill packs (3 times)

---

### mcp-toolsmith (Sonnet, Cyan)
**INVOKE WHEN:**
- ✅ Before integrating ANY new external API/service
- ✅ When adding MCP server integration
- ✅ When hardening existing integrations
- ✅ When creating tool contracts for partners

**DON'T INVOKE FOR:**
- ❌ Internal functions (not MCP tools)
- ❌ UI components
- ❌ Database operations (unless exposing as tool)

**TYPICAL INVOCATIONS:**
- Phase 2: EVERY new integration (Stripe, QuickBooks, LiveKit, Gmail, Calendar, DocuSign, Yelp/Places) - 7 times
- Phase 6: New skill packs (Hiring, Tax/Compliance, Notary) - 3 times
- Phase 1 (OPTIONAL): NeMo Guardrails/Presidio if MCP-wrapped

---

### aspire-test-engineer (Sonnet, Yellow)
**INVOKE WHEN:**
- ✅ After EVERY feature completion
- ✅ After EVERY skill pack implementation
- ✅ End of EVERY phase (comprehensive testing)
- ✅ Before production deployment
- ✅ When security-critical code is added

**DON'T INVOKE FOR:**
- ❌ Trivial changes (typo fixes, comment updates)
- ❌ Work-in-progress code (wait until feature complete)

**TYPICAL INVOCATIONS:**
- End of Phase 1: Comprehensive orchestrator tests
- After EVERY Phase 2 skill pack (7 times)
- After Phase 3: Mobile app tests
- Phase 4: Final production gate testing
- After EVERY Phase 6 skill pack (3 times)

---

### security-reviewer (Sonnet, Orange)
**INVOKE WHEN:**
- ✅ End of major phases (Phase 1, 2, 3, 4, 6)
- ✅ After major integrations (Stripe, QuickBooks, LiveKit)
- ✅ After auth/authorization changes
- ✅ Before production deployment
- ✅ During Phase 5 (ongoing monitoring)
- ✅ After security incidents

**DON'T INVOKE FOR:**
- ❌ UI-only changes
- ❌ Non-security features
- ❌ Small iterations (batch multiple features for efficiency)

**TYPICAL INVOCATIONS:**
- End of Phase 1: Initial security audit
- Week 14 Phase 2: Mid-phase integration audit
- End of Phase 2: Comprehensive audit
- End of Phase 3: Mobile security audit
- Phase 4: Comprehensive 5-pillar audit (MANDATORY)
- Ongoing Phase 5: Security monitoring
- Phase 6, Week 52: Final pre-launch audit

---

### release-sre (Opus, Green)
**INVOKE WHEN:**
- ✅ Phase 4 production hardening (PRIMARY USE)
- ✅ Before major production deployments
- ✅ Phase 5 for production monitoring setup
- ✅ Phase 6 cloud migration planning

**DON'T INVOKE FOR:**
- ❌ Development/testing environments
- ❌ Early phases (Phase 1-3)
- ❌ Non-production work

**TYPICAL INVOCATIONS:**
- Phase 4, Week 27-28: Production readiness review (MAIN)
- Ongoing Phase 5: Monitoring/incident response support
- Phase 6, Week 48-51: Cloud migration strategy

---

### proof-artifacts-builder (Opus, Cyan)
**INVOKE WHEN:**
- ✅ Phase 4 production hardening (PRIMARY USE)
- ✅ Major milestones requiring comprehensive documentation
- ✅ Stakeholder/compliance documentation needed

**DON'T INVOKE FOR:**
- ❌ Routine feature documentation
- ❌ Internal design docs (use aspire-system-architect)
- ❌ Early phases (overkill before production)

**TYPICAL INVOCATIONS:**
- Phase 4, Week 24-25: Complete 10/10 Bundle (MAIN)
- Optional: After major milestones if stakeholder docs needed

---

### /new-session (Sonnet)
**INVOKE WHEN:**
- ✅ Phase 1, Week 4, Day 1 (START OF PROJECT)
- ✅ After long breaks in development (re-initialize)
- ✅ When switching to new development environment

**DON'T INVOKE FOR:**
- ❌ Daily development work
- ❌ Mid-phase (only at phase starts)

**TYPICAL INVOCATIONS:**
- Phase 1, Day 1: ONE-TIME initialization

---

## 🎯 PHASE-SPECIFIC QUICK GUIDE

### Phase 1 (Weeks 4-9) - 6-8 Invocations
```
Week 4, Day 1:
  /new-session                    # Initialize
  aspire-system-architect         # Design architecture

Week 5-6:
  policy-gate-engineer            # Capability tokens
  mcp-toolsmith (OPTIONAL)        # If NeMo/Presidio need MCP wrapper

Week 7-8:
  receipt-ledger-auditor          # Audit receipt system

Week 9:
  aspire-test-engineer            # Comprehensive tests
  security-reviewer               # Security audit
```

### Phase 2 (Weeks 10-17) - 28-35 Invocations
```
FOR EACH SKILL PACK (7 total):
  mcp-toolsmith                   # Tool contract design
  [implement integration]
  policy-gate-engineer            # Risk tier
  receipt-ledger-auditor          # Verify receipts
  aspire-test-engineer            # RLS + evil tests

Week 14:
  security-reviewer               # Mid-phase audit

Week 17:
  security-reviewer               # End-of-phase audit
```

### Phase 3 (Weeks 18-23) - 4-6 Invocations
```
Week 18, Day 1:
  aspire-system-architect         # Mobile UI architecture

Week 22:
  receipt-ledger-auditor          # Mobile receipts
  aspire-test-engineer            # Mobile tests

Week 23:
  security-reviewer               # Mobile security audit
```

### Phase 4 (Weeks 24-29) - 4 MANDATORY (Sequential)
```
Week 24-25:
  proof-artifacts-builder         # 10/10 Bundle

Week 26-27:
  security-reviewer               # 5-pillar audit

Week 27-28:
  release-sre                     # Production readiness

Week 29:
  aspire-test-engineer            # Final gate testing
```

### Phase 5 (Weeks 30-37) - Continuous Monitoring
```
Ongoing:
  release-sre                     # Production monitoring
  security-reviewer               # Security monitoring
  receipt-ledger-auditor          # Continuous audit
```

### Phase 6 (Weeks 38-53) - 15-20 Invocations
```
Weeks 38-44 (3 skill packs):
  Same as Phase 2 pattern (4-5 agents per pack)

Week 45-48 (Multi-operator):
  aspire-system-architect         # Architecture design
  security-reviewer               # Isolation audit
  aspire-test-engineer            # Multi-operator tests

Week 48-51 (Cloud migration):
  release-sre                     # Migration strategy

Week 52:
  security-reviewer               # Final audit
```

---

## ⚡ EMERGENCY DECISION MATRIX

| Situation | Agent to Invoke | Priority |
|-----------|-----------------|----------|
| **Just finished feature** | `aspire-test-engineer` | HIGH |
| **Added new API integration** | `mcp-toolsmith` → `policy-gate-engineer` → `receipt-ledger-auditor` → `aspire-test-engineer` | CRITICAL |
| **Changed receipts/state** | `receipt-ledger-auditor` | HIGH |
| **Starting new phase** | `aspire-system-architect` | MEDIUM |
| **End of phase** | `security-reviewer` | HIGH |
| **Production deployment imminent** | `proof-artifacts-builder` → `security-reviewer` → `release-sre` → `aspire-test-engineer` | CRITICAL |
| **Security incident** | `security-reviewer` + `policy-gate-engineer` | CRITICAL |
| **Architecture refactor** | `aspire-system-architect` | MEDIUM |
| **Performance issues** | `release-sre` | LOW (unless production) |

---

## 🔥 COMMON MISTAKES TO AVOID

### ❌ DON'T:
1. Invoke `aspire-system-architect` for trivial features
2. Skip `aspire-test-engineer` after feature completion
3. Forget `receipt-ledger-auditor` after state-changing code
4. Invoke agents in parallel when sequential is needed
5. Use agents for UI-only changes
6. Skip `security-reviewer` at phase boundaries
7. Invoke `proof-artifacts-builder` before Phase 4

### ✅ DO:
1. Follow the Tool Integration Loop pattern religiously
2. Always test after features (`aspire-test-engineer`)
3. Always audit after receipts (`receipt-ledger-auditor`)
4. Save all agent outputs to `docs/agents/`
5. Update `agent-log.md` after every invocation
6. Use sequential execution for Phase 4 agents
7. Invoke `/new-session` ONCE at Phase 1 start

---

## 📞 "WHICH AGENT DO I NEED?" HOTLINE

**Q: "I just added a new Stripe payment endpoint. Which agents?"**
A: `mcp-toolsmith` → `policy-gate-engineer` → implement → `receipt-ledger-auditor` → `aspire-test-engineer`

**Q: "I'm starting Phase 1. Which agents?"**
A: `/new-session` → `aspire-system-architect`

**Q: "I just finished the mobile app. Which agents?"**
A: `receipt-ledger-auditor` → `aspire-test-engineer` → `security-reviewer`

**Q: "I'm about to deploy to production. Which agents?"**
A: `proof-artifacts-builder` → `security-reviewer` → `release-sre` → `aspire-test-engineer` (SEQUENTIAL)

**Q: "I changed how receipts are generated. Which agents?"**
A: `receipt-ledger-auditor` → `aspire-test-engineer`

**Q: "I'm designing multi-operator architecture. Which agents?"**
A: `aspire-system-architect` → implement → `security-reviewer` → `aspire-test-engineer`

---

**Keep this guide handy during development. When in doubt, check the flowchart at the top.** 🚀
