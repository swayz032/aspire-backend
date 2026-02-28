# Roadmap Phase Alignment (Ava User -> Aspire Phases)

**Source:** Ava User Enterprise Handoff v1.1

## Phase mapping

| Ava User Phase | Aspire Phase | Focus |
|---|---|---|
| Phase 1 | Phase 1 (Weeks 3-8) | Orchestrator + schema enforcement |
| Phase 2 | Phase 1B-2 (Weeks 4-18) | Policy + approval + presence |
| Phase 3 | Phase 3+ (Weeks 17+) | Insight Engine + Exception Cards |
| Phase 4 | Phase 3+ | Ritual Engine + Retention loops |
| Phase 5 | Phase 3+ | Research automation + n8n scheduling |
| Phase 6 | Phase 6 (Weeks 35-46) | Scale + telemetry + cohort caching |

## Key deliverables per Aspire phase

### Phase 1 (Orchestrator)
- AvaOrchestratorRequest as POST /v1/intents request schema
- AvaResult as /v1/intents response schema (normalized to lowercase risk tiers)
- Receipt hash chain implementation
- Error code taxonomy adoption
- Policy engine (POST /v1/policy/evaluate)

### Phase 2 (Founder Quarter MVP)
- 4 initial skill packs wired through AvaOrchestratorRequest
- Approval binding with payload-hash integrity
- Presence session binding for red tier

### Phase 3+ (Certification)
- Insight Engine (ExceptionCards, cohort caching)
- Ritual Engine (Weekly Review, Monthly Close)
- Research routing + n8n scheduling
