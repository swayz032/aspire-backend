"""N-4.8 — Trigger Drew full pipeline against Mangonia PDF.

Runs locally (via `railway run --service Ava-Brain python ...`) with
production env vars injected. Calls the pipeline function directly,
bypassing the HTTP capability_token layer.

Usage:
    RAILWAY_TOKEN=... railway run --service Ava-Brain python scripts/trigger_mangonia_ingest.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path

# Ensure orchestrator src is on path
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "src"))


PDF_PATH = Path(r"C:\Users\tonio\Projects\myapp\blueprints\Concrete blueprints Mangonia Park.pdf")
SUITE_ID = "94b89098-c4bf-4419-a154-e18d9d53f993"
OFFICE_ID = "94b89098-c4bf-4419-a154-e18d9d53f993"
MODEL = "gpt-5.4-mini"


async def main() -> int:
    if not PDF_PATH.exists():
        print(f"ERROR: PDF not found at {PDF_PATH}")
        return 1

    pdf_size = PDF_PATH.stat().st_size
    print(f"PDF: {PDF_PATH.name} ({pdf_size/1024/1024:.1f} MB)")

    # Verify we're inside Railway env (have prod credentials)
    required_env = [
        "ASPIRE_SUPABASE_URL",
        "ASPIRE_AZURE_DOC_INTEL_ENDPOINT",
        "ASPIRE_OPENAI_API_KEY",
    ]
    missing = [k for k in required_env if not os.getenv(k)]
    if missing:
        print(f"ERROR: missing env vars: {missing}")
        print("Run via: RAILWAY_TOKEN=... railway run --service Ava-Brain python scripts/trigger_mangonia_ingest.py")
        return 1

    print(f"ASPIRE_SUPABASE_URL: {os.environ['ASPIRE_SUPABASE_URL'][:50]}...")
    print(f"ASPIRE_DREW_SEE_PROVIDER: {os.getenv('ASPIRE_DREW_SEE_PROVIDER', '(unset, defaults local)')}")

    # Generate IDs
    project_id = str(uuid.uuid4())
    correlation_id = str(uuid.uuid4())
    print(f"\nproject_id     = {project_id}")
    print(f"correlation_id = {correlation_id}")
    print(f"suite_id       = {SUITE_ID}")

    # Create blueprint_projects row first (pipeline expects it to exist)
    print("\nCreating blueprint_projects row ...")
    from aspire_orchestrator.services.supabase_client import supabase_insert_sync  # noqa: E402
    supabase_insert_sync("blueprint_projects", {
        "id": project_id,
        "suite_id": SUITE_ID,
        "office_id": OFFICE_ID,
        "created_by": SUITE_ID,
        "address": "Mangonia Park, FL (cross-project audit N-4.8)",
        "stage_progress": {},
        "unwrangle_credits_used": 0,
    })
    print("  + row inserted")

    # Import + invoke pipeline
    print("\nImporting pipeline ...")
    from aspire_orchestrator.routes.blueprint_dispatch import _run_full_pipeline  # noqa: E402
    print("  ✓ imported")

    print(f"\nStarting _run_full_pipeline at {time.strftime('%H:%M:%S')} ...")
    t0 = time.monotonic()
    try:
        await _run_full_pipeline(
            project_id=project_id,
            suite_id=SUITE_ID,
            office_id=OFFICE_ID,
            filename=PDF_PATH.name,
            pdf_path=PDF_PATH,
            correlation_id=correlation_id,
            model=MODEL,
        )
    except Exception as e:
        elapsed = time.monotonic() - t0
        print(f"\nPIPELINE FAILED after {elapsed:.1f}s: {type(e).__name__}: {e}")
        return 2

    elapsed = time.monotonic() - t0
    print(f"\nPIPELINE COMPLETE in {elapsed:.1f}s")
    print(f"  project_id: {project_id}")
    print(f"  Next: query motifs/legends/primitives for this project_id")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
