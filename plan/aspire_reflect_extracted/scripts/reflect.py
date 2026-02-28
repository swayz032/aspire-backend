#!/usr/bin/env python3

"""
Aspire Reflect

- Reads .claude/session-notes.md
- Generates proposal diffs into proposed/reflect-*/
- Optionally applies low/medium-risk proposals (--apply)
- Always emits a Reflection Receipt JSON into the proposal bundle

High-risk files are never auto-applied by default.
"""
import argparse
import pathlib
import datetime
import re
import difflib
import json
import os
import hashlib
import uuid
import platform

RISK_HIGH_FILES = {"SAFETY.md", "RECEIPTS.md"}  # never auto-apply

def now_tag() -> str:
    return datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

def iso_now() -> str:
    return datetime.datetime.now().astimezone().isoformat()

def sha256_file(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def extract_candidates(text: str):
    """Lightweight extraction from session notes.

    Recognizes lines like:
      - Correction: ...
      - Approval: ...
      - Pattern: ...
      - Never: ...
    """
    buckets = {"corrections": [], "approvals": [], "patterns": [], "nevers": []}
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if re.match(r"(?i)^(\-|\*)?\s*correction\s*:\s*", s):
            buckets["corrections"].append(re.sub(r"(?i)^(\-|\*)?\s*correction\s*:\s*", "", s))
        elif re.match(r"(?i)^(\-|\*)?\s*approval\s*:\s*", s):
            buckets["approvals"].append(re.sub(r"(?i)^(\-|\*)?\s*approval\s*:\s*", "", s))
        elif re.match(r"(?i)^(\-|\*)?\s*pattern\s*:\s*", s):
            buckets["patterns"].append(re.sub(r"(?i)^(\-|\*)?\s*pattern\s*:\s*", "", s))
        elif re.match(r"(?i)^(\-|\*)?\s*never\s*:\s*", s):
            buckets["nevers"].append(re.sub(r"(?i)^(\-|\*)?\s*never\s*:\s*", "", s))
    return buckets

def propose_append(skill_path: pathlib.Path, bullets, section_header="## Changelog"):
    content = skill_path.read_text()
    if section_header not in content:
        content = content.rstrip() + f"\n\n{section_header}\n"
    tag = now_tag()
    entry_lines = [f"- {tag}: {b}" for b in bullets]
    updated = content.rstrip() + "\n" + "\n".join(entry_lines) + "\n"
    return content, updated

def unified_diff(old: str, new: str, from_path: str, to_path: str):
    return "\n".join(difflib.unified_diff(
        old.splitlines(True),
        new.splitlines(True),
        fromfile=from_path,
        tofile=to_path,
        lineterm=""
    ))

def git_info(repo_root: pathlib.Path):
    """Best-effort git metadata. No hard dependency."""
    def run(cmd):
        import subprocess
        try:
            out = subprocess.check_output(cmd, cwd=str(repo_root), stderr=subprocess.DEVNULL).decode().strip()
            return out
        except Exception:
            return None

    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    commit = run(["git", "rev-parse", "HEAD"])
    dirty = None
    try:
        import subprocess
        subprocess.check_call(["git", "diff", "--quiet"], cwd=str(repo_root))
        dirty = False
    except Exception:
        dirty = True
    if branch or commit:
        return {"branch": branch or "", "commit": (commit or "")[:12], "dirty": bool(dirty)}
    return None

def emit_reflection_receipt(
    bundle_dir: pathlib.Path,
    correlation_id: str,
    session_notes_path: pathlib.Path,
    skills_root: pathlib.Path,
    mode: str,
    extracted_signals: dict,
    proposal_entries: list,
    manifest_path: pathlib.Path | None,
    outcome: str,
    errors: list | None,
):
    receipt = {
        "receiptType": "reflection_receipt",
        "receiptVersion": "v1.0",
        "receiptId": f"rr_{now_tag()}_{uuid.uuid4().hex[:6]}",
        "timestamp": iso_now(),
        "actor": {"kind": "system", "name": "aspire-reflect", "tooling": "local-script"},
        "correlationId": correlation_id,
        "session": {
            "sessionId": os.getenv("CLAUDE_SESSION_ID", f"session_{uuid.uuid4().hex[:8]}"),
            "startedAt": os.getenv("CLAUDE_SESSION_STARTED_AT", ""),
            "endedAt": iso_now(),
        },
        "inputs": {
            "sessionNotesPath": str(session_notes_path),
            "skillsRoot": str(skills_root),
            "outDir": str(bundle_dir),
            "mode": mode,
            "riskPolicy": {"highRiskFiles": sorted(list(RISK_HIGH_FILES)), "autoApplyHighRisk": False},
        },
        "analysis": {
            "extractedSignals": extracted_signals,
            "summary": f"Generated {len(proposal_entries)} proposal(s) from session notes.",
            "confidence": "medium",
        },
        "proposals": proposal_entries,
        "status": {"outcome": outcome, "errors": errors or []},
        "artifacts": {
            "manifestPath": str(manifest_path) if manifest_path else "",
            "bundleDir": str(bundle_dir),
            "hashes": {},
        },
    }

    repo_root = skills_root.parent
    gi = git_info(repo_root)
    if gi:
        receipt["session"]["repo"] = gi

    receipt["session"]["environment"] = {
        "os": platform.platform(),
        "python": platform.python_version(),
    }

    hashes = {}
    for p in bundle_dir.glob("*"):
        if p.is_file() and p.suffix in {".diff", ".json"}:
            try:
                hashes[str(p.relative_to(repo_root))] = sha256_file(p)
            except Exception:
                pass
    receipt["artifacts"]["hashes"] = hashes

    out_path = bundle_dir / "reflection-receipt.json"
    out_path.write_text(json.dumps(receipt, indent=2))
    return out_path

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skills-root", default="skills")
    ap.add_argument("--session-notes", required=True)
    ap.add_argument("--out", default="proposed")
    ap.add_argument("--apply", action="store_true", help="Apply allowed (non-high-risk) proposals")
    ap.add_argument("--correlation-id", default="", help="Optional correlation id for traceability")
    args = ap.parse_args()

    skills_root = pathlib.Path(args.skills_root).resolve()
    out_root = pathlib.Path(args.out).resolve()
    bundle_dir = out_root / f"reflect-{now_tag()}"
    bundle_dir.mkdir(parents=True, exist_ok=True)

    correlation_id = args.correlation_id.strip() or f"corr_{uuid.uuid4().hex[:10]}"

    notes_path = pathlib.Path(args.session_notes).resolve()
    notes = notes_path.read_text()
    buckets = extract_candidates(notes)

    plan = []
    if buckets["corrections"] or buckets["patterns"]:
        plan.append((skills_root/"global"/"STYLE.md", buckets["corrections"] + buckets["patterns"], "medium"))
    if buckets["approvals"] or buckets["patterns"]:
        plan.append((skills_root/"aspire"/"DEBUGGING.md", buckets["approvals"] + buckets["patterns"], "low"))
    if buckets["nevers"]:
        plan.append((skills_root/"global"/"SAFETY.md", buckets["nevers"], "high"))

    manifest = {"session_notes": str(notes_path), "generated_at": iso_now(), "proposals": []}
    proposal_entries = []
    overall_errors = []

    mode = "proposal_only"
    if args.apply:
        mode = "apply_allowed"

    for skill_file, bullets, risk in plan:
        if not bullets:
            continue
        old, new = propose_append(skill_file, bullets)

        diff_path = bundle_dir / (skill_file.name + ".diff")
        diff_text = unified_diff(old, new, f"a/{skill_file}", f"b/{skill_file}")
        diff_path.write_text(diff_text)

        proposal_id = f"p_{skill_file.stem.lower()}_{uuid.uuid4().hex[:6]}"
        confidence = "high" if risk == "low" else ("medium" if risk == "medium" else "high")
        apply_allowed = (skill_file.name not in RISK_HIGH_FILES) and (risk != "high")

        decision = {"status": "proposed"}
        if args.apply and apply_allowed:
            try:
                skill_file.write_text(new)
                decision = {"status": "applied"}
            except Exception as e:
                decision = {"status": "deferred", "reason": f"apply failed: {e}"}
                overall_errors.append({"code": "APPLY_FAILED", "message": str(e)})

        proposal = {
            "proposalId": proposal_id,
            "targetFile": str(skill_file),
            "risk": "high" if risk == "high" else ("medium" if risk == "medium" else "low"),
            "confidence": confidence,
            "diffPath": str(diff_path),
            "summary": f"Append {len(bullets)} changelog entry(ies).",
            "evidence": [{"source": "session_notes", "snippet": b[:120]} for b in bullets[:5]],
            "decision": decision,
        }
        proposal_entries.append(proposal)

        (bundle_dir / (skill_file.name + ".json")).write_text(json.dumps({
            "file": str(skill_file),
            "risk": risk,
            "confidence": confidence,
            "summary": proposal["summary"],
            "apply_allowed": apply_allowed
        }, indent=2))

        manifest["proposals"].append({
            "file": str(skill_file),
            "risk": risk,
            "confidence": confidence,
            "diff": str(diff_path),
            "apply_allowed": apply_allowed,
        })

    manifest_path = bundle_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    outcome = "success"
    if overall_errors and proposal_entries:
        outcome = "partial"
    elif overall_errors and not proposal_entries:
        outcome = "failed"

    receipt_path = emit_reflection_receipt(
        bundle_dir=bundle_dir,
        correlation_id=correlation_id,
        session_notes_path=notes_path,
        skills_root=skills_root,
        mode=mode,
        extracted_signals=buckets,
        proposal_entries=proposal_entries,
        manifest_path=manifest_path,
        outcome=outcome,
        errors=overall_errors,
    )

    print(f"Wrote proposals to: {bundle_dir}")
    print(f"Wrote reflection receipt: {receipt_path}")
    if args.apply:
        print("Applied allowed proposals (high-risk excluded).")

if __name__ == "__main__":
    main()
