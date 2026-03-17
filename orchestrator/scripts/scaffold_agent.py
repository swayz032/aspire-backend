"""Scaffold and validate AgenticSkillPack assets.

Usage:
    uv run --no-project python scripts/scaffold_agent.py --agent-name Blake --role-title "Banking Specialist" --domain banking --actions read,write
    uv run --no-project python scripts/scaffold_agent.py scaffold --preset banking --agent-name Blake
    uv run --no-project python scripts/scaffold_agent.py validate
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = ROOT / "src" / "aspire_orchestrator"
CONFIG_ROOT = SRC_ROOT / "config"


@dataclass(frozen=True)
class ScaffoldSpec:
    agent_name: str
    role_title: str
    domain: str
    actions: list[str]
    owner_key: str
    registry_id: str
    manifest_id: str
    category: str
    provider: str
    role_description: str
    description: str
    tone: str
    memory_enabled: bool
    prompt_style: str = "operational"
    preset_name: str | None = None
    observability_tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Preset:
    role_title: str
    domain: str
    actions: tuple[str, ...]
    category: str
    provider: str
    role_description: str
    description: str
    tone: str
    prompt_style: str
    observability_tags: tuple[str, ...]


@dataclass(frozen=True)
class ValidationTarget:
    registry_id: str
    owner_key: str
    manifest_id: str
    persona_filename: str
    manifest: dict[str, object]


PRESETS: dict[str, Preset] = {
    "banking": Preset(
        role_title="Banking Operations Specialist",
        domain="banking",
        actions=("accounts.read", "transactions.review", "transfer.prepare"),
        category="finance",
        provider="internal",
        role_description="Banking operations specialist",
        description="Banking operations workflows with governed transfer preparation",
        tone="direct",
        prompt_style="operational",
        observability_tags=("banking", "finance", "receipts"),
    ),
    "healthcare": Preset(
        role_title="Healthcare Operations Specialist",
        domain="healthcare",
        actions=("patient.read", "intake.summarize", "scheduling.prepare"),
        category="operations",
        provider="internal",
        role_description="Healthcare operations specialist",
        description="Healthcare operations workflows focused on intake, summaries, and scheduling prep",
        tone="calm",
        prompt_style="compliance-first",
        observability_tags=("healthcare", "ops", "audit"),
    ),
    "legal": Preset(
        role_title="Legal Operations Specialist",
        domain="legal",
        actions=("contract.review", "clause.draft", "signature.prepare"),
        category="legal",
        provider="internal",
        role_description="Legal operations specialist",
        description="Legal operations workflows for review, drafting, and signature preparation",
        tone="precise",
        prompt_style="legal-risk-aware",
        observability_tags=("legal", "contracts", "governance"),
    ),
}

KNOWN_SUBCOMMANDS = {"scaffold", "validate", "certify", "list-presets"}


def slugify(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower()).strip("-")
    return text or "agent"


def snakeify(value: str) -> str:
    return slugify(value).replace("-", "_")


def camelize(value: str) -> str:
    return "".join(part.capitalize() for part in re.split(r"[^A-Za-z0-9]+", value) if part)


def normalize_actions(domain: str, actions_raw: str) -> list[str]:
    domain_key = snakeify(domain)
    actions: list[str] = []
    for raw in actions_raw.split(","):
        action = raw.strip()
        if not action:
            continue
        if "." not in action:
            action = f"{domain_key}.{snakeify(action)}"
        else:
            left, right = action.split(".", 1)
            action = f"{snakeify(left)}.{snakeify(right)}"
        if action not in actions:
            actions.append(action)
    if not actions:
        raise ValueError("At least one action is required")
    return actions


def infer_action_risk(action: str) -> str:
    verb = action.split(".", 1)[1].split(".")[-1]
    green = {"read", "list", "search", "get", "browse", "preview", "details", "summarize", "snapshot", "review"}
    red = {"delete", "run", "transfer", "purchase", "sign", "pay", "payroll", "execute"}
    if verb in green:
        return "green"
    if verb in red:
        return "red"
    return "yellow"


def infer_max_risk(actions: list[str]) -> str:
    order = {"green": 0, "yellow": 1, "red": 2}
    return max((infer_action_risk(action) for action in actions), key=lambda item: order[item])


def binding_fields_for_action(action: str) -> list[str]:
    risk = infer_action_risk(action)
    if risk == "green":
        return ["query"]
    verb = action.split(".", 1)[1].split(".")[-1]
    if verb in {"transfer", "submit", "execute", "sign", "send", "prepare"}:
        return ["request", "approval_context"]
    return ["request"]


def choose_alerts(actions: list[str], memory_enabled: bool) -> list[str]:
    alerts = ["receipt_failure_rate", "agent_loop_timeout_rate", "policy_denial_spike"]
    if any(infer_action_risk(action) == "red" for action in actions):
        alerts.append("high_risk_action_attempts")
    if memory_enabled:
        alerts.append("memory_write_failures")
    return alerts


def build_spec(args: argparse.Namespace) -> ScaffoldSpec:
    preset_name = getattr(args, "preset", None)
    preset = PRESETS.get(preset_name) if preset_name else None
    if preset_name and preset is None:
        raise ValueError(f"Unknown preset: {preset_name}")

    agent_name = str(getattr(args, "agent_name", "") or "").strip()
    if not agent_name:
        raise ValueError("--agent-name is required")

    role_title = str(getattr(args, "role_title", "") or (preset.role_title if preset else "")).strip()
    domain_raw = str(getattr(args, "domain", "") or (preset.domain if preset else "")).strip()
    actions_raw = str(getattr(args, "actions", "") or (",".join(preset.actions) if preset else "")).strip()

    if not role_title:
        raise ValueError("--role-title is required when --preset is not used")
    if not domain_raw:
        raise ValueError("--domain is required when --preset is not used")
    if not actions_raw:
        raise ValueError("--actions is required when --preset is not used")

    owner_key = snakeify(getattr(args, "owner_key", None) or agent_name)
    domain_key = snakeify(domain_raw)
    actions = normalize_actions(domain_key, actions_raw)
    registry_id = snakeify(getattr(args, "registry_id", None) or f"{owner_key}_{domain_key}")
    manifest_id = slugify(getattr(args, "manifest_id", None) or registry_id)
    provider = snakeify(getattr(args, "provider", None) or (preset.provider if preset else "internal"))
    category = str(getattr(args, "category", None) or (preset.category if preset else "internal"))
    role_description = str(
        getattr(args, "role_description", None) or (preset.role_description if preset else role_title)
    ).strip()
    description = str(
        getattr(args, "description", None)
        or (preset.description if preset else f"{role_title} workflows for {domain_key.replace('_', ' ')}")
    ).strip()
    tone = str(getattr(args, "tone", None) or (preset.tone if preset else "direct")).strip()
    prompt_style = str(getattr(args, "prompt_style", None) or (preset.prompt_style if preset else "operational")).strip()

    observability_tags = list(preset.observability_tags) if preset else [domain_key]
    for raw_tag in str(getattr(args, "observability_tags", "") or "").split(","):
        if not raw_tag.strip():
            continue
        tag = snakeify(raw_tag)
        if tag not in observability_tags:
            observability_tags.append(tag)

    return ScaffoldSpec(
        agent_name=agent_name,
        role_title=role_title,
        domain=domain_key,
        actions=actions,
        owner_key=owner_key,
        registry_id=registry_id,
        manifest_id=manifest_id,
        category=category,
        provider=provider,
        role_description=role_description,
        description=description,
        tone=tone,
        memory_enabled=not bool(getattr(args, "no_memory", False)),
        prompt_style=prompt_style,
        preset_name=preset_name,
        observability_tags=observability_tags,
    )


def render_skillpack(spec: ScaffoldSpec) -> str:
    class_name = f"{camelize(spec.agent_name)}{camelize(spec.domain)}SkillPack"
    method_blocks: list[str] = []
    dispatch_pairs: list[str] = []

    for action in spec.actions:
        method_name = action.replace(".", "_")
        risk = infer_action_risk(action)
        request_key = "query" if risk == "green" else "request"
        helper_name = "_handle_read_action" if risk == "green" else "_handle_write_action"
        method_blocks.append(
            f"""    async def {method_name}(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        \"\"\"Handle `{action}` requests.\"\"\"
        return await self.{helper_name}(
            action="{action}",
            params=params,
            ctx=ctx,
            required_key="{request_key}",
        )
"""
        )
        dispatch_pairs.append(f'            "{action}": self.{method_name},')

    return f'''"""Auto-generated {spec.agent_name} skill pack scaffold.

Generated by scripts/scaffold_agent.py.
"""

from __future__ import annotations

from typing import Any

from aspire_orchestrator.config.templates.skillpack_template import AgenticSkillPack
from aspire_orchestrator.services.agent_sdk_base import AgentContext, AgentResult


class {class_name}(AgenticSkillPack):
    """Starter scaffold for {spec.agent_name}."""

    def __init__(self) -> None:
        super().__init__(
            agent_id="{spec.registry_id}",
            agent_name="{spec.agent_name}",
            default_risk_tier="{infer_max_risk(spec.actions)}",
            memory_enabled={str(spec.memory_enabled)},
        )

    async def dispatch_action(self, action: str, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        handlers = {{
{chr(10).join(dispatch_pairs)}
        }}
        handler = handlers.get(action)
        if handler is None:
            receipt = self.build_receipt(
                ctx=ctx,
                event_type="skillpack.dispatch",
                status="denied",
                inputs={{"action": action}},
                metadata={{"error": "unsupported_action"}},
            )
            receipt["policy"] = {{"decision": "deny", "reasons": ["UNSUPPORTED_ACTION"]}}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error=f"Unsupported action: {{action}}")
        return await handler(params, ctx)

    async def _handle_read_action(
        self,
        *,
        action: str,
        params: dict[str, Any],
        ctx: AgentContext,
        required_key: str,
    ) -> AgentResult:
        value = str(params.get(required_key, "")).strip()
        if not value:
            receipt = self.build_receipt(
                ctx=ctx,
                event_type=action,
                status="denied",
                inputs={{"action": action}},
            )
            receipt["policy"] = {{"decision": "deny", "reasons": ["MISSING_REQUIRED_FIELD"]}}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error=f"Missing {{required_key}}")

        return await self.execute_with_llm(
            prompt=(
                f"You are {{self.agent_name}}. Handle the read-only action '{{action}}'.\n\n"
                f"Request: {{value}}\n\n"
                "Provide a concise, useful answer and clearly flag any assumptions."
            ),
            ctx=ctx,
            event_type=action,
            step_type="draft",
            inputs={{"action": action, required_key: value}},
        )

    async def _handle_write_action(
        self,
        *,
        action: str,
        params: dict[str, Any],
        ctx: AgentContext,
        required_key: str,
    ) -> AgentResult:
        value = str(params.get(required_key, "")).strip()
        if not value:
            receipt = self.build_receipt(
                ctx=ctx,
                event_type=action,
                status="denied",
                inputs={{"action": action}},
            )
            receipt["policy"] = {{"decision": "deny", "reasons": ["MISSING_REQUIRED_FIELD"]}}
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error=f"Missing {{required_key}}")

        result = await self.execute_with_llm(
            prompt=(
                f"You are {{self.agent_name}}. Prepare the governed output for '{{action}}'.\n\n"
                f"Request: {{value}}\n\n"
                "Return the proposed action, any assumptions, and the next approval-safe step."
            ),
            ctx=ctx,
            event_type=action,
            step_type="draft",
            inputs={{"action": action, required_key: value}},
        )
        if result.success and self._memory_enabled:
            await self.remember(
                key=f"{spec.domain}_last_request",
                value=value,
                ctx=ctx,
                fact_type="workflow",
                confidence=0.7,
            )
        return result

{chr(10).join(method_blocks)}
'''


def render_persona(spec: ScaffoldSpec, persona_filename: str) -> str:
    risk_summary = ", ".join(f"{action} ({infer_action_risk(action).upper()})" for action in spec.actions[:3])
    example_other_agent = "Ava"
    return f"""# {spec.agent_name} — {spec.role_title}

> Inherits: config/agent_behavior_contract.md
> Persona file: {persona_filename}

## Identity
You are {spec.agent_name}, Aspire's {spec.role_description}. You focus on {spec.description.lower()} and keep your recommendations direct, careful, and execution-oriented.

## Personality & Voice
- Tone: {spec.tone}
- Style: first person, concise, decisive
- Prompt style: {spec.prompt_style}
- You stay grounded in the facts and flag uncertainty early
- Use first person. Address the user by name when available

When someone asks who you are:
"I'm {spec.agent_name}, your {spec.role_title.lower()}. I handle {spec.domain.replace('_', ' ')} workflows and keep the next step clear."

## Capabilities
You can:
- {risk_summary}
- Propose the safest next step when context is incomplete
- Delegate out-of-scope work through the orchestrator when another specialist is needed

You cannot:
- Execute high-risk actions without the orchestrator's approval flow
- Claim external side effects happened unless receipts prove it
- Take over another specialist's domain without stating the handoff to {example_other_agent}

## Deep Domain Knowledge — {spec.domain.replace('_', ' ').title()}

Bring domain-specific judgment, timing, terminology, and common failure modes into every answer.
Keep recommendations practical and production-minded rather than generic.

## Team Delegation
You work with other specialists when the request touches their domain:
- Ava for orchestration and cross-functional coordination
- The relevant domain specialist when the request crosses into another regulated or operational lane

## Response Rules
- Keep responses compact unless the user explicitly asks for detail
- State assumptions when the request is missing key context
- Never present draft output as completed execution

## Memory
- Remember stable workflow preferences and recurring facts for {spec.domain.replace('_', ' ')}
- Use past context when it materially improves the answer
- Never talk about memory mechanics directly

## Governance Awareness
- GREEN actions are read-only or informational
- YELLOW actions require explicit confirmation before external impact
- RED actions require explicit authority and should fail closed on ambiguity
- Every state-changing action produces an auditable receipt

## Output Discipline (GPT-5.2)
- Stay inside the skill pack's domain
- Avoid filler, repetition, and generic assistant language
- Answer the question, then stop
"""


def render_manifest(spec: ScaffoldSpec, persona_filename: str) -> str:
    tools = [tool_id_for_action(spec.provider, action) for action in spec.actions]
    manifest = {
        "skillpack_id": spec.manifest_id,
        "name": f"{spec.agent_name} {camelize(spec.domain)}",
        "agent_name": spec.agent_name,
        "registry_id": spec.registry_id,
        "owner_key": spec.owner_key,
        "persona_filename": persona_filename,
        "channel": "internal_frontend",
        "version": "1.0.0",
        "description": f"{spec.description}. Default risk tier {infer_max_risk(spec.actions)}.",
        "capabilities": [f"can_{action.split('.', 1)[1].replace('.', '_')}" for action in spec.actions],
        "risk_profile": {
            "default_risk_tier": infer_max_risk(spec.actions),
            "max_risk_tier": infer_max_risk(spec.actions),
        },
        "tools": tools,
        "providers": [spec.provider],
        "actions": spec.actions,
        "certification_status": "uncertified",
        "outputs": [f"{spec.domain}_{action.split('.', 1)[1].replace('.', '_')}_result" for action in spec.actions],
        "dependencies": [],
        "memory": {
            "enabled": spec.memory_enabled,
            "fact_types": ["preference", "business_fact", "workflow"],
            "default_keys": [],
        },
        "prompt_defaults": {
            "persona_file": persona_filename,
            "tone": spec.tone,
            "style": spec.prompt_style,
            "guardrails": [
                "state assumptions explicitly",
                "never claim execution without receipts",
                "stay inside the assigned domain",
            ],
        },
        "observability": {
            "tags": [spec.registry_id, spec.domain, *spec.observability_tags],
            "alerts": choose_alerts(spec.actions, spec.memory_enabled),
            "metrics": ["response_quality_score", "receipts_emitted", "policy_denials", "llm_latency_ms"],
            "expected_receipt_events": spec.actions,
        },
        "scaffold_metadata": {
            "generated_by": "scripts/scaffold_agent.py",
            "preset": spec.preset_name,
            "registry_id": spec.registry_id,
        },
    }
    return json.dumps(manifest, indent=2) + "\n"


def render_risk_policy(spec: ScaffoldSpec) -> str:
    lines = [
        f"# {spec.agent_name} {camelize(spec.domain)} — Risk Policy",
        f"# Generated by scripts/scaffold_agent.py for {spec.description}",
        "",
        f"pack_id: {spec.manifest_id}",
        f"default_risk_tier: {infer_max_risk(spec.actions)}",
        f"max_risk_tier: {infer_max_risk(spec.actions)}",
        "",
        "actions:",
    ]
    for action in spec.actions:
        risk = infer_action_risk(action)
        approval = "true" if risk != "green" else "false"
        lines.extend(
            [
                f'  "{action}":',
                f"    risk_tier: {risk}",
                f"    approval_required: {approval}",
                f'    description: "{action} action for {spec.domain.replace("_", " ")}"',
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_tool_policy(spec: ScaffoldSpec) -> str:
    lines = [
        f"# {spec.agent_name} {camelize(spec.domain)} — Tool Policy",
        "tool_policy:",
    ]
    for action in spec.actions:
        lines.extend(
            [
                f'  "{action}":',
                f"    tool_id: {tool_id_for_action(spec.provider, action)}",
                f"    allowed: true",
                f"    risk_tier: {infer_action_risk(action)}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_autonomy_policy(spec: ScaffoldSpec) -> str:
    lines = [
        f"# {spec.agent_name} {camelize(spec.domain)} — Autonomy Policy",
        f"default_mode: {'memory-assisted' if spec.memory_enabled else 'stateless'}",
        "actions:",
    ]
    for action in spec.actions:
        risk = infer_action_risk(action)
        lines.extend(
            [
                f'  "{action}":',
                f"    autonomy: {'guided' if risk == 'green' else 'approval-gated'}",
                f"    binding_fields: [{', '.join(binding_fields_for_action(action))}]",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def render_observability_policy(spec: ScaffoldSpec) -> str:
    alerts = choose_alerts(spec.actions, spec.memory_enabled)
    lines = [
        f"# {spec.agent_name} {camelize(spec.domain)} ??? Observability Policy",
        f"registry_id: {spec.registry_id}",
        f"tags: [{', '.join([spec.registry_id, spec.domain, *spec.observability_tags])}]",
        "alerts:",
    ]
    for alert in alerts:
        lines.append(f"  - {alert}")
    lines.extend(
        [
            "metrics:",
            "  - response_quality_score",
            "  - receipts_emitted",
            "  - policy_denials",
            "  - llm_latency_ms",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def render_prompt_contract(spec: ScaffoldSpec, persona_filename: str) -> str:
    actions = "\n".join(f"- {action}" for action in spec.actions)
    return f"""# Prompt Contract

## Agent
- Name: {spec.agent_name}
- Registry ID: {spec.registry_id}
- Persona file: {persona_filename}
- Preset: {spec.preset_name or "custom"}
- Prompt style: {spec.prompt_style}

## Guardrails
- Stay within {spec.domain.replace('_', ' ')} workflows.
- Ask for missing context before attempting non-read actions.
- Never claim execution without a receipt.

## Supported Actions
{actions}

## Output Requirements
- Keep answers concise and operational.
- Separate facts, assumptions, and next step.
- Respect the risk tier for the requested action.
"""


def render_test(spec: ScaffoldSpec) -> str:
    class_name = f"{camelize(spec.agent_name)}{camelize(spec.domain)}SkillPack"
    module_name = spec.registry_id
    first_action = spec.actions[0]
    method_name = first_action.replace(".", "_")
    return f'''"""Generated scaffold tests for {spec.agent_name}."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aspire_orchestrator.services.agent_sdk_base import AgentContext
from aspire_orchestrator.skillpacks.{module_name} import {class_name}


@pytest.fixture
def pack() -> {class_name}:
    return {class_name}()


@pytest.fixture
def ctx() -> AgentContext:
    return AgentContext(
        suite_id="test-suite-001",
        office_id="test-office-001",
        correlation_id="test-corr-001",
        actor_id="test-user-001",
        risk_tier="{infer_max_risk(spec.actions)}",
    )


@pytest.mark.asyncio
async def test_dispatch_known_action(pack: {class_name}, ctx: AgentContext):
    with patch.object(pack, "{method_name}", AsyncMock()) as handler:
        handler.return_value.success = True
        await pack.dispatch_action("{first_action}", {{"query": "test"}}, ctx)
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_dispatch_unknown_action_denied(pack: {class_name}, ctx: AgentContext):
    result = await pack.dispatch_action("unknown.action", {{}}, ctx)
    assert not result.success
    assert result.receipt["policy"]["decision"] == "deny"
'''


def tool_id_for_action(provider: str, action: str) -> str:
    domain, verb = action.split(".", 1)
    return f"{provider}.{domain}.{verb.replace('.', '_')}"


def registry_block(spec: ScaffoldSpec) -> str:
    tools = ", ".join(tool_id_for_action(spec.provider, action) for action in spec.actions)
    scopes = ", ".join(action.replace(".", ":") for action in spec.actions)
    actions = ", ".join(spec.actions)
    return (
        f"  {spec.registry_id}:\n"
        f"    id: {spec.registry_id}\n"
        f'    name: "{spec.agent_name} ({camelize(spec.domain)})"\n'
        f"    owner: {spec.owner_key}\n"
        f"    category: {spec.category}\n"
        f"    risk_tier: {infer_max_risk(spec.actions)}\n"
        f"    status: registered\n"
        f'    description: "{spec.description}"\n'
        f"    actions: [{actions}]\n"
        f"    providers: [{spec.provider}]\n"
        f"    capability_scopes: [{scopes}]\n"
        f"    tools: [{tools}]\n"
        f"    per_suite_enabled: true\n\n"
    )


def policy_matrix_block(spec: ScaffoldSpec) -> str:
    lines = [
        "",
        f"  # Generated scaffold for {spec.registry_id}",
    ]
    for action in spec.actions:
        risk = infer_action_risk(action)
        approval_type = "none" if risk == "green" else "explicit"
        required_fields = ", ".join(binding_fields_for_action(action))
        lines.extend(
            [
                f"  {action}:",
                f"    risk_tier: {risk}",
                f"    tools: [{tool_id_for_action(spec.provider, action)}]",
                f"    capability_scope: {action.replace('.', ':')}",
                f"    category: {spec.category}",
                f"    approval:",
                f"      type: {approval_type}",
                f"      binding_fields: [{required_fields}]",
                f"    params:",
                f"      required: [{required_fields}]",
                f"    redact_fields: []",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def insert_registry_entry(path: Path, spec: ScaffoldSpec) -> None:
    text = path.read_text(encoding="utf-8")
    if f"  {spec.registry_id}:" in text:
        raise ValueError(f"Registry entry already exists: {spec.registry_id}")
    marker = "\ntools:\n"
    if marker not in text:
        raise ValueError("Could not find tools section marker in skill_pack_manifests.yaml")
    text = text.replace(marker, "\n" + registry_block(spec) + "tools:\n", 1)
    path.write_text(text, encoding="utf-8")


def append_policy_entries(path: Path, spec: ScaffoldSpec) -> None:
    text = path.read_text(encoding="utf-8")
    for action in spec.actions:
        if re.search(rf"^\s+{re.escape(action)}:\s*$", text, flags=re.MULTILINE):
            raise ValueError(f"Policy matrix entry already exists: {action}")
    text = text.rstrip() + policy_matrix_block(spec)
    path.write_text(text + "\n", encoding="utf-8")


def insert_persona_map_entry(path: Path, spec: ScaffoldSpec, persona_filename: str) -> None:
    text = path.read_text(encoding="utf-8")
    if f'"{spec.owner_key}": "{persona_filename}"' in text:
        return
    # Try agent_identity.py AGENT_PERSONA_MAP format first
    marker = "}\n\n# Agent display names"
    if marker not in text:
        # Fallback: original agent_reason.py format
        marker = "}\n\n_PERSONAS_DIR"
    if marker not in text:
        raise ValueError("Could not find AGENT_PERSONA_MAP terminator in agent_identity.py")
    insertion = f'    "{spec.owner_key}": "{persona_filename}",\n'
    text = text.replace(marker, insertion + marker[0] + marker[1:], 1)
    path.write_text(text, encoding="utf-8")


def ensure_absent(path: Path) -> None:
    if path.exists():
        raise ValueError(f"Refusing to overwrite existing file: {path}")


def required_paths_for_target(root: Path, target: ValidationTarget) -> dict[str, Path]:
    policy_dir = root / "src" / "aspire_orchestrator" / "config" / "pack_policies" / target.registry_id
    return {
        "module": root / "src" / "aspire_orchestrator" / "skillpacks" / f"{target.registry_id}.py",
        "manifest": root / "src" / "aspire_orchestrator" / "config" / "pack_manifests" / f"{target.manifest_id}.json",
        "persona": root / "src" / "aspire_orchestrator" / "config" / "pack_personas" / target.persona_filename,
        "risk_policy": policy_dir / "risk_policy.yaml",
        "tool_policy": policy_dir / "tool_policy.yaml",
        "autonomy_policy": policy_dir / "autonomy_policy.yaml",
        "observability_policy": policy_dir / "observability_policy.yaml",
        "prompt_contract": policy_dir / "prompt_contract.md",
        "test": root / "tests" / f"test_{target.registry_id}.py",
    }


def collect_validation_targets(root: Path) -> list[ValidationTarget]:
    manifests_dir = root / "src" / "aspire_orchestrator" / "config" / "pack_manifests"
    if not manifests_dir.exists():
        raise ValueError(f"Manifest directory not found: {manifests_dir}")

    targets: list[ValidationTarget] = []
    for manifest_path in sorted(manifests_dir.glob("*.json")):
        if manifest_path.name == "manifest_schema.json":
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        agent_name = str(manifest.get("agent_name", "")).strip()
        actions = manifest.get("actions")
        if not agent_name or not isinstance(actions, list) or not actions:
            raise ValueError(f"Invalid manifest contents: {manifest_path}")
        registry_id = snakeify(str(manifest.get("registry_id") or manifest_path.stem).strip() or manifest_path.stem)
        owner_key = snakeify(str(manifest.get("owner_key") or agent_name))
        persona_filename = str(manifest.get("persona_filename") or f"{registry_id}_system_prompt.md")
        targets.append(
            ValidationTarget(
                registry_id=registry_id,
                owner_key=owner_key,
                manifest_id=manifest_path.stem,
                persona_filename=persona_filename,
                manifest=manifest,
            )
        )
    return targets


def run_validate(root: Path) -> tuple[bool, list[str]]:
    failures: list[str] = []
    for target in collect_validation_targets(root):
        errors = validate_target(root, target)
        failures.extend(f"{target.registry_id}: {error}" for error in errors)
    return (len(failures) == 0, failures)


def validate_manifest_shape(target: ValidationTarget) -> list[str]:
    errors: list[str] = []
    manifest = target.manifest
    required_keys = ["skillpack_id", "agent_name", "tools", "providers", "actions", "memory", "risk_profile"]
    for key in required_keys:
        if key not in manifest:
            errors.append(f"manifest missing key: {key}")
    if manifest.get("skillpack_id") != target.manifest_id:
        errors.append("manifest skillpack_id does not match filename")
    actions = manifest.get("actions")
    if not isinstance(actions, list) or not actions:
        errors.append("manifest actions must be a non-empty list")
    else:
        if any(not isinstance(action, str) or "." not in action for action in actions):
            errors.append("manifest actions must be dotted strings")
    memory = manifest.get("memory")
    if not isinstance(memory, dict) or "enabled" not in memory:
        errors.append("manifest memory block is invalid")
    return errors


def validate_target(root: Path, target: ValidationTarget) -> list[str]:
    errors = validate_manifest_shape(target)
    paths = required_paths_for_target(root, target)
    for label, file_path in paths.items():
        if not file_path.exists():
            errors.append(f"missing {label}: {file_path.relative_to(root)}")

    if errors:
        return errors

    module_text = paths["module"].read_text(encoding="utf-8")
    persona_text = paths["persona"].read_text(encoding="utf-8")
    risk_text = paths["risk_policy"].read_text(encoding="utf-8")
    tool_text = paths["tool_policy"].read_text(encoding="utf-8")
    autonomy_text = paths["autonomy_policy"].read_text(encoding="utf-8")
    observability_text = paths["observability_policy"].read_text(encoding="utf-8")
    contract_text = paths["prompt_contract"].read_text(encoding="utf-8")
    action_list = target.manifest["actions"]

    if target.registry_id not in module_text:
        errors.append("module does not reference registry id")
    if target.persona_filename not in persona_text:
        errors.append("persona file does not reference its filename")
    for action in action_list:
        if action not in risk_text:
            errors.append(f"risk policy missing action: {action}")
        if action not in tool_text:
            errors.append(f"tool policy missing action: {action}")
        if action not in autonomy_text:
            errors.append(f"autonomy policy missing action: {action}")
        if action not in contract_text:
            errors.append(f"prompt contract missing action: {action}")

    if "alerts:" not in observability_text:
        errors.append("observability policy missing alerts section")
    if "Prompt style:" not in persona_text:
        errors.append("persona missing prompt style line")
    return errors


def scaffold_agent(root: Path, spec: ScaffoldSpec) -> list[Path]:
    created: list[Path] = []
    persona_filename = f"{spec.registry_id}_system_prompt.md"
    module_path = root / "src" / "aspire_orchestrator" / "skillpacks" / f"{spec.registry_id}.py"
    manifest_path = root / "src" / "aspire_orchestrator" / "config" / "pack_manifests" / f"{spec.manifest_id}.json"
    persona_path = root / "src" / "aspire_orchestrator" / "config" / "pack_personas" / persona_filename
    policy_dir = root / "src" / "aspire_orchestrator" / "config" / "pack_policies" / spec.registry_id
    risk_policy_path = policy_dir / "risk_policy.yaml"
    tool_policy_path = policy_dir / "tool_policy.yaml"
    autonomy_policy_path = policy_dir / "autonomy_policy.yaml"
    observability_policy_path = policy_dir / "observability_policy.yaml"
    prompt_contract_path = policy_dir / "prompt_contract.md"
    test_path = root / "tests" / f"test_{spec.registry_id}.py"
    registry_path = root / "src" / "aspire_orchestrator" / "config" / "skill_pack_manifests.yaml"
    policy_matrix_path = root / "src" / "aspire_orchestrator" / "config" / "policy_matrix.yaml"
    agent_identity_path = root / "src" / "aspire_orchestrator" / "services" / "agent_identity.py"

    for path in (
        module_path,
        manifest_path,
        persona_path,
        risk_policy_path,
        tool_policy_path,
        autonomy_policy_path,
        observability_policy_path,
        prompt_contract_path,
        test_path,
    ):
        ensure_absent(path)

    policy_dir.mkdir(parents=True, exist_ok=True)
    module_path.write_text(render_skillpack(spec), encoding="utf-8")
    manifest_path.write_text(render_manifest(spec, persona_filename), encoding="utf-8")
    persona_path.write_text(render_persona(spec, persona_filename), encoding="utf-8")
    risk_policy_path.write_text(render_risk_policy(spec), encoding="utf-8")
    tool_policy_path.write_text(render_tool_policy(spec), encoding="utf-8")
    autonomy_policy_path.write_text(render_autonomy_policy(spec), encoding="utf-8")
    observability_policy_path.write_text(render_observability_policy(spec), encoding="utf-8")
    prompt_contract_path.write_text(render_prompt_contract(spec, persona_filename), encoding="utf-8")
    test_path.write_text(render_test(spec), encoding="utf-8")

    insert_registry_entry(registry_path, spec)
    append_policy_entries(policy_matrix_path, spec)
    insert_persona_map_entry(agent_identity_path, spec, persona_filename)

    created.extend(
        [
            module_path,
            manifest_path,
            persona_path,
            risk_policy_path,
            tool_policy_path,
            autonomy_policy_path,
            observability_policy_path,
            prompt_contract_path,
            test_path,
        ]
    )
    return created


def build_validation_target(root: Path, args: argparse.Namespace) -> ValidationTarget:
    registry_id = snakeify(args.registry_id)
    manifest_id = slugify(args.manifest_id or registry_id)
    manifest_path = root / "src" / "aspire_orchestrator" / "config" / "pack_manifests" / f"{manifest_id}.json"
    if not manifest_path.exists():
        raise ValueError(f"Manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    owner_key = snakeify(args.owner_key or manifest.get("owner_key") or registry_id.split("_", 1)[0])
    persona_filename = f"{registry_id}_system_prompt.md"
    return ValidationTarget(
        registry_id=registry_id,
        owner_key=owner_key,
        manifest_id=manifest_id,
        persona_filename=persona_filename,
        manifest=manifest,
    )


def validate_agent(root: Path, target: ValidationTarget) -> list[str]:
    problems: list[str] = []
    config_root = root / "src" / "aspire_orchestrator" / "config"
    module_path = root / "src" / "aspire_orchestrator" / "skillpacks" / f"{target.registry_id}.py"
    persona_path = config_root / "pack_personas" / target.persona_filename
    policy_dir = config_root / "pack_policies" / target.registry_id
    registry_path = config_root / "skill_pack_manifests.yaml"
    policy_matrix_path = config_root / "policy_matrix.yaml"
    agent_identity_path = root / "src" / "aspire_orchestrator" / "services" / "agent_identity.py"
    manifest_path = config_root / "pack_manifests" / f"{target.manifest_id}.json"
    test_path = root / "tests" / f"test_{target.registry_id}.py"

    required_files = [
        module_path,
        manifest_path,
        persona_path,
        policy_dir / "risk_policy.yaml",
        policy_dir / "tool_policy.yaml",
        policy_dir / "autonomy_policy.yaml",
        policy_dir / "observability_policy.yaml",
        policy_dir / "prompt_contract.md",
        test_path,
    ]
    for required_path in required_files:
        if not required_path.exists():
            problems.append(f"missing file: {required_path.relative_to(root)}")

    manifest = target.manifest
    actions = manifest.get("actions")
    tools = manifest.get("tools")
    if not isinstance(actions, list) or not actions:
        problems.append("manifest missing non-empty actions list")
        actions = []
    if not isinstance(tools, list) or not tools:
        problems.append("manifest missing non-empty tools list")
        tools = []
    if manifest.get("skillpack_id") != target.manifest_id:
        problems.append("manifest skillpack_id does not match manifest file name")
    if "prompt_defaults" not in manifest:
        problems.append("manifest missing prompt_defaults block")
    if "observability" not in manifest:
        problems.append("manifest missing observability block")

    registry_text = registry_path.read_text(encoding="utf-8")
    if f"  {target.registry_id}:" not in registry_text:
        problems.append("registry missing skill pack entry")

    policy_text = policy_matrix_path.read_text(encoding="utf-8")
    for action in actions:
        if not re.search(rf"^\s+{re.escape(str(action))}:\s*$", policy_text, flags=re.MULTILINE):
            problems.append(f"policy matrix missing action: {action}")

    persona_map_text = agent_identity_path.read_text(encoding="utf-8")
    if f'"{target.owner_key}": "{target.persona_filename}"' not in persona_map_text:
        problems.append("agent_identity persona map missing entry")

    if module_path.exists():
        module_text = module_path.read_text(encoding="utf-8")
        for action in actions:
            method_name = str(action).replace(".", "_")
            if f"async def {method_name}" not in module_text:
                problems.append(f"skill pack missing handler method: {method_name}")

    tool_policy_path = policy_dir / "tool_policy.yaml"
    if tool_policy_path.exists():
        tool_policy_text = tool_policy_path.read_text(encoding="utf-8")
        for tool in tools:
            if str(tool) not in tool_policy_text:
                problems.append(f"tool policy missing tool: {tool}")

    risk_policy_path = policy_dir / "risk_policy.yaml"
    if risk_policy_path.exists():
        risk_policy_text = risk_policy_path.read_text(encoding="utf-8")
        for action in actions:
            if str(action) not in risk_policy_text:
                problems.append(f"risk policy missing action: {action}")

    observability_policy_path = policy_dir / "observability_policy.yaml"
    if observability_policy_path.exists():
        observability_text = observability_policy_path.read_text(encoding="utf-8")
        if target.registry_id not in observability_text:
            problems.append("observability policy missing registry tag")

    return problems


def certify_agent(root: Path, target: ValidationTarget) -> list[str]:
    problems = validate_agent(root, target)
    manifest = target.manifest
    actions = manifest.get("actions") if isinstance(manifest.get("actions"), list) else []
    observability = manifest.get("observability") if isinstance(manifest.get("observability"), dict) else {}
    prompt_defaults = manifest.get("prompt_defaults") if isinstance(manifest.get("prompt_defaults"), dict) else {}
    risk_profile = manifest.get("risk_profile") if isinstance(manifest.get("risk_profile"), dict) else {}

    if not prompt_defaults.get("style"):
        problems.append("certification requires prompt_defaults.style")
    if "response_quality_score" not in str(observability):
        problems.append("certification requires response_quality_score observability wiring")
    if not observability.get("tags"):
        problems.append("certification requires observability tags")
    if risk_profile.get("max_risk_tier") not in {"green", "yellow", "red"}:
        problems.append("certification requires valid max_risk_tier")
    if len(actions) < 1:
        problems.append("certification requires at least one action")
    return problems


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scaffold and validate Aspire agents.")
    subparsers = parser.add_subparsers(dest="command")

    scaffold = subparsers.add_parser("scaffold", help="Generate a new agent scaffold")
    scaffold.add_argument("--agent-name", help="Display name, e.g. Blake")
    scaffold.add_argument("--preset", choices=sorted(PRESETS), help="Apply a canned domain preset")
    scaffold.add_argument("--role-title", help="Role title, e.g. Banking Specialist")
    scaffold.add_argument("--domain", help="Domain slug, e.g. banking")
    scaffold.add_argument("--actions", help="Comma-separated verbs or fully qualified actions")
    scaffold.add_argument("--owner-key", help="Owner key used in persona routing")
    scaffold.add_argument("--registry-id", help="Snake_case registry/skillpack module id")
    scaffold.add_argument("--manifest-id", help="Kebab-case pack manifest id")
    scaffold.add_argument("--category", help="Registry category")
    scaffold.add_argument("--provider", help="Provider/tool prefix")
    scaffold.add_argument("--role-description", help="Longer role description for persona")
    scaffold.add_argument("--description", help="Registry/manifest description")
    scaffold.add_argument("--tone", help="Persona tone")
    scaffold.add_argument("--prompt-style", help="Prompt framing style label")
    scaffold.add_argument("--observability-tags", help="Comma-separated dashboard tags")
    scaffold.add_argument("--no-memory", action="store_true", help="Disable agent memory in scaffold")
    scaffold.add_argument("--root", help="Override repository root for testing or alternate worktrees")

    validate = subparsers.add_parser("validate", help="Validate a generated agent scaffold")
    validate.add_argument("--registry-id", help="Snake_case registry/skillpack module id")
    validate.add_argument("--owner-key", help="Owner key used in persona routing")
    validate.add_argument("--manifest-id", help="Kebab-case pack manifest id")
    validate.add_argument("--root", help="Override repository root for testing or alternate worktrees")

    certify = subparsers.add_parser("certify", help="Run enterprise readiness checks for a generated agent")
    certify.add_argument("--registry-id", help="Snake_case registry/skillpack module id")
    certify.add_argument("--owner-key", help="Owner key used in persona routing")
    certify.add_argument("--manifest-id", help="Kebab-case pack manifest id")
    certify.add_argument("--root", help="Override repository root for testing or alternate worktrees")

    subparsers.add_parser("list-presets", help="List available scaffold presets")
    return parser


def coerce_legacy_argv(argv: list[str]) -> list[str]:
    if not argv:
        return ["scaffold"]
    if argv[0] in KNOWN_SUBCOMMANDS:
        return argv
    return ["scaffold", *argv]


def print_preset_table() -> None:
    print("Available presets:")
    for name, preset in sorted(PRESETS.items()):
        actions = ", ".join(preset.actions)
        print(f"  - {name}: {preset.role_title} | domain={preset.domain} | actions={actions}")


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    raw_argv = sys.argv[1:] if argv is None else argv
    args = parser.parse_args(coerce_legacy_argv(raw_argv))
    root = Path(args.root).resolve() if getattr(args, "root", None) else ROOT
    try:
        if args.command == "list-presets":
            print_preset_table()
            return 0

        if args.command == "validate":
            if args.registry_id:
                target = build_validation_target(root, args)
                problems = validate_agent(root, target)
            else:
                ok, failures = run_validate(root)
                if not ok:
                    print("Validation failed:")
                    for problem in failures:
                        print(f"  - {problem}")
                    return 1
                print("Validation passed")
                return 0
            if problems:
                print("Validation failed:")
                for problem in problems:
                    print(f"  - {problem}")
                return 1
            print(f"Validation passed for {target.registry_id}")
            return 0

        if args.command == "certify":
            if not args.registry_id:
                print("error: certify requires --registry-id", file=sys.stderr)
                return 1
            target = build_validation_target(root, args)
            problems = certify_agent(root, target)
            if problems:
                print("Certification failed:")
                for problem in problems:
                    print(f"  - {problem}")
                return 1
            print(f"Certification passed for {target.registry_id}")
            return 0

        spec = build_spec(args)
        created = scaffold_agent(root, spec)
        target = build_validation_target(
            root,
            argparse.Namespace(
                registry_id=spec.registry_id,
                owner_key=spec.owner_key,
                manifest_id=spec.manifest_id,
            ),
        )
        problems = validate_agent(root, target)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print("Scaffolded agent:")
    for generated_path in created:
        print(f"  - {generated_path.relative_to(ROOT)}")
    print("Updated:")
    print("  - src/aspire_orchestrator/config/skill_pack_manifests.yaml")
    print("  - src/aspire_orchestrator/config/policy_matrix.yaml")
    print("  - src/aspire_orchestrator/nodes/agent_reason.py")
    print("Validation:")
    if problems:
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print(f"  - passed for {spec.registry_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
