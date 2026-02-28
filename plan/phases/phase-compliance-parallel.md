---
phase: "COMPLIANCE"
name: "SOC 2 Readiness (Parallel Track)"
status: "not_started"
blocking_phase: null
blocks_phases: []
duration_estimate: "Ongoing (Weeks 1-14, parallel with Phases 0-2)"
gates_satisfied: []
priority: "high"
parallel_execution: true
handoff_provides: "Complete SOC 2 compliance framework (policies, procedures, runbooks, evidence schemas)"
---

# COMPLIANCE TRACK: SOC 2 Readiness (Parallel Execution)

## Objective

Achieve SOC 2 Type II readiness by end of Phase 2 (instead of Phase 4) by running compliance work in parallel with engineering phases.

**⚠️ CRITICAL**: This track accelerates compliance by 6+ months, enabling SOC 2 audit readiness by Phase 3 start.

---

## Dependencies

**Requires (Blocking):**
- None (can start immediately with handoff package)

**Blocks (Downstream):**
- None (parallel track, does not block engineering phases)

---

## Handoff Package Contents

**Status:** ✅ COMPLETE SOC 2 FRAMEWORK PROVIDED

The handoff package includes a production-ready SOC 2 compliance framework.

**What's Included:**

---

## Trust Spine Ecosystem Documentation (IMPORTANT)

**📚 Evidence collection and SOC 2 compliance documentation exists in the Trust Spine package:**

### Evidence Collection Resources
- **Navigation Guide:** See `Trust-Spine-Package-Navigation.md` (in this plan directory) for evidence generation workflow
- **Receipts as Audit Trail:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/08_CLAUDE_JOB_KIT/ADR/ADR-0002-receipts-v1.md` for:
  - Append-only receipt ledger (immutable audit trail for SOC 2)
  - Hash-chained integrity (tamper-evident evidence)
  - 100% action coverage (complete audit trail)
- **Receipt API:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/02_CANONICAL/openapi.unified.yaml` for receipt retrieval API (evidence export)

### SOC 2 Compliance Resources
- **Security Documentation:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/06_ADDONS/security/THREAT_MODEL.md` for:
  - 5 security pillars (network boundary, credentials, shadow execution prevention, tenant isolation, safe logging)
  - Threat analysis and mitigation strategies
  - Evil tests for security validation
- **RLS Policies:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/13_E2E_TESTS/sql/tenant_isolation.sql` for SOC 2 data segregation evidence
- **Incident Runbooks:** See `${ECOSYSTEM_ROOT}/platform/trust-spine/06_ADDONS/ops/runbooks/` for SOC 2 incident response procedures

### Evidence Generation Workflow
1. **Receipts Generated:** All actions logged via Trust Spine receipts-api (Phase 0B deployment)
2. **Evidence Export:** Use `GET /v1/receipts` API to export receipts for SOC 2 audit trail
3. **Hash Chain Verification:** Use Go verification service to prove tamper-evidence
4. **Compliance Reporting:** Generate SOC 2 evidence reports from receipt ledger

**⭐ Start Here:** Read `Trust-Spine-Package-Navigation.md` → then ADR-0002 for receipts as audit trail.

---

**What's Included:**

### 1. Control-to-Evidence Mapping
- - - Maps SOC 2 controls to Aspire implementation evidence

### 2. Evidence Schemas
-  directory
- JSON schemas for evidence export
- Receipt artifact format specifications

### 3. Policies (9 Documents)
- Access Control Policy
- Business Continuity & Disaster Recovery
- Data Classification & Handling
- Incident Response Policy
- Information Security Policy
- Privacy Policy
- Risk Management Policy
- SDLC Policy
- Vendor Management Policy

### 4. Procedures
- Access review procedures
- Alerting procedures
- Backup procedures
- Change management
- Privacy request handling

### 5. Runbooks
- Incident response runbooks
- Rollback procedures
- Escalation paths

### 6. Implementation Guides
-  (build list)
-  (vendor evaluation)
-  (Level 2 requirements)

---

## Timeline (Parallel with Engineering Phases)

### Weeks 1-2 (Phase 0B)
- [ ] Copy compliance pack to  directory
- [ ] Review control-to-evidence mapping
- [ ] Identify controls already satisfied by handoff (Phase 0/1 infrastructure)

