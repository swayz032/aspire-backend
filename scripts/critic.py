#!/usr/bin/env python3

"""
Aspire Post-Proposal Critic

Reads a proposal bundle from proposed/reflect-*/, compares each proposal
against existing skills/ files, and outputs critic-report.json into the
same bundle directory.

This is a tool, not a brain (Law 7). It performs deterministic analysis
and reports findings. It never auto-applies or auto-rejects.
If it can't analyze, proposals proceed to manual review unchanged.
"""

import argparse
import difflib
import json
import pathlib
import re
import datetime


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

REDUNDANCY_THRESHOLD = 0.70

VAGUE_PATTERNS = re.compile(
    r"\b(improve|better|consider|maybe|think about|could be|might|should look"
    r"|look into|possibly|potentially|try to)\b",
    re.IGNORECASE,
)

ACTIONABLE_PATTERNS = re.compile(
    r"\b(always|never|use .+ instead of|require|must|add .+ to|do not|don't"
    r"|prefer .+ over|ensure|enforce|include|exclude)\b",
    re.IGNORECASE,
)

# Expected risk tiers by skill filename
EXPECTED_RISK = {
    "SAFETY.md": {"high"},
    "RECEIPTS.md": {"high"},
    "STYLE.md": {"low", "medium"},
    "DEBUGGING.md": {"low", "medium"},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def iso_now() -> str:
    return datetime.datetime.now().astimezone().isoformat()


def load_skill_rules(skill_path: pathlib.Path) -> list[str]:
    """Extract individual rules/bullets from a skill file."""
    if not skill_path.exists():
        return []
    lines = skill_path.read_text(encoding="utf-8").splitlines()
    rules: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("- ") and len(stripped) > 4:
            rules.append(stripped[2:])
    return rules


def extract_changelog_entries(skill_path: pathlib.Path) -> list[str]:
    """Extract existing changelog entries from a skill file."""
    if not skill_path.exists():
        return []
    text = skill_path.read_text(encoding="utf-8")
    in_changelog = False
    entries: list[str] = []
    for line in text.splitlines():
        if line.strip().startswith("## Changelog"):
            in_changelog = True
            continue
        if in_changelog and line.strip().startswith("## "):
            break
        if in_changelog and line.strip().startswith("- "):
            entry = line.strip()[2:]
            if entry and entry != "(append entries here)":
                entries.append(entry)
    return entries


def extract_proposed_entries(evidence_list: list[dict]) -> list[str]:
    """Pull snippet text from proposal evidence entries."""
    return [e.get("snippet", "") for e in evidence_list if e.get("snippet")]


# ---------------------------------------------------------------------------
# Analysis Checks
# ---------------------------------------------------------------------------

def check_redundancy(
    proposed_entries: list[str],
    existing_rules: list[str],
    existing_changelog: list[str],
) -> tuple[float, list[dict]]:
    """Check if proposed entries are redundant against existing content.

    Returns (max_redundancy_score, list of matches).
    """
    all_existing = existing_rules + existing_changelog
    if not all_existing or not proposed_entries:
        return 0.0, []

    max_score = 0.0
    matches: list[dict] = []

    for proposed in proposed_entries:
        for existing in all_existing:
            ratio = difflib.SequenceMatcher(None, proposed.lower(), existing.lower()).ratio()
            if ratio >= REDUNDANCY_THRESHOLD:
                matches.append({
                    "proposed": proposed[:120],
                    "existing": existing[:120],
                    "similarity": round(ratio, 3),
                })
            max_score = max(max_score, ratio)

    return round(max_score, 3), matches


def check_contradictions(
    proposed_entries: list[str],
    target_file: str,
    all_skill_rules: dict[str, list[str]],
) -> list[dict]:
    """Check if proposed entries contradict rules in OTHER skill files.

    Simple: detect negation patterns (always/never mismatch).
    """
    contradictions: list[dict] = []
    target_name = pathlib.Path(target_file).name

    # Negation pairs to check
    negation_pairs = [
        (r"\balways\b", r"\bnever\b"),
        (r"\bmust\b", r"\bmust not\b"),
        (r"\bdo not\b", r"\bdo\b"),
        (r"\brequire\b", r"\bavoid\b"),
        (r"\binclude\b", r"\bexclude\b"),
    ]

    for proposed in proposed_entries:
        proposed_lower = proposed.lower()
        # Extract key terms (nouns/verbs, 4+ chars) from proposal
        proposed_terms = set(
            w for w in re.findall(r"\b\w{4,}\b", proposed_lower)
            if w not in {"this", "that", "with", "from", "have", "been", "will", "should"}
        )

        for skill_name, rules in all_skill_rules.items():
            if skill_name == target_name:
                continue
            for rule in rules:
                rule_lower = rule.lower()
                # Check term overlap first (must share at least one key term)
                rule_terms = set(
                    w for w in re.findall(r"\b\w{4,}\b", rule_lower)
                    if w not in {"this", "that", "with", "from", "have", "been", "will", "should"}
                )
                overlap = proposed_terms & rule_terms
                if not overlap:
                    continue

                # Check negation patterns
                for pos_pat, neg_pat in negation_pairs:
                    proposed_has_pos = bool(re.search(pos_pat, proposed_lower))
                    proposed_has_neg = bool(re.search(neg_pat, proposed_lower))
                    rule_has_pos = bool(re.search(pos_pat, rule_lower))
                    rule_has_neg = bool(re.search(neg_pat, rule_lower))

                    if (proposed_has_pos and rule_has_neg) or (proposed_has_neg and rule_has_pos):
                        contradictions.append({
                            "file": skill_name,
                            "rule": rule[:120],
                            "explanation": (
                                f"Proposed entry may contradict existing rule in {skill_name}. "
                                f"Overlapping terms: {', '.join(sorted(overlap)[:5])}. "
                                f"Negation mismatch detected."
                            ),
                        })
                        break  # One contradiction per rule is enough

    return contradictions


def check_evidence_quality(evidence_list: list[dict]) -> tuple[int, str]:
    """Score evidence snippets on a 0-10 scale.

    - Has specific context (file path, error message, command)? → +3
    - Is actionable (clear do/don't guidance)? → +3
    - Length > 40 chars (not too vague)? → +2
    - Length > 10 chars (not empty)? → +1
    - Extremely short (< 10 chars)? → +0
    """
    if not evidence_list:
        return 0, "No evidence snippets provided."

    total = 0
    notes_parts: list[str] = []

    snippets = [e.get("snippet", "") for e in evidence_list]
    avg_len = sum(len(s) for s in snippets) / len(snippets) if snippets else 0

    # Specificity: file paths, error messages, commands
    specificity_patterns = re.compile(
        r"([\w/\\]+\.\w{1,5}|error|exception|traceback|failed|`[^`]+`|--\w+|\$\w+)",
        re.IGNORECASE,
    )
    has_specificity = any(specificity_patterns.search(s) for s in snippets)
    if has_specificity:
        total += 3
        notes_parts.append("Has specific context (paths/errors/commands).")
    else:
        notes_parts.append("Lacks specific context.")

    # Actionability
    has_actionable = any(ACTIONABLE_PATTERNS.search(s) for s in snippets)
    if has_actionable:
        total += 3
        notes_parts.append("Contains actionable guidance.")
    else:
        notes_parts.append("Lacks actionable guidance.")

    # Length scoring
    if avg_len > 40:
        total += 2
        notes_parts.append(f"Good detail (avg {avg_len:.0f} chars).")
    elif avg_len > 10:
        total += 1
        notes_parts.append(f"Moderate detail (avg {avg_len:.0f} chars).")
    else:
        notes_parts.append(f"Very short snippets (avg {avg_len:.0f} chars).")

    # Cap at 10 (though max from above is 8, future-proof)
    total = min(total, 10)
    return total, " ".join(notes_parts)


def check_risk_alignment(risk_tier: str, target_file: str) -> tuple[bool, str]:
    """Verify the risk tier assigned by reflect.py matches expectations."""
    filename = pathlib.Path(target_file).name
    expected = EXPECTED_RISK.get(filename)

    if expected is None:
        return True, f"No risk expectation defined for {filename}."

    if risk_tier in expected:
        return True, f"Risk '{risk_tier}' is appropriate for {filename}."

    return False, (
        f"Risk '{risk_tier}' may be misaligned for {filename}. "
        f"Expected: {', '.join(sorted(expected))}."
    )


def check_actionability(proposed_entries: list[str]) -> tuple[int, list[str]]:
    """Score actionability on a 0-10 scale.

    Penalize vague language, reward actionable language.
    """
    if not proposed_entries:
        return 0, ["No proposed entries to evaluate."]

    vague_found: list[str] = []
    actionable_count = 0

    for entry in proposed_entries:
        vague_matches = VAGUE_PATTERNS.findall(entry)
        vague_found.extend(vague_matches)
        if ACTIONABLE_PATTERNS.search(entry):
            actionable_count += 1

    # Base score: proportion of entries that are actionable (0-5)
    actionable_ratio = actionable_count / len(proposed_entries)
    base_score = round(actionable_ratio * 5)

    # Bonus for strong actionable language (0-3)
    strong_patterns = re.compile(r"\b(always|never|must|require|enforce)\b", re.IGNORECASE)
    strong_count = sum(1 for e in proposed_entries if strong_patterns.search(e))
    strong_bonus = min(strong_count, 3)

    # Penalty for vague language (0-3)
    vague_penalty = min(len(vague_found), 3)

    score = max(0, min(10, base_score + strong_bonus + 2 - vague_penalty))
    return score, list(set(vague_found))


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

def compute_verdict(
    evidence_score: int,
    actionability_score: int,
    redundancy_score: float,
    contradictions: list[dict],
) -> str:
    """Determine verdict based on combined scores.

    TOTAL = evidence_score + actionability_score (0-20 scale)

    if contradictions exist → "recommend-reject"
    if redundancy_score ≥ 0.70 → "recommend-skip"
    if TOTAL ≥ 14 AND no contradictions AND not redundant → "recommend-merge"
    if TOTAL ≥ 8 → "needs-review"
    else → "recommend-reject"
    """
    total = evidence_score + actionability_score

    if contradictions:
        return "recommend-reject"
    if redundancy_score >= REDUNDANCY_THRESHOLD:
        return "recommend-skip"
    if total >= 14:
        return "recommend-merge"
    if total >= 8:
        return "needs-review"
    return "recommend-reject"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_all_skill_rules(skills_root: pathlib.Path) -> dict[str, list[str]]:
    """Load rules from all known skill files."""
    result: dict[str, list[str]] = {}
    for md_file in skills_root.rglob("*.md"):
        result[md_file.name] = load_skill_rules(md_file)
    return result


def review_proposal(
    proposal: dict,
    skills_root: pathlib.Path,
    all_skill_rules: dict[str, list[str]],
) -> dict:
    """Run all 5 checks on a single proposal and return the review."""
    target_file = proposal.get("targetFile", "")
    target_path = pathlib.Path(target_file)
    target_name = target_path.name
    risk_tier = proposal.get("risk", "medium")
    evidence_list = proposal.get("evidence", [])
    proposed_entries = extract_proposed_entries(evidence_list)

    # Existing content for the target file
    existing_rules = load_skill_rules(target_path)
    existing_changelog = extract_changelog_entries(target_path)

    # 1. Redundancy
    redundancy_score, matching_rules = check_redundancy(
        proposed_entries, existing_rules, existing_changelog,
    )

    # 2. Contradictions
    contradictions = check_contradictions(
        proposed_entries, target_file, all_skill_rules,
    )

    # 3. Evidence quality
    evidence_score, evidence_notes = check_evidence_quality(evidence_list)

    # 4. Risk alignment
    risk_aligned, risk_note = check_risk_alignment(risk_tier, target_file)

    # 5. Actionability
    actionability_score, vague_phrases = check_actionability(proposed_entries)

    # Verdict
    total = evidence_score + actionability_score
    verdict = compute_verdict(
        evidence_score, actionability_score, redundancy_score, contradictions,
    )

    # Build reasoning string
    reasons: list[str] = []
    if contradictions:
        reasons.append(f"Found {len(contradictions)} contradiction(s) with other skill files.")
    if redundancy_score >= REDUNDANCY_THRESHOLD:
        reasons.append(f"High redundancy ({redundancy_score:.2f}) with existing rules.")
    if evidence_score >= 6:
        reasons.append("Strong evidence with specific context.")
    elif evidence_score >= 3:
        reasons.append("Moderate evidence quality.")
    else:
        reasons.append("Weak evidence — lacks specificity or actionable guidance.")
    if actionability_score >= 7:
        reasons.append("Highly actionable language.")
    elif actionability_score >= 4:
        reasons.append("Moderately actionable.")
    else:
        reasons.append("Vague or non-actionable language.")
    if not risk_aligned:
        reasons.append(f"Risk misalignment: {risk_note}")

    return {
        "proposalId": proposal.get("proposalId", "unknown"),
        "targetFile": target_file,
        "verdict": verdict,
        "scores": {
            "evidence": evidence_score,
            "actionability": actionability_score,
            "total": total,
            "redundancy": redundancy_score,
        },
        "redundant": redundancy_score >= REDUNDANCY_THRESHOLD,
        "matchingRules": matching_rules[:5],  # Cap to avoid huge output
        "contradictions": contradictions[:5],
        "riskAligned": risk_aligned,
        "riskNote": risk_note,
        "vagueLanguage": vague_phrases[:10],
        "evidenceNotes": evidence_notes,
        "reasoning": " ".join(reasons),
    }


def find_latest_bundle(proposed_root: pathlib.Path) -> pathlib.Path | None:
    """Find the most recent reflect-* bundle directory."""
    bundles = sorted(proposed_root.glob("reflect-*"), reverse=True)
    for b in bundles:
        if b.is_dir() and (b / "manifest.json").exists():
            return b
    return None


def main():
    ap = argparse.ArgumentParser(
        description="Post-proposal critic: analyze reflect.py proposals and produce critic-report.json",
    )
    ap.add_argument(
        "--bundle",
        required=True,
        help="Path to proposal bundle directory, or 'latest' to auto-find.",
    )
    ap.add_argument("--skills-root", default="skills")
    args = ap.parse_args()

    skills_root = pathlib.Path(args.skills_root).resolve()

    # Resolve bundle path
    if args.bundle == "latest":
        proposed_root = skills_root.parent / "proposed"
        bundle_dir = find_latest_bundle(proposed_root)
        if bundle_dir is None:
            print("No proposal bundles found.")
            return
    else:
        bundle_dir = pathlib.Path(args.bundle).resolve()

    if not bundle_dir.is_dir():
        print(f"Bundle directory not found: {bundle_dir}")
        return

    manifest_path = bundle_dir / "manifest.json"
    if not manifest_path.exists():
        print(f"No manifest.json in bundle: {bundle_dir}")
        return

    # Load manifest and any proposal metadata
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # Load reflection receipt for proposal details (richer than manifest)
    receipt_path = bundle_dir / "reflection-receipt.json"
    proposals: list[dict] = []
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        proposals = receipt.get("proposals", [])
    else:
        # Fallback: build minimal proposal objects from manifest
        for entry in manifest.get("proposals", []):
            proposals.append({
                "proposalId": f"p_{pathlib.Path(entry['file']).stem.lower()}_unknown",
                "targetFile": entry["file"],
                "risk": entry.get("risk", "medium"),
                "evidence": [],
            })

    if not proposals:
        print("No proposals found in bundle.")
        return

    # Load all skill rules for cross-file checks
    all_skill_rules = load_all_skill_rules(skills_root)

    # Review each proposal
    reviews: list[dict] = []
    summary: dict[str, int] = {
        "recommend-merge": 0,
        "needs-review": 0,
        "recommend-skip": 0,
        "recommend-reject": 0,
    }

    for proposal in proposals:
        review = review_proposal(proposal, skills_root, all_skill_rules)
        reviews.append(review)
        verdict = review["verdict"]
        summary[verdict] = summary.get(verdict, 0) + 1

    # Build report
    report = {
        "criticVersion": "v1.0",
        "timestamp": iso_now(),
        "bundleDir": str(bundle_dir),
        "proposalsReviewed": len(reviews),
        "summary": summary,
        "reviews": reviews,
    }

    # Write report
    report_path = bundle_dir / "critic-report.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Critic report: {report_path}")
    print(f"  Reviewed: {len(reviews)} proposal(s)")
    for verdict, count in summary.items():
        if count > 0:
            print(f"  {verdict}: {count}")


if __name__ == "__main__":
    main()
