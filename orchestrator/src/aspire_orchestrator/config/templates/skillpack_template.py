"""{AgentName} Skill Pack — {Brief description}.

All operations are {GREEN/YELLOW} tier. Uses {provider} for {domain}.

Agentic capabilities:
  - 3-tier memory: working (in-context), episodic (cross-session), semantic (learned facts)
  - Multi-step reasoning: plan -> execute -> reflect loop (bounded by orchestrator)
  - Autonomous memory decisions: agent decides what to store/retrieve

Law compliance:
  - Law #1: {AgentName} proposes, orchestrator decides. Multi-step loop is bounded.
  - Law #2: Every operation AND every memory write emits a receipt
  - Law #3: Missing params/tokens = fail closed
  - Law #6: All memory scoped by (suite_id, agent_id) — zero cross-tenant leakage
  - Law #7: Tools execute, never decide. Agent reasons within orchestrator-set bounds.
"""

from __future__ import annotations

import logging
from typing import Any

from aspire_orchestrator.config.templates.agent_memory_mixin import AgentMemoryMixin
from aspire_orchestrator.services.agent_sdk_base import AgentContext, AgentResult
from aspire_orchestrator.skillpacks.base_skill_pack import EnhancedSkillPack

logger = logging.getLogger(__name__)


class AgenticSkillPack(EnhancedSkillPack, AgentMemoryMixin):
    """Base class for agentic agents with memory + multi-step reasoning.

    Extends EnhancedSkillPack with:
      - AgentMemoryMixin: 3-tier memory (working, episodic, semantic)
      - run_agentic_loop(): Bounded multi-step plan->execute->reflect cycle
      - Automatic memory context injection into LLM calls

    New agents should subclass THIS instead of EnhancedSkillPack directly.
    """

    def __init__(
        self,
        agent_id: str,
        agent_name: str,
        *,
        default_risk_tier: str = "green",
        trust_spine: Any | None = None,
        auto_load_config: bool = True,
        memory_enabled: bool = True,
    ) -> None:
        super().__init__(
            agent_id=agent_id,
            agent_name=agent_name,
            default_risk_tier=default_risk_tier,
            trust_spine=trust_spine,
            auto_load_config=auto_load_config,
        )
        self._memory_enabled = memory_enabled
        self.__init_memory__()

    async def get_greeting(
        self,
        ctx: AgentContext,
        *,
        user_name: str | None = None,
        time_of_day: str | None = None,
    ) -> str:
        """Generate context-aware greeting (7a).

        Uses semantic memory to determine if this is a first-time or returning
        interaction. Subclasses can override for custom personality.

        Args:
            ctx: Agent context with suite_id for memory lookup
            user_name: User's display name (e.g., "Mr. Scott")
            time_of_day: "morning", "afternoon", or "evening" (auto-detected if None)

        Returns:
            Personalized greeting string
        """
        if time_of_day is None:
            from datetime import datetime, timezone
            hour = datetime.now(timezone.utc).hour
            if hour < 12:
                time_of_day = "morning"
            elif hour < 17:
                time_of_day = "afternoon"
            else:
                time_of_day = "evening"

        name_part = f" {user_name}" if user_name else ""
        time_part = f"Good {time_of_day}"

        # Check memory for prior interactions
        is_returning = False
        if self._memory_enabled:
            try:
                episodes = await self.recall_episodes(ctx, limit=1)
                is_returning = bool(episodes)
            except Exception:
                pass  # Memory failure is non-critical for greetings

        if is_returning:
            greeting_prompt = f"You are {self._agent_name}. Welcome back {user_name or 'the user'} for another {time_of_day} session. Keep it warm, professional, and very brief."
        else:
            greeting_prompt = f"You are {self._agent_name}. Introduce yourself to {user_name or 'the user'} this {time_of_day}. Keep it natural, business-like, and welcoming."

        try:
            # Use 'chat' step type for natural temperature (0.7)
            response = await self.call_llm(greeting_prompt, step_type="chat", risk_tier=ctx.risk_tier)
            return response.get("content") or f"Good {time_of_day}, I'm {self._agent_name}. How can I help?"
        except Exception:
            return f"Good {time_of_day}, I'm {self._agent_name}. How can I help?"

    async def get_error_message(
        self,
        missing_fields: list[str] | None = None,
        error_type: str = "generic",
    ) -> str:
        """Generate warm, actionable error message (7c).

        Subclasses can override for custom personality.

        Args:
            missing_fields: List of missing required field names
            error_type: Type of error ("missing_fields", "validation", "generic")

        Returns:
            Friendly error message with guidance
        """
        if error_type == "missing_fields" and missing_fields:
            fields_str = " and ".join(missing_fields)
            return f"I need the {fields_str} — can you provide {'those' if len(missing_fields) > 1 else 'that'}?"
        elif error_type == "validation":
            return "Something doesn't look right with that input. Could you double-check and try again?"
        else:
            return "I ran into an issue. Let me know what you'd like to try next."

    async def run_agentic_loop(
        self,
        task: str,
        ctx: AgentContext,
        *,
        max_steps: int = 5,
        timeout_s: float = 30.0,
        fail_fast: bool = True,
        plan_step_type: str = "plan",
        execute_step_type: str = "draft",
        reflect_step_type: str = "verify",
    ) -> AgentResult:
        """Execute a bounded multi-step reasoning loop.

        The orchestrator calls this with a task description and bounds.
        The agent autonomously:
          1. Recalls relevant memory for context
          2. Plans steps (LLM call with persona + task + memory)
          3. Executes each step (with receipt per step)
          4. Reflects on results and stores insights to memory
          5. Returns final result with full receipt chain

        Guardrails (Law #1 compliance):
          - max_steps: Hard cap on reasoning steps
          - timeout_s: Wall-clock timeout for entire loop
          - Receipt emitted per step (Law #2)
          - Orchestrator initiated this call — agent works within bounds

        Model tiering (Phase 5F):
          Each phase of the loop can use a different model profile via
          the LLM router's step_type routing. This enables cost savings
          by using cheaper models for simple steps (e.g. reflection)
          and reserving expensive models for complex steps (e.g. planning).
          Defaults match pre-existing behavior (plan/draft/verify).

        Args:
            task: Natural language description of what to accomplish
            ctx: Agent context (suite_id, correlation_id, risk_tier)
            max_steps: Maximum reasoning/execution steps (default 5)
            timeout_s: Maximum wall-clock seconds (default 30)
            fail_fast: Exit loop on first step failure (default True)
            plan_step_type: LLM router step_type for the planning phase
                (default "plan" -> primary_reasoner via router)
            execute_step_type: LLM router step_type for execution steps
                (default "draft" -> fast_general via router)
            reflect_step_type: LLM router step_type for the reflection phase
                (default "verify" -> cheap_classifier via router)

        Returns:
            AgentResult with accumulated data and receipt chain
        """
        import asyncio
        import time

        autonomy_cfg = {}
        if hasattr(self, "get_autonomy_policy"):
            autonomy_cfg = getattr(self, "get_autonomy_policy")().get("autonomy", {})
        max_steps = min(max_steps, int(autonomy_cfg.get("max_agentic_iterations", max_steps)))
        timeout_s = min(timeout_s, float(autonomy_cfg.get("timeout_ms", int(timeout_s * 1000)) / 1000.0))

        receipts: list[dict[str, Any]] = []
        accumulated_data: dict[str, Any] = {"steps": [], "task": task}
        loop_start = time.monotonic()
        interruption_reason: str | None = None

        # Memory search cache — local to this loop run to avoid repeated DB hits
        _memory_cache: dict[str, Any] = {}

        # Step 1: Recall relevant memory
        memory_context = ""
        if self._memory_enabled:
            cache_key_facts = f"search:{task}:None:3"
            if cache_key_facts in _memory_cache:
                facts = _memory_cache[cache_key_facts]
            else:
                facts = await self.search_memory(task, ctx, limit=3)
                _memory_cache[cache_key_facts] = facts

            cache_key_episodes = f"episodes:{ctx.suite_id}:2"
            if cache_key_episodes in _memory_cache:
                episodes = _memory_cache[cache_key_episodes]
            else:
                episodes = await self.recall_episodes(ctx, limit=2)
                _memory_cache[cache_key_episodes] = episodes

            if facts:
                memory_context += "\nRelevant facts:\n"
                memory_context += "\n".join(
                    f"- {f['fact_key']}: {f['fact_value']}" for f in facts
                )
            if episodes:
                memory_context += "\nRecent episodes:\n"
                memory_context += "\n".join(
                    f"- {e['summary']}" for e in episodes
                )

        # Step 2: Plan (LLM call to break task into steps)
        plan_prompt = (
            f"You are {self._agent_name}. Plan how to accomplish this task.\n\n"
            f"Task: {task}\n"
            f"{memory_context}\n\n"
            f"Break this into {max_steps} or fewer concrete steps. "
            f"Return each step as a numbered list. Be specific and actionable."
        )

        # Planning gets at most 1/3 of total budget, leaving 2/3 for execution
        plan_budget = min(timeout_s / 3, 10.0)

        try:
            plan_result = await asyncio.wait_for(
                self._llm_call_with_retry(
                    self.execute_with_llm,
                    prompt=plan_prompt,
                    ctx=ctx,
                    event_type=f"{self._agent_id}.plan",
                    step_type=plan_step_type,
                    inputs={"task": task, "max_steps": max_steps},
                ),
                timeout=plan_budget,
            )
        except asyncio.TimeoutError:
            receipt = self.build_receipt(
                ctx=ctx,
                event_type=f"{self._agent_id}.plan",
                status="failed",
                inputs={"task": task},
                metadata={"error": "timeout", "timeout_s": timeout_s},
            )
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Planning timed out")

        if not plan_result.success:
            return plan_result

        receipts.append(plan_result.receipt)
        plan_content = plan_result.data.get("content", "")
        accumulated_data["plan"] = plan_content

        # Step 3: Execute steps (each gets its own receipt)
        early_exit = False
        for step_num in range(1, max_steps + 1):
            elapsed = time.monotonic() - loop_start
            remaining_time = timeout_s - elapsed
            if remaining_time <= 1.0:  # Not enough time for another step
                logger.info("Agentic loop timeout reached after %d steps", step_num - 1)
                interruption_reason = "Loop timed out before another step could complete"
                break
            step_prompt = (
                f"You are {self._agent_name}. Execute step {step_num} of your plan.\n\n"
                f"Original task: {task}\n"
                f"Plan: {plan_content}\n"
                f"{memory_context}\n\n"
                f"Execute step {step_num}. Return the result clearly."
            )

            try:
                step_result = await asyncio.wait_for(
                    self._llm_call_with_retry(
                        self.execute_with_llm,
                        prompt=step_prompt,
                        ctx=ctx,
                        event_type=f"{self._agent_id}.step",
                        step_type=execute_step_type,
                        inputs={"task": task, "step": step_num},
                    ),
                    timeout=remaining_time,
                )
            except asyncio.TimeoutError:
                receipt = self.build_receipt(
                    ctx=ctx,
                    event_type=f"{self._agent_id}.step",
                    status="failed",
                    inputs={"task": task, "step": step_num},
                    metadata={"error": "timeout"},
                )
                await self.emit_receipt(receipt)
                interruption_reason = f"Step {step_num} timed out"
                break

            receipts.append(step_result.receipt)
            accumulated_data["steps"].append({
                "step": step_num,
                "content": step_result.data.get("content", ""),
                "success": step_result.success,
            })

            if not step_result.success:
                interruption_reason = f"Step {step_num} failed"
                if fail_fast:
                    logger.warning(
                        "Agentic loop: step %d failed, exiting early (fail_fast=True)",
                        step_num,
                    )
                    break
                continue

            # Phase 5D: Early completion detection (heuristic, no LLM call)
            step_content = step_result.data.get("content", "")
            if self._is_task_complete(step_content, task):
                logger.info(
                    "Agentic loop: task complete after step %d (early exit)",
                    step_num,
                )
                early_exit = True
                break

        # Step 4: Reflect and store insights
        # Skip reflection for single-step completions (saves 1 LLM call)
        completed_steps = accumulated_data["steps"]
        if len(completed_steps) <= 1:
            logger.info("Single-step completion — skipping reflection")
        elif self._memory_enabled and completed_steps:
            reflection_prompt = (
                f"You are {self._agent_name}. Reflect on what you just did.\n\n"
                f"Task: {task}\n"
                f"Steps completed: {len(completed_steps)}\n\n"
                f"What is ONE key fact or insight worth remembering for next time? "
                f"Return ONLY the fact in the format: key: value"
            )
            try:
                reflection = await asyncio.wait_for(
                    self._llm_call_with_retry(
                        self.call_llm,
                        reflection_prompt,
                        step_type=reflect_step_type,
                    ),
                    timeout=10.0,
                )
                content = reflection.get("content", "")
                if ":" in content:
                    fact_key, fact_value = content.split(":", 1)
                    await self.remember(
                        fact_key.strip().lower().replace(" ", "_"),
                        fact_value.strip(),
                        ctx,
                        fact_type="business_fact",
                        confidence=0.8,
                    )
            except Exception:
                logger.debug("Reflection failed for %s — non-critical", self._agent_id)

        # Step 5: Build final result
        final_content = completed_steps[-1]["content"] if completed_steps else ""
        successful_steps = [s for s in completed_steps if s["success"]]
        all_succeeded = bool(completed_steps) and len(successful_steps) == len(completed_steps)
        final_status = "ok"
        final_error: str | None = None

        if not completed_steps:
            final_status = "failed"
            final_error = interruption_reason or "No execution steps completed"
        elif all_succeeded:
            final_status = "ok"
        else:
            final_status = "partial" if successful_steps else "failed"
            final_error = interruption_reason or "One or more execution steps failed"

        final_receipt = self.build_receipt(
            ctx=ctx,
            event_type=f"{self._agent_id}.complete",
            status=final_status,
            inputs={"task": task, "steps_completed": len(completed_steps)},
            metadata={
                "receipt_chain": [r.get("receipt_id") for r in receipts],
                "total_steps": len(completed_steps),
                "early_exit": early_exit,
                "interruption_reason": interruption_reason,
            },
        )
        await self.emit_receipt(final_receipt)

        return AgentResult(
            success=all_succeeded,
            data={"content": final_content, **accumulated_data},
            receipt=final_receipt,
            error=final_error,
        )


    def _is_task_complete(self, result: str, task: str) -> bool:
        """Heuristic check if the agentic task is complete (no LLM needed).

        Checks for completion indicators in the step result text.
        Returns True if the result contains final answer patterns
        and no "next step" indicators.

        Args:
            result: The text output from the most recent step
            task: The original task description (reserved for future use)

        Returns:
            True if the task appears complete based on text heuristics
        """
        result_lower = result.lower()

        # Completion indicators
        complete_signals = [
            "task complete",
            "here is the answer",
            "the result is",
            "summary:",
            "final answer",
            "completed successfully",
        ]

        # Continuation indicators (override completion)
        continue_signals = [
            "next step",
            "then we need to",
            "additionally",
            "remaining tasks",
            "todo:",
            "step 2:",
            "step 3:",
        ]

        has_complete = any(s in result_lower for s in complete_signals)
        has_continue = any(s in result_lower for s in continue_signals)

        return has_complete and not has_continue

    async def _llm_call_with_retry(
        self,
        call_fn: Any,
        *args: Any,
        max_retries: int = 2,
        base_delay: float = 1.0,
        **kwargs: Any,
    ) -> Any:
        """Retry transient LLM failures with exponential backoff.

        Only retries on transient errors (rate limits, timeouts, 5xx).
        Non-transient errors are raised immediately.

        Args:
            call_fn: Async callable to invoke
            *args: Positional arguments for call_fn
            max_retries: Maximum retry attempts (default 2)
            base_delay: Base delay in seconds, doubled each retry
            **kwargs: Keyword arguments for call_fn

        Returns:
            Result from call_fn
        """
        import asyncio

        _transient_markers = ("rate_limit", "timeout", "429", "500", "502", "503", "529")
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                return await call_fn(*args, **kwargs)
            except Exception as e:
                last_error = e
                error_str = str(e).lower()
                is_transient = any(kw in error_str for kw in _transient_markers)
                if is_transient and attempt < max_retries:
                    delay = base_delay * (2 ** attempt)
                    logger.warning(
                        "LLM call failed (attempt %d/%d, retrying in %.1fs): %s",
                        attempt + 1,
                        max_retries + 1,
                        delay,
                        e,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise

        # All retries exhausted — should not reach here but satisfies type checker
        raise last_error  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════════
# TEMPLATE: Copy below and customize for your new agent
# ═══════════════════════════════════════════════════════════════════════════


class _TemplateSkillPack(AgenticSkillPack):
    """{Brief description of what this agent does}.

    Replace this entire class with your agent's implementation.
    See EnhancedEliInbox for a real-world example.
    """

    def __init__(self) -> None:
        super().__init__(
            agent_id="{agent-id}",       # kebab-case, matches manifest
            agent_name="{Agent Name}",   # Display name
            default_risk_tier="green",   # green, yellow, or red
            memory_enabled=True,         # Set False if agent doesn't need memory
        )

    async def read_action(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        """{Read/query action}. GREEN tier — no side effects."""
        # 1. Validate required params (Law #3: fail closed)
        required_field = params.get("required_field")
        if not required_field:
            receipt = self.build_receipt(
                ctx=ctx,
                event_type="{domain}.read",
                status="denied",
                inputs={"action": "{domain}.read"},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_REQUIRED_FIELD"]
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Missing required_field")

        # 2. Check memory for relevant context (agentic memory)
        past_context = await self.search_memory(str(required_field), ctx, limit=2)

        # 3. Execute via LLM with governance
        memory_note = ""
        if past_context:
            memory_note = f"\nRelevant context: {past_context[0]['fact_value']}"

        return await self.execute_with_llm(
            prompt=(
                f"You are {self._agent_name}. {memory_note}\n\n"
                f"Read request: {required_field}\n\n"
                f"Provide the information requested."
            ),
            ctx=ctx,
            event_type="{domain}.read",
            step_type="draft",
            inputs={"action": "{domain}.read", "query": str(required_field)},
        )

    async def write_action(self, params: dict[str, Any], ctx: AgentContext) -> AgentResult:
        """{State-changing action}. YELLOW tier — requires approval."""
        # 1. Validate (Law #3)
        required_field = params.get("required_field")
        if not required_field:
            receipt = self.build_receipt(
                ctx=ctx,
                event_type="{domain}.{action}",
                status="denied",
                inputs={"action": "{domain}.{action}"},
            )
            receipt["policy"]["decision"] = "deny"
            receipt["policy"]["reasons"] = ["MISSING_REQUIRED_FIELD"]
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error="Missing required_field")

        # 2. Execute (call tools, providers, LLM)
        try:
            result = await self._execute_write(params, ctx)
        except Exception as e:
            receipt = self.build_receipt(
                ctx=ctx,
                event_type="{domain}.{action}",
                status="failed",
                inputs=params,
            )
            await self.emit_receipt(receipt)
            return AgentResult(success=False, receipt=receipt, error=str(e))

        # 3. Store insight to memory (agentic memory)
        await self.remember(
            f"{'{domain}'}_{required_field}",
            f"Completed {'{domain}.{action}'} for {required_field}",
            ctx,
            fact_type="business_fact",
        )

        # 4. Emit success receipt (Law #2)
        receipt = self.build_receipt(
            ctx=ctx,
            event_type="{domain}.{action}",
            status="ok",
            inputs=params,
            metadata={"tool_used": "...", "model_used": "..."},
        )
        await self.emit_receipt(receipt)

        return AgentResult(success=True, data=result, receipt=receipt)

    async def _execute_write(
        self, params: dict[str, Any], ctx: AgentContext
    ) -> dict[str, Any]:
        """Internal implementation — replace with actual provider/tool logic."""
        # Use self.call_llm() for LLM calls
        # Use external provider clients for tool calls
        # Always pass ctx.correlation_id for tracing
        raise NotImplementedError("Implement action logic")

    async def agentic_action(
        self, task: str, ctx: AgentContext
    ) -> AgentResult:
        """Complex multi-step action using the agentic loop.

        Use this for tasks that require planning, multiple steps,
        and reflection. The orchestrator bounds the loop.
        """
        return await self.run_agentic_loop(
            task=task,
            ctx=ctx,
            max_steps=3,     # Adjust per action complexity
            timeout_s=25.0,  # Leave headroom under 30s orchestrator timeout
        )
