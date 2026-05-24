---
name: wave-20-6-context-binder-patterns
description: Stage 6a/6b of Wave 20.6 — context_aware_binder and vlm_symbol_verifier design patterns, weight choices, and integration lessons.
metadata:
  type: project
---

## context_aware_binder.compute_binding_score() — Key Invariants

**The disambiguation problem**: Same circular glyph = SPRINKLER (FP), JUNCTION (E), DRAIN (P), FIXTURE (RCP). Geometry cosine_sim returns 1.0 for all. Discipline weight (0.35) is the primary discriminator.

**Weight profile** (plan v7 §2.7.6, LOCKED):
- geometry_weight = 0.20
- discipline_weight = 0.35 (heaviest — blocks cross-discipline at cosine_sim=1.0)
- sheet_context_weight = 0.20
- nearby_text_weight = 0.25

**Cross-discipline rejection math**: 0.20×1.0 + 0.35×0.0 + 0.20×1.0 + 0.25×0.0 = 0.40 (below 0.92 auto-bind). This is the design invariant; test_cross_discipline_rejected_despite_perfect_geometry validates it.

**Tokenizer**: `_tokenize()` lower-cases, strips non-alphanumeric with `re.sub(r"[^a-z0-9\s]", " ", ...)`, then splits. Handles hyphens — "QR-PENDANT" → {'qr', 'pendant'}. Jaccard("QR-PENDANT" vs ['qr','pendant','v2708']) = 2/3 ≈ 0.667.

**Sheet context score**: `_PLACED_REGIONS = {'plan', 'detail'}` → 1.0; any other region → 0.5. This penalizes schedule/legend occurrences where the symbol is referenced rather than placed.

## vlm_symbol_verifier — Key Patterns

**Budget tracking**: `_project_spend_tracker` is a mutable `dict[str, float]` passed by reference across all calls in one project. Cap check happens BEFORE the tenant isolation lookup to fail fast. Cap env var: `ASPIRE_VLM_VERIFIER_CAP_USD` (default 0.50).

**Cost model** (gpt-5.4-mini, May 2026): $0.15/1M input tokens, $0.60/1M output tokens. Two 100×100px crops ≈ ~1100 input tokens + 30 output tokens ≈ $0.0002/call.

**Fail-closed parse**: `_parse_verdict()` — if response doesn't start with YES or NO, verdict = 'no'. Law #3.

**PNG crop**: Pillow `Image.crop((left, upper, right, lower))` with 20px padding. Bbox dict accepts {x0,y0,x1,y1} OR {left,top,right,bottom}; list accepts [x0,y0,x1,y1].

**Receipt action_type**: `provider.openai.vlm_verifier` for all outcomes (success, failure, denied). Metadata fields: cluster_id, candidate_legend_id, verdict, reason (truncated to 200 chars), cost_usd, model.

## generate_json_async Integration

From `vision_helper.py` pattern: call `generate_json_async` with `model_profile="primary_reasoner"`, wrap in `asyncio.wait_for(timeout=CALL_TIMEOUT_S + 2.0)`. Result dict has `.get("text")` for raw string and `.get("json")` for parsed dict. Usage is in `result.get("usage") or {}` with keys `prompt_tokens` / `completion_tokens`.

**Why +2.0 on asyncio.wait_for**: The inner `generate_json_async` has its own `timeout_seconds` param. The outer asyncio.wait_for is a hard kill guard — set 2s longer so the inner timeout fires first and surfaces a clean error rather than an asyncio.CancelledError.

## Cross-Branch Rebase Lesson

When teammates push to the same branch concurrently, local commits interleave with remote commits. `git rebase origin/branch` fails if untracked files would be overwritten by the rebase. Fix: the rebase may report "up to date" because git already detected the commits are equivalent — verify with `git log --oneline origin/...` before assuming push failure.
