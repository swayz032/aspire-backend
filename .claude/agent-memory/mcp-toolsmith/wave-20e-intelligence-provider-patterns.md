---
name: wave-20e-intelligence-provider-patterns
description: SheetIntelligenceProvider cache-first pattern, cascade wiring, and patch-target fix for module-level imports
metadata:
  type: project
---

## SheetIntelligenceProvider (Wave 20e) — Key patterns

### Module-level import for patchability
Import `supabase_select` at module level (not lazily inside `load_all`) so tests can patch
`aspire_orchestrator.services.blueprint.sheet_intelligence_provider.supabase_select`.
Lazy `from ... import X` inside a function body makes the name un-patchable at the module path.

### Constructor args order
`SheetIntelligenceProvider(project_id, suite_id, office_id=None)` — note project_id FIRST,
unlike SheetDataProvider which takes `(suite_id, project_id, correlation_id)`.

### Cache-first pattern in resolver
```python
intel_provider = getattr(candidate, "intelligence_provider", None)
if intel_provider is not None:
    tables = await intel_provider.tables_on_sheet(sheet_id)
    if tables:
        # cache hit — return result with azure_calls_made=0
```
Use `getattr(candidate, "intelligence_provider", None)` not `candidate.intelligence_provider`
when the field might not exist on older candidate instances (defensive access).

### resolve_batch wiring
Build intel_provider + call `await intel_provider.load_all()` BEFORE building candidates.
Pattern mirrors VisionBudget construction — fail-safe try/except, always falls through to live Azure on failure.

### Test fixture pre-loading
Tests that test accessors (legends, schedules, notes, tables_on_sheet) can bypass load_all:
```python
provider = SheetIntelligenceProvider(_PROJECT, _SUITE)
provider._rows = {sheet_id: row_dict}
provider._loaded = True
```
This avoids patching and makes tests fast/clear.

### Commit 20e context
- 20a: migration (blueprint_sheet_intelligence table)
- 20b: universal_sheet_extractor + base.py intelligence_provider field (already committed by architect)
- 20c+20d: ingest integration (wires extractor at INGEST time)
- 20e: THIS — provider reads cache, cascade uses it cache-first
- 20f: Drew REASON stage consumes provider

**Why:** 20e was pre-committed partially — base.py, sheet_intelligence_provider.py, and test file were already in HEAD when toolsmith-cascade started. Only missing_input_cascade.py and schedule_resolver.py needed the wiring.
