"""ElevenLabs Agent Contract Validator (Pass 0).

Loads `el_agent_contract_v1.yaml` and validates a prompt + agent config against
all 26 rules.  One method per rule.  Returns a `ContractReport` with per-rule
pass/fail, applied overrides, and a score string (e.g. "26/26").

Law #2: construction emits a `contract_validator_initialized` receipt via the
existing receipt_store service (non-blocking, GREEN tier).
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ContractComplianceError(Exception):
    """Raised when an agent prompt fails the EL contract (Pass 1 enforcement).

    This exception carries the full ContractReport so callers (sync scripts,
    CI gates) can surface the exact failing rules and emit receipts before
    aborting the pipeline.
    """

    def __init__(self, agent_id: str, report: "ContractReport") -> None:
        self.agent_id = agent_id
        self.report = report
        super().__init__(
            f"Agent {agent_id} failed contract: {report.score}, "
            f"failing rules: {[r.id for r in report.failing_rules]}"
        )


# ---------------------------------------------------------------------------
# Dataclasses (strict typing — no Any)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FailedRule:
    """A rule that did not pass validation."""

    id: int
    id_suffix: str  # "" for main rules, "b" / "c" for sub-rules
    name: str
    severity: str  # "HIGH" | "MEDIUM"
    message: str
    evidence: str


@dataclass(frozen=True)
class OverrideRecord:
    """A rule waived via a signed agent-config override block."""

    rule: int
    rule_suffix: str  # "" | "b" | "c"
    reason: str
    approved_by: str
    approved_at: str  # ISO date string from YAML


@dataclass
class ContractReport:
    """Full validation report for one agent."""

    agent_id: str
    agent_kind: str
    score: str  # e.g. "26/26" or "24/26 with 2 overrides"
    total_rules: int
    passing_rules: list[str]  # rule id strings like "1", "12", "12b", "12c"
    failing_rules: list[FailedRule]
    overrides_applied: list[OverrideRecord]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_CONTRACT_PATH = (
    Path(__file__).parent.parent
    / "config"
    / "contracts"
    / "el_agent_contract_v1.yaml"
)

# Known workspace agent display names (rule 14).  Source: MEMORY.md agent IDs.
_WORKSPACE_AGENT_NAMES: list[str] = [
    "Tiffany",
    "Sarah",
    "Ava",
    "Finn",
    "Eli",
    "Nora",
]

# Dynamic variable pattern (rule 15, 16)
_DYN_VAR_RE: re.Pattern[str] = re.compile(r"\{\{([a-z_]+)\}\}")

# Numbered step line pattern (rules 3, 8)
_NUMBERED_STEP_RE: re.Pattern[str] = re.compile(r"^\s*(\d+)\.\s+", re.MULTILINE)

# List item pattern (rule 4)
_LIST_ITEM_RE: re.Pattern[str] = re.compile(r"^\s*[-*]\s+|^\s*\d+\.\s+", re.MULTILINE)

# Heading pattern (rule 1, 9)
_H1_RE: re.Pattern[str] = re.compile(r"^#\s+(.+)", re.MULTILINE)

# Bracketed audio cue pattern (rule 12) — lowercase letters + underscores inside []
_BRACKETED_CUE_RE: re.Pattern[str] = re.compile(r"\[[a-z][a-z_]*\]")

# Banned conciseness phrases (rule 2) — lowercased for matching
_BANNED_PHRASES: tuple[str, ...] = (
    "i'd be happy to assist you with that today",
    "thank you for your patience",
    "i appreciate your inquiry",
    "have a wonderful day",
    "is there anything else i can help you with today",
    "please hold while i",
    "based on my analysis",
    "i am now transferring",
)

# State-changing tool name patterns (rule 22)
_STATE_CHANGING_TOOL_PATTERNS: tuple[str, ...] = (
    "schedule",
    "create",
    "update",
    "delete",
    "send",
    "book",
    "cancel",
    "pay",
)

# Required tool section labels (rule 5)
_TOOL_REQUIRED_LABELS: tuple[str, ...] = (
    "**When to use:**",
    "**Parameters:**",
    "**Error handling:**",
)

_TOOL_EXEMPT_PHRASE: str = "No tools are configured for this agent."

# Default dyn vars that the personalization webhook is guaranteed to provide.
# This set is the canonical registry; rule 16 validates against it.
# Source of truth: routes/sarah.py:_build_dyn_vars and resolve_personalization_by_phone RPC.
# When sarah.py adds/renames a key, this set must be updated in the same PR (CI guard via
# tests/contracts/test_el_contract_compliance_workspace.py::test_default_dyn_vars_match_personalization_payload).
_DEFAULT_DYN_VARS: frozenset[str] = frozenset(
    {
        # Agent identity
        "agent_first_name",
        "agent_id",
        # Business profile
        "business_name",
        "business_city",
        "business_state",
        "business_phone",
        "business_hours",
        "business_address",
        "industry",
        "industry_specialty",
        "timezone",
        "voicemail_email",
        "language",
        "greeting_name_override",
        # Owner identity (used in transfer + addressing)
        "owner_title",
        "owner_salutation",
        "owner_formal_name",
        "owner_first_name",
        "owner_last_name",
        # Caller (matched contact, when known)
        "first_name",
        "last_name",
        "caller_is_known",
        "caller_first_name",
        "caller_company",
        "caller_category",
        "caller_display_name",
        "caller_last_seen_days_ago",
        "caller_total_calls",
        "caller_last_call_summary",
        "caller_history_summary",
        "caller_id_prefix",
        # Time / open-state
        "time_of_day",
        "is_after_hours",
        "is_open_now",
        # Mode flags
        "after_hours_mode",
        "busy_mode",
        "catch_mode",
        "public_number_mode",
        # Routing roster
        "routing_contacts_summary",
        "configured_roles",
        "routing_owner_phone",
        "routing_owner_name",
        "routing_sales_phone",
        "routing_sales_name",
        "routing_support_phone",
        "routing_support_name",
        "routing_billing_phone",
        "routing_billing_name",
        "routing_scheduling_phone",
        "routing_scheduling_name",
        # Trade pack
        "trade_id",
        "trade_primary_term",
        "trade_emergency_keywords",
        "trade_intake_fields_json",
        # Tenancy / call context
        "office_id",
        "suite_id",
        "tenant_id",
        "called_number",
        "call_sid",
        # Webhook control
        "conversation_config_override",
        "pronunciation_override",
        "fallback_reason",
    }
)


def _extract_section(prompt: str, heading: str) -> str:
    """Return the text of a section (from heading to next H1 or end of string)."""
    pattern = re.compile(
        r"^#\s+" + re.escape(heading.lstrip("# ")) + r"\s*$(.+?)(?=^#\s|\Z)",
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    match = pattern.search(prompt)
    return match.group(1).strip() if match else ""


def _has_section(prompt: str, heading: str) -> bool:
    """Return True if the prompt contains the given H1 heading (case-insensitive)."""
    escaped = re.escape(heading.lstrip("# ").strip())
    return bool(re.search(r"^#\s+" + escaped + r"\s*$", prompt, re.MULTILINE | re.IGNORECASE))


def _agent_is_override(overrides: list[dict[str, object]], rule_id: int, suffix: str = "") -> Optional[OverrideRecord]:
    """Return a valid OverrideRecord if rule is overridden in agent config, else None."""
    for entry in overrides:
        entry_rule = entry.get("rule")
        entry_suffix = str(entry.get("rule_suffix", ""))
        if entry_rule != rule_id or entry_suffix != suffix:
            continue
        reason = str(entry.get("reason") or "")
        approved_by = str(entry.get("approved_by") or "")
        approved_at = str(entry.get("approved_at") or "")
        if not approved_by or not approved_at:
            # Missing signatory invalidates the override (rule: unsigned override is rejected)
            return None
        return OverrideRecord(
            rule=rule_id,
            rule_suffix=suffix,
            reason=reason,
            approved_by=approved_by,
            approved_at=approved_at,
        )
    return None


# ---------------------------------------------------------------------------
# ContractValidator
# ---------------------------------------------------------------------------


class ContractValidator:
    """Validates EL agent prompts and configs against el_agent_contract_v1.yaml.

    Usage::

        validator = ContractValidator()
        report = validator.validate(
            prompt_text=my_prompt,
            agent_config=my_config,
            agent_kind="receptionist",
        )
        print(report.score)  # "26/26"
    """

    def __init__(self, contract_path: Path = _CONTRACT_PATH) -> None:
        with contract_path.open("r", encoding="utf-8") as fh:
            self._contract: dict[str, object] = yaml.safe_load(fh)
        self._emit_init_receipt()

    # ------------------------------------------------------------------
    # Receipt (Law #2)
    # ------------------------------------------------------------------

    def _emit_init_receipt(self) -> None:
        """Emit a GREEN-tier receipt for validator construction (Law #2)."""
        try:
            from aspire_orchestrator.services.receipt_store import store_receipts

            store_receipts(
                [
                    {
                        "id": str(uuid.uuid4()),
                        "receipt_type": "contract_validator_initialized",
                        "action_type": "contract_validator_initialized",
                        "risk_tier": "GREEN",
                        "actor_type": "SYSTEM",
                        "actor_id": "el_contract_validator",
                        "outcome": "success",
                        "redacted_inputs": {
                            "contract_version": self._contract.get("contract_version"),
                            "applies_to": self._contract.get("applies_to"),
                        },
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    }
                ]
            )
        except Exception as exc:  # pragma: no cover — receipt store may not be available in CI
            logger.warning("Could not emit contract_validator_initialized receipt: %s", exc)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate(
        self,
        prompt_text: str,
        agent_config: dict[str, object],
        agent_kind: str,
    ) -> ContractReport:
        """Run all 26 rules and return a ContractReport.

        Args:
            prompt_text: The fully rendered system prompt string.
            agent_config: The agent configuration dict (matches EL API shape +
                Aspire extensions such as `model_rationale`, `receipts_emitted`,
                `contract_overrides`).
            agent_kind: One of "receptionist", "front_desk", "inbound_voice",
                "assistant", "advisor", etc.  Drives conditional rules.

        Returns:
            ContractReport with score, per-rule pass/fail, and overrides.
        """
        overrides_raw: list[dict[str, object]] = list(
            agent_config.get("contract_overrides", [])  # type: ignore[arg-type]
        )
        agent_id: str = str(agent_config.get("agent_id", "unknown"))

        passing: list[str] = []
        failing: list[FailedRule] = []
        overrides_applied: list[OverrideRecord] = []

        # All check methods return (passed: bool, evidence: str)
        checks: list[tuple[int, str, str, str, object]] = [
            # (rule_id, rule_suffix, rule_name, severity, check_callable)
            (1, "", "required_section_headings", "HIGH", self._check_rule_1_structure(prompt_text, agent_id)),
            (2, "", "conciseness_no_filler", "MEDIUM", self._check_rule_2_conciseness(prompt_text, agent_id)),
            (3, "", "emphasis_critical_steps", "MEDIUM", self._check_rule_3_emphasis(prompt_text, agent_id)),
            (4, "", "guardrails_section_populated", "HIGH", self._check_rule_4_guardrails(prompt_text, agent_id)),
            (5, "", "tool_description_format", "HIGH", self._check_rule_5_tool_format(prompt_text, agent_id)),
            (6, "", "tool_param_examples", "MEDIUM", self._check_rule_6_tool_param_examples(prompt_text, agent_id)),
            (7, "", "tool_error_handling_instructions", "MEDIUM", self._check_rule_7_tool_error_handling(prompt_text, agent_id)),
            (8, "", "single_goal_max_seven_steps", "MEDIUM", self._check_rule_8_goal_steps(prompt_text, agent_id)),
            (9, "", "markdown_style_sentence_case", "MEDIUM", self._check_rule_9_markdown_style(prompt_text, agent_id)),
            (10, "", "text_normalisation_declared", "MEDIUM", self._check_rule_10_text_normalisation(agent_config, agent_id)),
            (11, "", "model_selection_rationale", "MEDIUM", self._check_rule_11_model_rationale(agent_config, agent_id)),
            (12, "", "no_bracketed_audio_cues_in_prompt_body", "HIGH", self._check_rule_12_no_bracketed_cues(prompt_text, agent_id)),
            (12, "b", "per_tag_description_min_length", "HIGH", self._check_rule_12b_tag_descriptions(agent_config, agent_id)),
            (12, "c", "no_voice_settings_for_v3_conversational", "HIGH", self._check_rule_12c_v3_voice_settings(agent_config, agent_id)),
            (13, "", "no_read_back_phrase", "MEDIUM", self._check_rule_13_no_read_back(prompt_text, agent_id)),
            (14, "", "no_other_agent_name_in_prompt", "HIGH", self._check_rule_14_agent_name_isolation(prompt_text, agent_config, agent_id)),
            (15, "", "first_message_empty_slot_defense", "HIGH", self._check_rule_15_first_message_defense(agent_config, agent_id)),
            (16, "", "dyn_var_coverage", "HIGH", self._check_rule_16_dyn_var_coverage(prompt_text, agent_config, agent_id)),
            (17, "", "webhook_conversation_initiation_enabled", "MEDIUM", self._check_rule_17_webhook(agent_config, agent_kind, agent_id)),
            (18, "", "post_call_webhook_attached", "HIGH", self._check_rule_18_post_call_webhook(agent_config, agent_id)),
            (19, "", "tool_registry_uniqueness", "HIGH", self._check_rule_19_tool_uniqueness(agent_config, agent_id)),
            (20, "", "ai_disclosure_policy", "HIGH", self._check_rule_20_ai_disclosure(prompt_text, agent_id)),
            (21, "", "closing_single_utterance", "MEDIUM", self._check_rule_21_closing(prompt_text, agent_id)),
            (22, "", "identity_verification_for_state_changes", "HIGH", self._check_rule_22_identity_verification(prompt_text, agent_config, agent_id)),
            (23, "", "escalation_path_declared", "HIGH", self._check_rule_23_escalation_path(prompt_text, agent_id)),
            (24, "", "capture_first_for_transfers", "HIGH", self._check_rule_24_capture_first(prompt_text, agent_config, agent_id)),
            (25, "", "receipts_declaration", "HIGH", self._check_rule_25_receipts_declaration(agent_config, agent_id)),
            (26, "", "trade_aware_vocabulary", "HIGH", self._check_rule_26_trade_vocab(prompt_text, agent_config, agent_kind, agent_id)),
        ]

        total_rules = len(checks)

        for rule_id, rule_suffix, rule_name, severity, result in checks:
            passed: bool
            evidence: str
            passed, evidence = result  # type: ignore[misc]

            rule_key = str(rule_id) + rule_suffix  # "12", "12b", "12c"

            if passed:
                passing.append(rule_key)
                continue

            # Check for valid override
            override = _agent_is_override(overrides_raw, rule_id, rule_suffix)
            if override is not None:
                overrides_applied.append(override)
                passing.append(rule_key)
                continue

            failing.append(
                FailedRule(
                    id=rule_id,
                    id_suffix=rule_suffix,
                    name=rule_name,
                    severity=severity,
                    message=f"Agent {agent_id}: rule {rule_key} ({rule_name}) failed.",
                    evidence=evidence,
                )
            )

        pass_count = len(passing)
        override_count = len(overrides_applied)
        score_num = f"{pass_count}/{total_rules}"
        if override_count:
            score = f"{score_num} with {override_count} override(s)"
        else:
            score = score_num

        return ContractReport(
            agent_id=agent_id,
            agent_kind=agent_kind,
            score=score,
            total_rules=total_rules,
            passing_rules=passing,
            failing_rules=failing,
            overrides_applied=overrides_applied,
        )

    # ------------------------------------------------------------------
    # Rule implementations — return (passed: bool, evidence: str)
    # ------------------------------------------------------------------

    def _check_rule_1_structure(self, prompt: str, agent_id: str) -> tuple[bool, str]:
        """Rule 1: required section headings present."""
        heading_groups: list[list[str]] = [
            ["# Personality", "# Persona"],
            ["# Environment"],
            ["# Tone"],
            ["# Goal"],
            ["# Guardrails"],
            ["# Tools"],
            ["# Error handling"],
        ]
        missing: list[str] = []
        for group in heading_groups:
            found = any(_has_section(prompt, h) for h in group)
            if not found:
                missing.append(group[0])
        if not missing:
            return True, ""
        return False, f"missing headings: {missing}"

    def _check_rule_2_conciseness(self, prompt: str, agent_id: str) -> tuple[bool, str]:
        """Rule 2: no line > 200 chars; no banned filler phrases."""
        violations: list[str] = []
        prompt_lower = prompt.lower()
        for phrase in _BANNED_PHRASES:
            if phrase in prompt_lower:
                violations.append(f"banned phrase: '{phrase}'")
        for i, line in enumerate(prompt.splitlines(), 1):
            if len(line) > 200:
                violations.append(f"line {i} exceeds 200 chars ({len(line)} chars)")
        if not violations:
            return True, ""
        return False, "; ".join(violations[:5])  # cap evidence length

    def _check_rule_3_emphasis(self, prompt: str, agent_id: str) -> tuple[bool, str]:
        """Rule 3: every section with a numbered list has 'This step is important.'"""
        # Split into sections at H1 boundaries
        sections = re.split(r"(?m)^#\s+", prompt)
        violations: list[str] = []
        for sec in sections:
            if _NUMBERED_STEP_RE.search(sec) and "This step is important." not in sec:
                heading = sec.splitlines()[0].strip() if sec.splitlines() else "unknown"
                violations.append(f"section '{heading[:40]}' has numbered list but no emphasis phrase")
        if not violations:
            return True, ""
        return False, "; ".join(violations)

    def _check_rule_4_guardrails(self, prompt: str, agent_id: str) -> tuple[bool, str]:
        """Rule 4: Guardrails section non-empty with >= 3 list items."""
        section = _extract_section(prompt, "# Guardrails")
        if not section:
            return False, "# Guardrails section is empty or missing"
        items = _LIST_ITEM_RE.findall(section)
        if len(items) >= 3:
            return True, ""
        return False, f"only {len(items)} list item(s) in Guardrails (minimum 3)"

    def _check_rule_5_tool_format(self, prompt: str, agent_id: str) -> tuple[bool, str]:
        """Rule 5: each tool block in # Tools has the three required labels."""
        section = _extract_section(prompt, "# Tools")
        if not section or _TOOL_EXEMPT_PHRASE in section:
            return True, ""
        missing_labels: list[str] = []
        for label in _TOOL_REQUIRED_LABELS:
            if label not in section:
                missing_labels.append(label)
        if not missing_labels:
            return True, ""
        return False, f"missing tool labels: {missing_labels}"

    def _check_rule_6_tool_param_examples(self, prompt: str, agent_id: str) -> tuple[bool, str]:
        """Rule 6: every **Parameters:** block contains '(e.g., …)'."""
        section = _extract_section(prompt, "# Tools")
        if not section or _TOOL_EXEMPT_PHRASE in section:
            return True, ""
        # Find all **Parameters:** blocks and check each for (e.g.,
        param_blocks = re.split(r"\*\*Parameters:\*\*", section)
        violations: list[str] = []
        for i, block in enumerate(param_blocks[1:], 1):  # skip text before first **Parameters:**
            # Take text until next bold label or end
            block_text = re.split(r"\*\*[A-Z][^*]+:\*\*", block)[0]
            if "(e.g.," not in block_text:
                violations.append(f"Parameters block #{i} missing '(e.g., …)'")
        if not violations:
            return True, ""
        return False, "; ".join(violations)

    def _check_rule_7_tool_error_handling(self, prompt: str, agent_id: str) -> tuple[bool, str]:
        """Rule 7: every **Error handling:** block has substantive content (>=15 chars)."""
        section = _extract_section(prompt, "# Tools")
        if not section or _TOOL_EXEMPT_PHRASE in section:
            return True, ""
        error_blocks = re.split(r"\*\*Error handling:\*\*", section)
        violations: list[str] = []
        banned = {"n/a", "none", "tbd"}
        for i, block in enumerate(error_blocks[1:], 1):
            block_text = re.split(r"\*\*[A-Z][^*]+:\*\*|\n## ", block)[0].strip()
            if len(block_text) < 15 or block_text.lower() in banned:
                violations.append(f"Error handling block #{i} is empty or placeholder")
        if not violations:
            return True, ""
        return False, "; ".join(violations)

    def _check_rule_8_goal_steps(self, prompt: str, agent_id: str) -> tuple[bool, str]:
        """Rule 8: # Goal has at most 7 numbered steps."""
        section = _extract_section(prompt, "# Goal")
        if not section:
            return True, ""  # No Goal section — rule 1 will catch missing heading
        matches = _NUMBERED_STEP_RE.findall(section)
        if matches:
            max_step = max(int(n) for n in matches)
            if max_step > 7:
                return False, f"Goal section has {max_step} numbered steps (max 7)"
        return True, ""

    def _check_rule_9_markdown_style(self, prompt: str, agent_id: str) -> tuple[bool, str]:
        """Rule 9: H1 headings use sentence case."""
        # Exempt specific headings that are proper-noun or short by design
        exempt = {"tools", "goal", "tone", "environment", "guardrails", "persona", "personality"}
        violations: list[str] = []
        for match in _H1_RE.finditer(prompt):
            heading_text = match.group(1).strip()
            words = heading_text.split()
            if not words:
                continue
            heading_lower = heading_text.lower()
            if heading_lower in exempt:
                continue
            # Check if NOT sentence case: first word capitalized is fine;
            # subsequent non-proper words all-caps or title-cased is a violation.
            # Simple heuristic: if every word is capitalized and len > 1 word — Title Case fail.
            if len(words) > 1:
                all_capitalized = all(w[0].isupper() for w in words if len(w) > 3)
                if all_capitalized:
                    violations.append(f"heading '{heading_text}' appears to be Title Case")
        if not violations:
            return True, ""
        return False, "; ".join(violations)

    def _check_rule_10_text_normalisation(
        self, agent_config: dict[str, object], agent_id: str
    ) -> tuple[bool, str]:
        """Rule 10: agent config declares text_normalisation_type."""
        allowed = {"system_prompt", "elevenlabs"}
        value = agent_config.get("text_normalisation_type")
        if value in allowed:
            return True, ""
        return False, f"text_normalisation_type={value!r} (expected one of {allowed})"

    def _check_rule_11_model_rationale(
        self, agent_config: dict[str, object], agent_id: str
    ) -> tuple[bool, str]:
        """Rule 11: model_rationale is present and >= 20 chars."""
        value = str(agent_config.get("model_rationale") or "")
        if len(value) >= 20:
            return True, ""
        return False, f"model_rationale length={len(value)} (min 20 chars)"

    def _check_rule_12_no_bracketed_cues(
        self, prompt: str, agent_id: str
    ) -> tuple[bool, str]:
        """Rule 12: no [word] bracketed audio cues in prompt body."""
        matches = _BRACKETED_CUE_RE.findall(prompt)
        if not matches:
            return True, ""
        return False, f"bracketed cues found: {matches[:5]}"

    def _check_rule_12b_tag_descriptions(
        self, agent_config: dict[str, object], agent_id: str
    ) -> tuple[bool, str]:
        """Rule 12b: every suggested_audio_tag has description >= 20 chars."""
        voice: dict[str, object] = agent_config.get("voice", {})  # type: ignore[assignment]
        tags: list[dict[str, object]] = voice.get("suggested_audio_tags", [])  # type: ignore[assignment]
        if not tags:
            return True, ""  # No tags configured — not a violation
        violations: list[str] = []
        for tag in tags:
            tag_name = str(tag.get("name") or tag.get("tag") or "unknown")
            description = str(tag.get("description") or "")
            if len(description) < 20:
                violations.append(f"tag '{tag_name}' description length={len(description)} (min 20)")
        if not violations:
            return True, ""
        return False, "; ".join(violations)

    def _check_rule_12c_v3_voice_settings(
        self, agent_config: dict[str, object], agent_id: str
    ) -> tuple[bool, str]:
        """Rule 12c: v3_conversational agents must not set stability/speed/similarity/style/use_speaker_boost."""
        voice: dict[str, object] = agent_config.get("voice", {})  # type: ignore[assignment]
        model_family = str(voice.get("model_family") or "").lower()
        if model_family != "v3_conversational":
            return True, ""  # Rule only applies to v3
        forbidden = ("stability", "similarity_boost", "speed", "style", "use_speaker_boost")
        found: list[str] = [f for f in forbidden if voice.get(f) is not None]
        if not found:
            return True, ""
        return False, f"v3_conversational has forbidden voice settings: {found}"

    def _check_rule_13_no_read_back(
        self, prompt: str, agent_id: str
    ) -> tuple[bool, str]:
        """Rule 13: no 'read back' / 'read it back' / 'read that back'."""
        pattern = re.compile(r"read\s+(back|it\s+back|that\s+back)", re.IGNORECASE)
        matches = pattern.findall(prompt)
        if not matches:
            return True, ""
        return False, f"'read back' phrase found {len(matches)} time(s)"

    def _check_rule_14_agent_name_isolation(
        self, prompt: str, agent_config: dict[str, object], agent_id: str
    ) -> tuple[bool, str]:
        """Rule 14: other agents' names must not appear in this agent's prompt."""
        this_agent_name = str(agent_config.get("display_name") or agent_config.get("name") or "")
        violations: list[str] = []
        for name in _WORKSPACE_AGENT_NAMES:
            if name == this_agent_name:
                continue
            if re.search(r"\b" + re.escape(name) + r"\b", prompt):
                violations.append(name)
        if not violations:
            return True, ""
        return False, f"other agent names found in prompt: {violations}"

    def _check_rule_15_first_message_defense(
        self, agent_config: dict[str, object], agent_id: str
    ) -> tuple[bool, str]:
        """Rule 15: all {{vars}} in first_message_template have fallbacks in _DEFAULT_DYN_VARS."""
        first_msg = str(agent_config.get("first_message_template") or agent_config.get("first_message") or "")
        if not first_msg:
            return True, ""  # No first message template — not a violation
        vars_in_template: set[str] = set(_DYN_VAR_RE.findall(first_msg))
        unregistered = vars_in_template - _DEFAULT_DYN_VARS
        if not unregistered:
            return True, ""
        return False, f"first_message vars without fallback: {sorted(unregistered)}"

    def _check_rule_16_dyn_var_coverage(
        self, prompt: str, agent_config: dict[str, object], agent_id: str
    ) -> tuple[bool, str]:
        """Rule 16: all {{vars}} in prompt and first_message are in _DEFAULT_DYN_VARS."""
        first_msg = str(agent_config.get("first_message_template") or agent_config.get("first_message") or "")
        combined = prompt + "\n" + first_msg
        vars_found: set[str] = set(_DYN_VAR_RE.findall(combined))
        unregistered = vars_found - _DEFAULT_DYN_VARS
        if not unregistered:
            return True, ""
        return False, f"dynamic variable(s) not in _DEFAULT_DYN_VARS: {sorted(unregistered)}"

    def _check_rule_17_webhook(
        self, agent_config: dict[str, object], agent_kind: str, agent_id: str
    ) -> tuple[bool, str]:
        """Rule 17: receptionist/front_desk/inbound_voice agents need initiation webhook enabled."""
        applicable_kinds = {"receptionist", "front_desk", "inbound_voice"}
        if agent_kind not in applicable_kinds:
            return True, ""  # Not applicable
        value = agent_config.get("enable_conversation_initiation_client_data_from_webhook")
        if value is True:
            return True, ""
        return False, f"enable_conversation_initiation_client_data_from_webhook={value!r} (expected True)"

    def _check_rule_18_post_call_webhook(
        self, agent_config: dict[str, object], agent_id: str
    ) -> tuple[bool, str]:
        """Rule 18: post_call_webhook_id must be present and non-empty."""
        value = str(agent_config.get("post_call_webhook_id") or "")
        if value:
            return True, ""
        return False, "post_call_webhook_id is missing or empty"

    def _check_rule_19_tool_uniqueness(
        self, agent_config: dict[str, object], agent_id: str
    ) -> tuple[bool, str]:
        """Rule 19: no duplicate tool names; transfer_to_number <= 1."""
        tools: list[dict[str, object]] = agent_config.get("tools", [])  # type: ignore[assignment]
        names: list[str] = [str(t.get("name") or "") for t in tools]
        seen: dict[str, int] = {}
        violations: list[str] = []
        for name in names:
            seen[name] = seen.get(name, 0) + 1
        for name, count in seen.items():
            if count > 1:
                violations.append(f"'{name}' appears {count} times")
        if not violations:
            return True, ""
        return False, f"duplicate tool names: {violations}"

    def _check_rule_20_ai_disclosure(self, prompt: str, agent_id: str) -> tuple[bool, str]:
        """Rule 20: prompt has AI disclosure 'only when asked' rule."""
        ai_keywords = re.compile(r"\b(AI|artificial intelligence|not a human|language model)\b", re.IGNORECASE)
        only_when_asked = re.compile(
            r"(only\s+when\s+asked|when\s+directly\s+asked|if\s+(a\s+caller|someone|they)\s+ask)",
            re.IGNORECASE,
        )
        if ai_keywords.search(prompt) and only_when_asked.search(prompt):
            return True, ""
        if not ai_keywords.search(prompt):
            return False, "prompt has no AI disclosure statement"
        return False, "AI disclosure exists but is not scoped to 'only when asked'"

    def _check_rule_21_closing(self, prompt: str, agent_id: str) -> tuple[bool, str]:
        """Rule 21: prompt instructs agent to say closing once and stop."""
        pattern = re.compile(
            r"(say.{0,30}once|do\s+not\s+(speak|continue|say).{0,30}after)",
            re.IGNORECASE,
        )
        context_kw = re.compile(r"(closing|goodbye|end of call|hang\s+up)", re.IGNORECASE)
        if pattern.search(prompt) and context_kw.search(prompt):
            return True, ""
        if not context_kw.search(prompt):
            return False, "no closing/goodbye context found in prompt"
        return False, "closing context found but no 'say once / do not speak after' instruction"

    def _check_rule_22_identity_verification(
        self, prompt: str, agent_config: dict[str, object], agent_id: str
    ) -> tuple[bool, str]:
        """Rule 22: agents with state-changing tools must have identity verification step."""
        tools: list[dict[str, object]] = agent_config.get("tools", [])  # type: ignore[assignment]
        tool_names = [str(t.get("name") or "").lower() for t in tools]
        has_state_change = any(
            any(pat in name for pat in _STATE_CHANGING_TOOL_PATTERNS)
            for name in tool_names
        )
        if not has_state_change:
            return True, ""  # Exempt — no state-changing tools
        verif_pattern = re.compile(
            r"(verif|confirm|identif).{0,50}(caller|identity|name|number)",
            re.IGNORECASE,
        )
        if verif_pattern.search(prompt):
            return True, ""
        return False, "agent has state-changing tools but no identity verification step in prompt"

    def _check_rule_23_escalation_path(self, prompt: str, agent_id: str) -> tuple[bool, str]:
        """Rule 23: prompt declares fallback escalation path."""
        guardrails = _extract_section(prompt, "# Guardrails")
        error_handling = _extract_section(prompt, "# Error handling")
        combined = guardrails + "\n" + error_handling
        pattern = re.compile(
            r"(escalat|transfer|human|message.{0,20}captur|voicemail|callback)",
            re.IGNORECASE,
        )
        if pattern.search(combined):
            return True, ""
        return False, "no escalation path declared in Guardrails or Error handling sections"

    def _check_rule_24_capture_first(
        self, prompt: str, agent_config: dict[str, object], agent_id: str
    ) -> tuple[bool, str]:
        """Rule 24: if transfer_to_number tool present, prompt has capture-first rule."""
        tools: list[dict[str, object]] = agent_config.get("tools", [])  # type: ignore[assignment]
        tool_names = [str(t.get("name") or "").lower() for t in tools]
        if "transfer_to_number" not in tool_names:
            return True, ""  # Not applicable
        pattern = re.compile(
            r"(captur|collect|get|confirm).{0,80}(before.{0,30}transfer|prior.{0,30}transfer)",
            re.IGNORECASE,
        )
        if pattern.search(prompt):
            return True, ""
        return False, "transfer_to_number tool present but no capture-first rule in prompt"

    def _check_rule_25_receipts_declaration(
        self, agent_config: dict[str, object], agent_id: str
    ) -> tuple[bool, str]:
        """Rule 25: agent config has receipts_emitted array."""
        tools: list[dict[str, object]] = agent_config.get("tools", [])  # type: ignore[assignment]
        tool_names = [str(t.get("name") or "").lower() for t in tools]
        has_state_change = any(
            any(pat in name for pat in _STATE_CHANGING_TOOL_PATTERNS)
            for name in tool_names
        )
        receipts = agent_config.get("receipts_emitted")
        if not has_state_change:
            # Agent with no state-changing tools — receipts_emitted must exist (even if empty list)
            if receipts is not None:
                return True, ""
            return False, "receipts_emitted field missing (even no-op agents must declare it)"
        # Agent with state-changing tools — receipts_emitted must be non-empty list
        if isinstance(receipts, list) and len(receipts) >= 1:
            return True, ""
        return False, f"receipts_emitted missing or empty for agent with state-changing tools (value={receipts!r})"

    def _check_rule_26_trade_vocab(
        self,
        prompt: str,
        agent_config: dict[str, object],
        agent_kind: str,
        agent_id: str,
    ) -> tuple[bool, str]:
        """Rule 26: receptionist/front_desk agents must reference {{industry}} and {{industry_specialty}}."""
        applicable_kinds = {"receptionist", "front_desk"}
        if agent_kind not in applicable_kinds:
            return True, ""  # Not applicable
        missing_vars: list[str] = []
        if "{{industry}}" not in prompt:
            missing_vars.append("{{industry}}")
        if "{{industry_specialty}}" not in prompt:
            missing_vars.append("{{industry_specialty}}")
        if missing_vars:
            return False, f"missing required trade variables: {missing_vars}"
        # Also check for banned hardcoded verticals used as substitutes
        banned_verticals = ("HVAC", "electrician", "plumber", "specialty remodeler", "specialty_remodeler")
        found_banned: list[str] = [v for v in banned_verticals if v in prompt]
        if found_banned:
            return False, f"hardcoded vertical name(s) found (use {{industry}} instead): {found_banned}"
        return True, ""