### Weeks 3-6 (Phase 1)
- [ ] Implement evidence exporter endpoint
- [ ] Update policies with Aspire-specific details
- [ ] Configure evidence export automation

### Weeks 7-14 (Phase 2)
- [ ] Ongoing: Access reviews
- [ ] Ongoing: Incident runbook drills
- [ ] Vanta integration decision
- [ ] Generate sample evidence bundle
- [ ] Validate control coverage

**Result:** SOC 2 audit-ready by Phase 3 start (Week 15)

---

## Tasks

### 1. Setup & Review (Weeks 1-2)

- [ ]  **Copy Compliance Pack**
  -   - Verify all directories created

- [ ]  **Review Control Mapping**
  - Read   - Identify controls satisfied by handoff Phase 0/1
  - Document gaps

### 2. Evidence Infrastructure (Weeks 3-6)

- [ ]  **Implement Evidence Exporter**
  - Endpoint:   - Use schemas from   - Export format: 
- [ ]  **Receipt Artifact Storage**
  - Implement S3 storage for receipt artifacts
  - 7-year retention policy
  - Immutable storage enforcement

### 3. Policy Documentation (Weeks 3-6)

- [ ]  **Update Access Control Policy**
  - Replace placeholders with Aspire specifics
  - Define RBAC roles (Suite Owner, Office User, Admin)

- [ ]  **Update Incident Response Policy**
  - Define severity levels (LOW/MED/HIGH/CRITICAL)
  - Escalation procedures
  - Communication protocols

- [ ]  **Update Remaining 7 Policies**
  - Data Classification, BCP/DR, Privacy, Risk, SDLC, Vendor, InfoSec
  - Aspire-specific customization

### 4. Operational Procedures (Weeks 7-14)

- [ ]  **Access Review Procedure**
  - Quarterly review schedule
  - Automated user enumeration
  - Approval workflow

- [ ]  **Incident Runbook Drills**
  - Game-day simulation
  - Test escalation paths
  - Document lessons learned

- [ ]  **Vanta Integration Decision**
  - Review   - Decide: DIY evidence collection vs Vanta automation
  - If Vanta: Configure integration

### 5. Evidence Generation (Weeks 7-14)

- [ ]  **Generate Sample Evidence Bundle**
  - Export 30-day evidence window
  - Validate format compliance
  - Test audit trail reconstruction

- [ ]  **Control Coverage Validation**
  - Verify all controls have evidence sources
  - Document any gaps
  - Plan remediation

---

## Success Criteria

### Implementation Success Criteria

- [ ]  Evidence exporter operational
- [ ]  All 9 policies documented and approved
- [ ]  Control-to-evidence mapping validated
- [ ]  Sample evidence bundle generated
- [ ]  Vanta integration decision made
- [ ]  Access review procedure tested
- [ ]  Incident runbook drill completed

---

## Related Artifacts

**Created in This Phase:**
- Evidence exporter endpoint
- Updated policies (9 documents)
- Operational procedures
- Sample evidence bundle

**From Handoff:**
- Control-to-evidence mapping
- Evidence schemas
- Policy templates
- Procedure templates
- Runbook templates

---

## Related Gates

**No production gates blocked by this phase** (parallel track)

**Gates Enabled:**
- Gate 6: Receipts Immutable (compliance evidence depends on this)
- Gate 7: RLS Isolation (tenant data separation for compliance)

---

## Estimated Duration

**Ongoing:** Weeks 1-14 (parallel with Phases 0B through Phase 2)

**Milestones:**
- Week 2: Compliance pack integrated
- Week 6: Evidence exporter + policies complete
- Week 14: SOC 2 audit-ready

**Acceleration:** SOC 2 readiness achieved 6+ months earlier (Phase 3 vs Phase 4)

---

## Cost

**/usr/bin/bash-100/mo** (optional Vanta subscription)

**Breakdown:**
- Vanta: /usr/bin/bash/mo (if DIY) or 00/mo (if automated)
- Evidence storage: /usr/bin/bash/mo (S3 free tier sufficient for Phase 2)

---

## Related Files

- **Main Roadmap:** [Aspire-Production-Roadmap.md](../Aspire-Production-Roadmap.md)
- **Handoff Package:** - **Control Mapping:** - **Engineering Tasks:** 
---

**Last Updated:** 2026-02-08
**Status:** ⏳ NOT STARTED
