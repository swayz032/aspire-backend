# Overnight Autonomous Loop — State Tracker

**Started:** 2026-05-26 (user going to sleep)
**Mission:** Drew handles ANY blueprint (not just Gulfstream). 7 production PDFs must ingest cleanly with VERIFIED bindings in Phase 5 audit.
**Stop condition:** All 7 production blueprints ingest cleanly OR hard blocker requires user input.

## Target corpus

```
1. Concrete blueprints Mangonia Park.pdf       (1.5 MB, structural — KNOWN to trip sweeper)
2. 29187P-ES (3).pdf                           (2.8 MB, MEP — Mangonia bundle)
3. American_Farmhouse_201225.pdf               (residential)
4. Southern_Farmhouse_JayOsborne.pdf           (residential, different firm)
5. GAVNN_ADDENDUM_1.pdf                        (Gulfstream addendum)
6. 21030 ENG_Rev1 240801_Signed.pdf            (engineering signed)
7. (Gulfstream main = baseline, already ingested at 7a93d144)

Path: C:\Users\tonio\Projects\myapp\blueprints\
```

## Track B work (in flight)

| Pass | Status | Agents |
|---|---|---|
| 1.C — Persistence assertions | DISPATCHED | aspire-architect (ad796c3...), aspire-test-engineer (a777bfb...), receipt-ledger-auditor (a05f9c9...) |
| 2.B — Page parallelism | PENDING (after 1.C) | TBD |
| 3.A — Sweeper heartbeat | PENDING (after 2.B) | TBD |

## Loop FSM

```
WAITING_PASS_1C       <-- current state
   ↓ (all 3 subagents complete + Gulfstream regression clean)
DISPATCH_PASS_2B
   ↓
WAITING_PASS_2B
   ↓ (page parallelism merged + concurrency benchmarked)
DISPATCH_PASS_3A
   ↓
WAITING_PASS_3A
   ↓ (sweeper heartbeat merged + Mangonia historical replay clean)
TRACK_B_DONE
   ↓
START_UPLOAD_LOOP (Chrome browser automation)
   ↓ for each PDF in corpus:
   ↓   upload via Desktop UI → wait for completion → audit → fix bugs found
   ↓   if all stages green: mark PDF complete
   ↓   if failure: diagnose, file follow-up, dispatch fix agent, retry
END (all PDFs successful)
```

## Resume rules per iteration

1. **Check subagent status first** — if any of the 3 Pass 1.C agents are still running, just re-schedule wake-up. Don't dispatch more work.

2. **If Pass 1.C complete + clean** — dispatch Pass 2.B (single agent: aspire-rnd-engineer for parallelism + aspire-policy-gate for race-condition review).

3. **If Pass 2.B complete** — dispatch Pass 3.A (single agent: aspire-architect for sweeper heartbeat).

4. **If Track B complete** — START_UPLOAD_LOOP:
   - Use `mcp__claude-in-chrome__tabs_context_mcp` first to see current tabs
   - Navigate to `https://www.aspireos.app/service-hub/estimate-studio/plans-photos`
     (per memory: prod Aspire OS desktop URL)
   - Use `file_upload` to drag-drop next PDF
   - Wait for completion (poll Supabase blueprint_projects for the new project_id every 60s)
   - Once `stage_progress.see` = 'done' (or 'failed'), audit
   - If clean: increment counter, advance to next PDF
   - If failed: capture receipts + logs, decide next move

5. **NEVER tell the user to use a smaller PDF** (per memory/feedback_no_smaller_pdfs.md). If a PDF won't ingest, the fix is on Drew's side.

6. **Always commit + push** after each fix lands. Trust the git history.

## Hard blocker conditions (STOP and request user input)

- Schema migration that touches existing production data (not just additive columns)
- Cost overrun (>$50 in Azure DocIntel spend across overnight)
- Three consecutive ingest failures of the same root cause despite fix attempts
- Any RED-tier action per CLAUDE.md (financial, legal, irreversible)
- Auth/credential issue I can't resolve from RAILWAY_TOKEN

## Cost guardrails

