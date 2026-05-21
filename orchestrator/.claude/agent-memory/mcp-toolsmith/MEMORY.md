# MCP Toolsmith Agent Memory — Aspire Backend

## Blueprint streaming patterns (Wave 9f, 2026-05-19)
- See `blueprint-streaming-storage-patterns.md`
- stream_pdf_to_temp_file returns (Path, hex_digest, size_bytes) — caller owns temp file, must unlink in finally
- _run_full_pipeline now accepts pdf_path: Path, reads bytes once at top, deletes in finally block
- Every early-exit HTTPException path in upload-complete must pdf_path.unlink(missing_ok=True) before raising
- Magic-byte check reads only 8 bytes: `pdf_path.open("rb").read(8)` — never load full PDF for type check

## storage_lifecycle orphan query — correct column (Wave 9f, 2026-05-19)
- blueprint_projects has NO upload_id column — FK is pdf_storage_path = "{suite_id}/{upload_id}/raw.pdf"
- PostgREST filter for paths with slashes: url-encode with quote(path, safe="") → %2F separators
- Pattern: `f"pdf_storage_path=eq.{quote(f'{suite_id}/{upload_id}/raw.pdf', safe='')}"`

## supplier_matcher shim pattern
- adam_supplier_router is the canonical module; supplier_matcher.py is a backward-compat shim
- The original supplier_matcher aggregated BOTH serpapi_homedepot AND google_places for ALL categories
- Shim re-exposes: match_suppliers(), _fetch_office_location(), _insert_missing_input(), _emit_search_receipt()
- Tests patch at supplier_matcher.* namespace for these helpers

## supabase_select lazy import pattern
- storage_lifecycle.py imports supabase_select INSIDE function bodies (lazy), not at module level
- Patching "aspire_orchestrator.services.blueprint.storage_lifecycle.supabase_select" → AttributeError
- Correct patch target: "aspire_orchestrator.services.supabase_client.supabase_select"
- OR patch.object(sc_module, "supabase_select") where sc_module = imported supabase_client

## drew_blueprint.py pre-existing syntax error (Wave 9f context)
- drew_blueprint.py:1499 has an empty try: block (Team E incomplete work)
- Any test that imports blueprint_dispatch transitively imports drew_blueprint → IndentationError
- Workaround: use AST/source-text tests instead of module import for blueprint_dispatch regression guards
- DO NOT fix drew_blueprint.py — Team E owns it

## test_blueprint_dispatch_auth.py magic-byte / dedup tests (Wave 9f)
- Tests patching stream_pdf_for_hash must be updated to stream_pdf_to_temp_file
- New mock return value: (real_Path, hex_hash, size_bytes) — must write actual bytes to a real temp file
- Use tempfile.NamedTemporaryFile(delete=False) + Path(tmp.name) + write bytes + close
- Wrap test body in try/finally: path.unlink(missing_ok=True)
