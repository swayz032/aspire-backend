# New Aspire Agent — Copy-Paste Guide

> **Time to create a new agent: ~30 minutes**
> All agents inherit from `AgenticSkillPack` (memory + multi-step reasoning).
> All agents comply with the 10 Laws. The behavior contract is the shared constitution.

Quick start, one command:
`scripts\new_agent.cmd --agent-name Blake --role-title "Banking Specialist" --domain banking --actions read,write`

Preset quick starts:
- `scripts\new_agent.cmd --agent-name Blake --preset banking`
- `scripts\new_agent.cmd --agent-name Maya --preset healthcare`
- `scripts\new_agent.cmd --agent-name Clara --preset legal`

Post-generation validation:
`scripts\validate_agent.cmd --registry-id blake_banking`

---

## Prerequisites

Before creating a new agent, read:
1. `config/agent_behavior_contract.md` — The shared behavioral rules
2. An existing agent as reference (recommended: `eli_inbox.py` for YELLOW, `adam_research.py` for GREEN)
3. This guide

---

## Checklist

```
NEW AGENT: {AgentName} ({agent-id})
=========================================
1. [ ] Persona file:        config/pack_personas/{id}_system_prompt.md
2. [ ] Registry entry:      config/skill_pack_manifests.yaml
3. [ ] Manifest:            config/pack_manifests/{id}.json
4. [ ] Risk policy:         config/pack_policies/{id}/risk_policy.yaml
5. [ ] Tool policy:         config/pack_policies/{id}/tool_policy.yaml
6. [ ] Autonomy policy:     config/pack_policies/{id}/autonomy_policy.yaml
7. [ ] Observability:       config/pack_policies/{id}/observability_policy.yaml
8. [ ] Prompt contract:     config/pack_policies/{id}/prompt_contract.md
9. [ ] Skill pack class:    skillpacks/{id}.py (extends AgenticSkillPack)
10. [ ] Memory config:      Define memory types in manifest + enable in __init__
11. [ ] Policy matrix:      Add actions to config/policy_matrix.yaml
12. [ ] Persona routing:    Update agent_reason.py _PERSONA_MAP only if adding a new persona file
13. [ ] Tests:              tests/test_{id}.py (actions + memory + governance)
```

---

## Step 1: Persona File

**Path:** `config/pack_personas/{agent_id}_system_prompt.md`

Copy `config/templates/persona_template.md` and fill in every `{PLACEHOLDER}`.

Key rules:
- Reference the behavior contract but don't copy it verbatim
- Include deep domain knowledge (this is what makes the agent credible)
- Define team delegation (which agents to route to for cross-domain)
- Include a Memory section describing what this agent remembers

---

## Step 2: Runtime Registry

**Path:** `config/skill_pack_manifests.yaml`

Add the new agent to the central registry. This is the routing source of truth
used by the orchestrator, registry, and skill pack factory.

Required fields:
- `id`: Pack identifier used by routing
- `owner`: Persona/agent owner key
- `actions`: Action types routed to this pack
- `tools`: Tool allowlist exposed to the pack
- `providers`: External integrations used by the pack
- `risk_tier`: Highest supported risk tier for the pack

If this YAML entry is missing, the pack may load locally but it will not be
routable by the control plane.

---

## Step 3: Manifest

**Path:** `config/pack_manifests/{agent-id}.json`

Copy `config/templates/manifest_template.json` and fill in:
- `skillpack_id`: kebab-case identifier (must match risk policy `pack_id`)
- `agent_name`: First name only (used in pack metadata and UI)
- `capabilities`: List of `can_{action}` strings
- `risk_profile`: Default and max risk tiers
- `tools`: List of tool identifiers this agent can use
- `actions`: List of `{domain}.{action}` strings (must match policy matrix)
- `memory.enabled`: true/false
- `memory.fact_types`: Which fact types this agent stores

---

## Step 4: Risk Policy

**Path:** `config/pack_policies/{agent-id}/risk_policy.yaml`

Copy `config/templates/risk_policy_template.yaml` and define:
- Every action this agent can perform
- Risk tier (green/yellow/red) for each action
- Whether approval is required
- Optional DLP redaction config

The one-command scaffold also creates:
- `tool_policy.yaml` for authorized tool defaults
- `autonomy_policy.yaml` for iteration/time budget defaults
- `observability_policy.yaml` for receipt/alert expectations
- `prompt_contract.md` for prompt and response guardrails

---

## Step 5: Skill Pack Class

**Path:** `skillpacks/{agent_id}.py`

Copy the `_TemplateSkillPack` class from `config/templates/skillpack_template.py`.