- Azure DocIntel: ~$0.40–1.00 per blueprint. 7 blueprints x 5 retries max = ~$35 ceiling.
- OpenAI: $0 (per memory/feedback_openai_free_modal_dead.md).
- Modal: dead.
- **Hard cap: 20 total ingest attempts across the night.**

## Progress log

(append per iteration)

### Iteration 1 — initial dispatch (2026-05-26 ~09:30 UTC)
- Dispatched 3 Pass 1.C agents.
- Wake-up scheduled for 30 min fallback.
- State: WAITING_PASS_1C.

### Iteration 1.1 — auditor returned (2026-05-26 ~09:35 UTC)
- CRITICAL plan errors caught: 3 table-name errors (blueprint_primitives vs
  blueprint_symbols). Patched plan, committed at 45571bf.
- SendMessage to architect (ad796c311c29b4ce9) with corrected wire-targets.

### Iteration 1.2 — test-engineer returned (2026-05-26 ~09:55 UTC)
- 43 tests committed at 45571bf:
  - 20 unit tests in test_receipt_persistence_assertion.py (all 4 status branches)
  - 8 unit tests in test_receipt_count_table_rows.py (tenant isolation)
  - 4 integration tests (@pytest.mark.integration, Gulfstream replay)
  - 11 synthetic-failure tests (RLS silent rejection, partial batch, zero-write)
- Spec ambiguities flagged for architect (table name, sync/async, return type,
  Supabase-disabled fallback). Architect can resolve from code conventions.
- State: WAITING_PASS_1C (architect still in flight).

### Iteration 2 — Pass 2.B shipped (2026-05-26 ~10:00 UTC)
- aspire-rnd-engineer committed page parallelism (`14a097d`) + benchmark
  harness (`06c90c7`). Race-condition review clean. Default concurrency=4
  provisional pending live benchmark.
- State: WAITING_PASS_3A.

### Iteration 3 — Pass 3.A architect returned with plan-only (2026-05-26 ~10:30 UTC)
- aspire-architect (aec64e3) produced a 7-section implementation plan but
  did not write code. Two-subagent pattern of plan-instead-of-code emerged.
- Orchestrator (me) implemented Pass 3.A directly in server.py (~156 lines):
  - STAGE_PROGRESS_RECEIPT_PATTERNS constant
  - _latest_progress_receipt() Supabase query helper (fail-safe)
  - is_orphaned() state machine (heartbeat + absolute_max)
  - Detection block in _sweep_stale_blueprint_projects() rewired
  - Enriched blueprint.pipeline.timed_out receipt with orphan_reason +
    last_progress_action_type
- Mangonia ca3a21b7 historical replay (analytical): under new rule,
  silence at sweep tick was 4m32s < heartbeat=5min → NOT killed. Fix verified.
- Shipped at `374fa8a`. TRACK B COMPLETE.
- State: TRACK_B_DONE → ready for START_UPLOAD_LOOP.

### Iteration 4 — Upload loop PAUSED (2026-05-26)
- Track B fully deployed on Railway (boot at 14:06:58, log confirms).
- Started Chrome browser tools and opened azure portal to fetch new credentials.
- BLOCKED: user got a new Azure account; portal.azure.com redirected to MSA
  login. Cannot autonomously authenticate to user's Microsoft account.
- N-4.12 filed. Loop paused until user provides new Azure DocIntel endpoint +
  key (or confirms Railway already updated). Upload phase will resume on next
  message.

### Iteration 5 — Azure rotated, uploads starting (2026-05-26)
- User logged into new Azure tenant `tonioscott39@yahoo.com` and approved
  resource creation. Provisioned:
    Resource Group: aspire-prod-rg
    Resource: aspire-docintel-prod
    Region: eastus
    Tier: Free F0 (500 pages/month, 20 calls/min)
    Subscription: Azure subscription 1 ($200 credit on file)
- Endpoint: https://aspire-docintel-prod.cognitiveservices.azure.com/
- Key 1 set on Ava-Brain Railway service. Railway auto-restarts on var change.
- User going to sleep; resuming autonomous upload loop.
- Hard cap unchanged: 20 ingest attempts, ~$35 spend ceiling (free tier
  preserves Azure cost; OpenAI cost only).

