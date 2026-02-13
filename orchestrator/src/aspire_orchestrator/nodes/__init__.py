"""LangGraph node implementations for the Aspire orchestrator pipeline.

8 nodes matching the canonical flow from architecture.md:
1. intake - Validate AvaOrchestratorRequest, derive suite_id from auth
2. safety_gate - NeMo Guardrails jailbreak/topic detection
3. policy_eval - Policy engine evaluation (risk tier, allowlist)
4. approval_check - Yellow/Red tier approval verification
5. token_mint - Capability token creation for approved actions
6. execute - Bounded tool execution via skill packs
7. receipt_write - Immutable receipt chain entry
8. respond - AvaResult construction and egress validation
"""
