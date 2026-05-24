---
name: drew-wave19-cascade-patterns
description: Wave 19 Drew self-resolution cascade patterns — resolver framework, dedup constraint, DB schema quirks, OpenAI Responses API vision format
metadata:
  type: project
---

## Wave 19 Self-Resolution Cascade (2026-05-21)

### Architecture
- `resolvers/base.py` — MissingInputResolver ABC + ResolverResult + MissingInputCategory enum
- `resolvers/{discipline,scale,addenda,dimension,detail,schedule}_resolver.py` — 6 resolvers
- `missing_input_cascade.py` — cascade orchestrator: categorize → route → parallel dispatch (90s cap)
- Resolved threshold: confidence >= 0.85
- Receipt per cascade run: `blueprint.cascade.{category}` (Law #2)

### Dedup UNIQUE constraint (Wave 19a)
- `description_hash` generated column: `md5(description) STORED`
- `UNIQUE(project_id, description_hash)` prevents exact-text duplicates from re-runs
- Insert sites use `supabase_insert_batch_ignore_conflicts(..., conflict_target="project_id,description_hash")`
- Current 22 Gulfstream rows are SEMANTICALLY duped but TEXTUALLY unique → not collapsed by constraint

### DB schema quirk: resolved_by is UUID FK
- `blueprint_missing_inputs.resolved_by` is `uuid` FK to `auth.users`
- For automated cascade resolution: use `resolved_method text` (Wave 19 new column)
- Set `resolved_method = 'cascade:wave19:<method>'`, leave `resolved_by = NULL`
- DO NOT write text strings into `resolved_by` — 400 error

### OpenAI Responses API vision format (CRITICAL)
- Responses API does NOT accept `"type": "image_url"` in message content
- Correct format: `{"type": "input_image", "image_url": "<url>"}` and `{"type": "input_text", "text": "..."}`
- Standard Chat Completions format uses `"type": "image_url"` — different APIs
- `generate_json_async` in this codebase routes through Responses API by default

### Backfill results (Gulfstream 5b0adf45)
- Before: 22 unresolved  
- After: 18 unresolved, 4 resolved by cascade
- discipline: 4/4 resolved via vision_title_block (conf 0.96-0.99)
- scale: 0/4 resolved (OCR-only project, no image bytes stored for these sheets)
- addenda: 0/2 resolved (best_conf=0.80, below 0.85 threshold — legit contractor todo)
- dimension: 0/2 resolved (no thumbnails available for vision pass)
- detail: 0/2 resolved (best_conf=0.40, callout markers not resolvable from OCR alone)
- schedule: 0/6 resolved (no Azure OCR tier=premium configured, no image bytes)
- unknown: 0/2 — fire protection items not matching any category keyword

### Category keyword patterns
- discipline: `\bdiscipline|\bclassif` → no `\b` suffix on plural
- dimension: `\bdimension` → no `\b` suffix (matches "dimensions")
- addenda: `\baddend(?:um|a)?\b|\brevision\s+hierarch` (no `\b` after hierarch)
- Pattern `\b` after root word causes no-match on inflected forms — use without trailing `\b`

### Cascade wire-in locations in drew_blueprint.py
- `_async_reason_pipeline`: after `write_story()` returns
- `_async_classify_pipeline`: after verifier receipt emits, before return
- Both are non-blocking (cascade errors caught, logged, skipped)

**Why:** scale/schedule/dimension items need actual image bytes or Azure OCR in production. The 18 remaining are LEGITIMATE contractor todos on this OCR-only project.