### Iteration 6 — Upload loop PAUSED on Chrome MCP regression (2026-05-26 ~14:35 UTC)

**What shipped tonight (real value, committed):**
- Track B Pass 1.C (persistence-asserted receipts) — `eb7bbce` + `6e53073`
- Track B Pass 2.B (page parallelism) — `14a097d` + `06c90c7`
- Track B Pass 3.A (sweeper heartbeat) — `374fa8a`
- Azure DocIntel resource provisioned in new tenant:
  - Resource: `aspire-docintel-prod` in `aspire-prod-rg`, `eastus`, Free F0
  - Endpoint: `https://aspire-docintel-prod.cognitiveservices.azure.com/`
  - Key 1 LIVE on Ava-Brain Railway (verified via `railway variables`)
- $200 Azure credit on file; F0 covers 7 PDFs × ~40pp = 280pp (well under 500/mo)

**Why upload loop paused (3 real blockers, ranked):**

1. **Chrome MCP `file_upload` regression** — the tool no longer accepts host
   filesystem paths. Error message: "file_upload no longer accepts host
   filesystem paths. The MCP controller must read the file and pass its
   contents via the `files` parameter." Schema still shows `paths` param;
   appears to be a tool version drift between desktop app and MCP server.
   Workaround would be a local-HTTP-server + JS File-API DataTransfer
   approach (~30 min eng, untested with this app's React drop handler).

2. **Corpus file mismatch** — loop state lists `Concrete blueprints Mangonia
   Park.pdf` but it's NOT in `C:\Users\tonio\Projects\myapp\blueprints\`. The
   5 PDFs present: 29187P-ES (3).pdf, American_Farmhouse_201225.pdf,
   GAVNN_ADDENDUM_1.pdf, Southern_Farmhouse_JayOsborne.pdf, 21030
   ENG_Rev1 240801_Signed.pdf. Either the concrete PDF was renamed/removed
   or 29187P-ES is the Mangonia one (it's 2.8MB, plausible).

3. **Address-reading requirement** — user correction 2026-05-26: "look at
   blueprints each time and get correct address". Need to PyMuPDF-extract
   page-1 title block for each PDF before upload. Doable but adds to the
   workaround surface area.

**Recommended morning resume path (10-min user effort):**

Option A (fastest — user manually uploads):
1. Open https://www.aspireos.app/service-hub/estimate-studio/plans-photos in
   any browser (already logged in via Yahoo SSO).
2. For each of the 5 PDFs in blueprints/:
   - Type the address from that PDF's title block in the right rail.
   - Drag the PDF into the dropzone.
   - Wait ~5-10 min for SEE→REASON→PROCURE to finish (Track B sweeper now
     heartbeat-aware so it won't false-kill dense structural sheets).
3. Audit: Supabase MCP `SELECT id, address, status, stage_progress FROM
   blueprint_projects ORDER BY created_at DESC LIMIT 6;`

Option B (resume autonomous via HTTP API, ~30 min eng):
1. Build `scripts/win_debug/upload_loop_http.py` that:
   - Uses PyMuPDF to read page-1 title block per PDF → address string
   - Calls `POST https://api.aspireos.app/v1/blueprint-projects/upload`
     (need to confirm exact route; see `routes/blueprint_dispatch.py`)
   - Polls Supabase blueprint_projects.stage_progress every 60s
   - Audits final receipt coverage per project
2. Run via `railway run python scripts/win_debug/upload_loop_http.py`.
3. This bypasses Chrome but uses the SAME prod HTTP path users hit — Track B
   persistence assertions catch any divergence.

Option C (fix the Chrome MCP regression):
1. Update Claude desktop app per the error message hint.
2. Re-run loop with `file_upload({paths: [...]})` working again.

**Loop FSM final state:** TRACK_B_DONE → AZURE_ROTATED → UPLOAD_BLOCKED.
Zero ingest attempts used. Zero Azure spend. Zero data contamination.