Key decisions:
- **Subclass `AgenticSkillPack`** (not `EnhancedSkillPack` directly) to get memory + agentic loop
- Set `memory_enabled=True` if the agent should learn from interactions
- Implement one method per action (matching your manifest's `actions` list)
- Use `execute_with_llm()` for LLM-powered actions
- Use `run_agentic_loop()` for complex multi-step tasks
- Every method must emit a receipt (success AND failure)
- Every method must validate params and fail closed (Law #3)

Pattern for each action method:
```python
async def {action}(self, params: dict, ctx: AgentContext) -> AgentResult:
    # 1. Validate required params (fail closed)
    # 2. Check memory for relevant context (optional, agentic)
    # 3. Execute (LLM call, provider call, or agentic loop)
    # 4. Store insights to memory (optional, agentic)
    # 5. Emit receipt
    # 6. Return AgentResult
```

---

## Step 6: Policy Matrix

**Path:** `config/policy_matrix.yaml`

Add entries for every action your agent handles:

```yaml
{domain}.{action}:
  risk_tier: green|yellow|red
  tools:
    - "{provider}.{domain}.{action}"
  capability_scope: "{domain}:{action}"
  approval:
    type: none|explicit|spend|legal
  params:
    required: ["{param1}", "{param2}"]
  redact_fields: ["{sensitive_field}"]
  category: "{functional_domain}"
```

---

## Step 7: Persona Routing

`respond.py` resolves persona identity through shared agent identity helpers.
Do not add a local `_PERSONA_MAP` there.

If the new agent introduces a new persona file, update:

**`nodes/agent_reason.py`** — Add to `_PERSONA_MAP`:
```python
"{agent_first_name_lower}": "{agent_id}_system_prompt",
```

---

## Step 8: Tests

**Path:** `tests/test_{agent_id}.py`

Copy `config/templates/test_template.py` and implement:

Required test categories:
1. **Config loading** — manifest, persona, policies load correctly
2. **Action success** — each action returns success with correct receipt
3. **Action failure** — missing params → denied receipt (fail closed)
4. **Memory operations** — remember, recall, search, forget
5. **Governance** — receipts have required fields, tenant-scoped, risk tiers correct
6. **Agentic loop** — if agent uses `run_agentic_loop()`, test bounds and timeout

---

## Naming Conventions

| Context | Format | Example |
|---------|--------|---------|
| Python class | `CamelCaseSkillPack` | `BlakeBankSkillPack` |
| Python file | `snake_case.py` | `blake_bank.py` |
| Pack ID (manifests/policies) | `kebab-case` | `blake-bank` |
| Persona map key | `lowercase` | `blake` |
| Persona file | `{id}_system_prompt.md` | `blake_bank_system_prompt.md` |
| Action name | `{domain}.{action}` | `banking.read` |
| Capability scope | `{domain}:{action}` | `banking:read` |
| Memory fact key | `{domain}_{topic}` | `banking_client_preferences` |
| Test file | `test_{id}.py` | `test_blake_bank.py` |

---

## Architecture: How It All Connects

```
User Intent
  ↓
Intent Classifier (services/intent_classifier.py)
  ↓ routes to skill pack based on action
Policy Check (config/policy_matrix.yaml)
  ↓ verifies risk tier, approval, token
Orchestrator calls skill pack method
  ↓
Your Skill Pack (skillpacks/{id}.py)
  ├── recall memory (optional)
  ├── execute_with_llm() or run_agentic_loop()
  ├── store memory (optional)
  ├── emit receipt
  └── return AgentResult
  ↓
Respond Node (nodes/respond.py)
  ↓ loads persona, generates conversational response
User sees natural language response
```

---

## Common Patterns

### GREEN read-only action
```python
async def read_data(self, params, ctx):
    return await self.execute_with_llm(
        prompt=f"Read: {params['query']}",
        ctx=ctx, event_type="{domain}.read",
        step_type="classify", inputs=params,
    )
```

### YELLOW action with approval
```python
async def create_thing(self, params, ctx):
    # Validation, LLM draft, return for approval
    # Orchestrator handles approval gate before execution
    result = await self.execute_with_llm(...)
    await self.remember(f"created_{params['name']}", "details...", ctx)
    return result
```

### Multi-step agentic action
```python
async def complex_analysis(self, task, ctx):
    return await self.run_agentic_loop(
        task=task, ctx=ctx, max_steps=3, timeout_s=25.0,
    )
```

---

## Verification

After creating all files:

1. **Unit tests pass:** `pytest tests/test_{id}.py -v`
2. **Scaffold validation passes:** `scripts\validate_agent.cmd --registry-id {id}`
3. **Config loads:** Instantiate your pack — registry entry, manifest, persona, and policies should load without errors
4. **Full suite passes:** `pytest tests/ -q --tb=short` — zero regressions
5. **Naming matches:** Pack ID in manifest == pack_id in risk policy == persona map key maps to correct file
