#!/usr/bin/env python3

"""
Unit tests for scripts/critic.py

Covers all 5 analysis checks, verdict logic, and edge cases.
"""

import json
import pathlib
import sys
import textwrap

import pytest

# Add scripts/ to path so we can import critic
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

import critic


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_skills(tmp_path):
    """Create a minimal skills/ directory with known content."""
    skills = tmp_path / "skills"
    (skills / "global").mkdir(parents=True)
    (skills / "aspire").mkdir(parents=True)

    (skills / "global" / "STYLE.md").write_text(textwrap.dedent("""\
        # Style
        ## Rules
        - Prefer clarity over cleverness.
        - Use kebab-case for filenames.
        - Always log with structured JSON.
        ## Changelog
        - 20260101-120000: Added structured logging rule.
    """))

    (skills / "global" / "SAFETY.md").write_text(textwrap.dedent("""\
        # Safety
        ## Never do
        - Never log secrets, API keys, auth tokens, or full user PII.
        - Never disable security checks to make it work.
        - Never auto-modify governance without explicit human approval.
        ## Changelog
        - (append entries here)
    """))

    (skills / "aspire" / "DEBUGGING.md").write_text(textwrap.dedent("""\
        # Debugging
        ## Standard workflow
        - Reproduce the issue with steps and environment.
        - Collect evidence via correlationId.
        - Patch with smallest change.
        ## Changelog
        - (append entries here)
    """))

    (skills / "aspire" / "RECEIPTS.md").write_text(textwrap.dedent("""\
        # Receipts
        ## Invariants
        - Every run emits a receipt with a stable schema.
        - Receipts are append-only; no destructive edits in-place.
        ## Changelog
        - (append entries here)
    """))

    return skills


@pytest.fixture
def all_rules(tmp_skills):
    """Load all skill rules from the test skills directory."""
    return critic.load_all_skill_rules(tmp_skills)


# ---------------------------------------------------------------------------
# 1. Redundancy Check
# ---------------------------------------------------------------------------

class TestRedundancy:
    def test_redundant_proposal_flagged(self, tmp_skills):
        """A proposal that closely matches an existing rule gets high score."""
        existing_rules = critic.load_skill_rules(tmp_skills / "global" / "STYLE.md")
        existing_changelog = critic.extract_changelog_entries(tmp_skills / "global" / "STYLE.md")

        # Very similar to existing rule
        proposed = ["Prefer clarity over cleverness in all code."]
        score, matches = critic.check_redundancy(proposed, existing_rules, existing_changelog)

        assert score >= critic.REDUNDANCY_THRESHOLD
        assert len(matches) > 0
        assert matches[0]["similarity"] >= critic.REDUNDANCY_THRESHOLD

    def test_novel_proposal_passes(self, tmp_skills):
        """A genuinely new proposal gets low redundancy score."""
        existing_rules = critic.load_skill_rules(tmp_skills / "global" / "STYLE.md")
        existing_changelog = critic.extract_changelog_entries(tmp_skills / "global" / "STYLE.md")

        proposed = ["Always run linting before committing TypeScript files."]
        score, matches = critic.check_redundancy(proposed, existing_rules, existing_changelog)

        assert score < critic.REDUNDANCY_THRESHOLD
        assert len(matches) == 0

    def test_empty_existing_rules(self):
        """No existing rules means no redundancy."""
        score, matches = critic.check_redundancy(["some new rule"], [], [])
        assert score == 0.0
        assert matches == []

    def test_empty_proposed(self):
        """No proposed entries means no redundancy."""
        score, matches = critic.check_redundancy([], ["existing rule"], [])
        assert score == 0.0
        assert matches == []


# ---------------------------------------------------------------------------
# 2. Contradiction Check
# ---------------------------------------------------------------------------

class TestContradictions:
    def test_always_vs_never_detected(self, all_rules):
        """'always log secrets' contradicts SAFETY's 'never log secrets'."""
        proposed = ["Always log secrets for debugging purposes."]
        contradictions = critic.check_contradictions(
            proposed, "skills/global/STYLE.md", all_rules,
        )
        assert len(contradictions) > 0
        assert any("SAFETY.md" in c["file"] for c in contradictions)

    def test_no_contradiction_for_unrelated(self, all_rules):
        """Unrelated proposals don't trigger contradictions."""
        proposed = ["Use blue color for primary buttons in the dashboard."]
        contradictions = critic.check_contradictions(
            proposed, "skills/global/STYLE.md", all_rules,
        )
        assert len(contradictions) == 0

    def test_same_file_ignored(self, all_rules):
        """Contradictions within the same file are not flagged (not cross-file)."""
        proposed = ["Never use kebab-case for filenames."]
        contradictions = critic.check_contradictions(
            proposed, "skills/global/STYLE.md", all_rules,
        )
        # Should not flag STYLE.md against itself
        assert all(c["file"] != "STYLE.md" for c in contradictions)


# ---------------------------------------------------------------------------
# 3. Evidence Quality
# ---------------------------------------------------------------------------

class TestEvidenceQuality:
    def test_high_quality_evidence(self):
        """Specific, actionable evidence with file paths scores high."""
        evidence = [
            {"snippet": "Always use `structured JSON` for logging in backend/api/routes.ts to ensure correlation."},
            {"snippet": "Never import from internal modules — enforce boundary via eslint rule."},
        ]
        score, notes = critic.check_evidence_quality(evidence)
        assert score >= 6
        assert "specific context" in notes.lower() or "actionable" in notes.lower()

    def test_vague_evidence_scores_low(self):
        """Short, vague evidence scores poorly."""
        evidence = [
            {"snippet": "improve things"},
            {"snippet": "be better"},
        ]
        score, notes = critic.check_evidence_quality(evidence)
        assert score <= 3

    def test_no_evidence_scores_zero(self):
        """Empty evidence list returns 0."""
        score, notes = critic.check_evidence_quality([])
        assert score == 0
        assert "no evidence" in notes.lower()

    def test_moderate_evidence(self):
        """Evidence with some detail but no file paths gets moderate score."""
        evidence = [
            {"snippet": "Always validate user input before processing the request in handlers."},
        ]
        score, notes = critic.check_evidence_quality(evidence)
        assert 2 <= score <= 7


# ---------------------------------------------------------------------------
# 4. Risk Alignment
# ---------------------------------------------------------------------------

class TestRiskAlignment:
    def test_safety_high_risk_aligned(self):
        """SAFETY.md with high risk is aligned."""
        aligned, note = critic.check_risk_alignment("high", "skills/global/SAFETY.md")
        assert aligned is True

    def test_safety_low_risk_misaligned(self):
        """SAFETY.md with low risk is misaligned."""
        aligned, note = critic.check_risk_alignment("low", "skills/global/SAFETY.md")
        assert aligned is False
        assert "misaligned" in note.lower()

    def test_style_low_risk_aligned(self):
        """STYLE.md with low risk is aligned."""
        aligned, note = critic.check_risk_alignment("low", "skills/global/STYLE.md")
        assert aligned is True

    def test_style_high_risk_misaligned(self):
        """STYLE.md with high risk is misaligned."""
        aligned, note = critic.check_risk_alignment("high", "skills/global/STYLE.md")
        assert aligned is False

    def test_unknown_file_passes(self):
        """Unknown skill files pass risk alignment (no expectation)."""
        aligned, note = critic.check_risk_alignment("low", "skills/custom/NEWFILE.md")
        assert aligned is True


# ---------------------------------------------------------------------------
# 5. Actionability
# ---------------------------------------------------------------------------

class TestActionability:
    def test_highly_actionable(self):
        """Clear directive language scores high."""
        entries = [
            "Always use structured JSON for logging.",
            "Never disable linting in CI pipelines.",
            "Must include correlationId in every receipt.",
        ]
        score, vague = critic.check_actionability(entries)
        assert score >= 7
        assert len(vague) == 0

    def test_vague_language_scores_low(self):
        """Vague advisory language scores poorly."""
        entries = [
            "Consider improving the logging situation.",
            "Maybe think about better error handling.",
            "Could potentially look into performance.",
        ]
        score, vague = critic.check_actionability(entries)
        assert score <= 4
        assert len(vague) > 0

    def test_empty_entries(self):
        """No entries scores 0."""
        score, vague = critic.check_actionability([])
        assert score == 0


# ---------------------------------------------------------------------------
# Verdict Logic
# ---------------------------------------------------------------------------

class TestVerdict:
    def test_contradictions_force_reject(self):
        """Any contradiction → recommend-reject regardless of scores."""
        verdict = critic.compute_verdict(
            evidence_score=10,
            actionability_score=10,
            redundancy_score=0.0,
            contradictions=[{"file": "SAFETY.md", "rule": "test", "explanation": "conflict"}],
        )
        assert verdict == "recommend-reject"

    def test_high_redundancy_forces_skip(self):
        """Redundancy ≥ threshold → recommend-skip."""
        verdict = critic.compute_verdict(
            evidence_score=10,
            actionability_score=10,
            redundancy_score=0.85,
            contradictions=[],
        )
        assert verdict == "recommend-skip"

    def test_high_scores_recommend_merge(self):
        """High total (≥14) with no issues → recommend-merge."""
        verdict = critic.compute_verdict(
            evidence_score=8,
            actionability_score=8,
            redundancy_score=0.1,
            contradictions=[],
        )
        assert verdict == "recommend-merge"

    def test_moderate_scores_needs_review(self):
        """Moderate total (8-13) → needs-review."""
        verdict = critic.compute_verdict(
            evidence_score=5,
            actionability_score=5,
            redundancy_score=0.1,
            contradictions=[],
        )
        assert verdict == "needs-review"

    def test_low_scores_recommend_reject(self):
        """Low total (<8) → recommend-reject."""
        verdict = critic.compute_verdict(
            evidence_score=2,
            actionability_score=2,
            redundancy_score=0.1,
            contradictions=[],
        )
        assert verdict == "recommend-reject"

    def test_contradiction_takes_priority_over_redundancy(self):
        """Contradiction check runs before redundancy check."""
        verdict = critic.compute_verdict(
            evidence_score=10,
            actionability_score=10,
            redundancy_score=0.90,
            contradictions=[{"file": "X", "rule": "Y", "explanation": "Z"}],
        )
        assert verdict == "recommend-reject"


# ---------------------------------------------------------------------------
# Integration: review_proposal
# ---------------------------------------------------------------------------

class TestReviewProposal:
    def test_strong_proposal_gets_merge(self, tmp_skills, all_rules):
        """A well-evidenced, actionable, non-redundant proposal → recommend-merge."""
        proposal = {
            "proposalId": "p_style_abc123",
            "targetFile": str(tmp_skills / "global" / "STYLE.md"),
            "risk": "medium",
            "evidence": [
                {"source": "session_notes", "snippet": "Always use `prettier --write` before committing TypeScript to enforce consistent formatting."},
                {"source": "session_notes", "snippet": "Never mix tabs and spaces in .ts files — enforce via editorconfig."},
            ],
        }
        review = critic.review_proposal(proposal, tmp_skills, all_rules)

        assert review["proposalId"] == "p_style_abc123"
        assert review["verdict"] in ("recommend-merge", "needs-review")
        assert review["scores"]["evidence"] >= 5
        assert review["riskAligned"] is True

    def test_vague_proposal_gets_rejected(self, tmp_skills, all_rules):
        """Vague, low-quality proposal → recommend-reject."""
        proposal = {
            "proposalId": "p_style_def456",
            "targetFile": str(tmp_skills / "global" / "STYLE.md"),
            "risk": "medium",
            "evidence": [
                {"source": "session_notes", "snippet": "maybe improve"},
            ],
        }
        review = critic.review_proposal(proposal, tmp_skills, all_rules)

        assert review["verdict"] == "recommend-reject"
        assert review["scores"]["total"] < 8

    def test_redundant_proposal_gets_skip(self, tmp_skills, all_rules):
        """Proposal matching existing rule → recommend-skip."""
        proposal = {
            "proposalId": "p_style_dup789",
            "targetFile": str(tmp_skills / "global" / "STYLE.md"),
            "risk": "medium",
            "evidence": [
                {"source": "session_notes", "snippet": "Prefer clarity over cleverness."},
            ],
        }
        review = critic.review_proposal(proposal, tmp_skills, all_rules)

        assert review["redundant"] is True
        assert review["verdict"] == "recommend-skip"


# ---------------------------------------------------------------------------
# Edge Cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_no_evidence_in_proposal(self, tmp_skills, all_rules):
        """Proposal with empty evidence list → low scores, recommend-reject."""
        proposal = {
            "proposalId": "p_debug_empty",
            "targetFile": str(tmp_skills / "aspire" / "DEBUGGING.md"),
            "risk": "low",
            "evidence": [],
        }
        review = critic.review_proposal(proposal, tmp_skills, all_rules)

        assert review["scores"]["evidence"] == 0
        assert review["verdict"] == "recommend-reject"

    def test_risk_misalignment_noted(self, tmp_skills, all_rules):
        """SAFETY.md with low risk gets flagged."""
        proposal = {
            "proposalId": "p_safety_lowrisk",
            "targetFile": str(tmp_skills / "global" / "SAFETY.md"),
            "risk": "low",
            "evidence": [
                {"source": "session_notes", "snippet": "Always validate auth tokens before processing."},
            ],
        }
        review = critic.review_proposal(proposal, tmp_skills, all_rules)

        assert review["riskAligned"] is False
        assert "misaligned" in review["riskNote"].lower()


# ---------------------------------------------------------------------------
# CLI / Bundle Integration
# ---------------------------------------------------------------------------

class TestBundleIntegration:
    def test_find_latest_bundle(self, tmp_path):
        """find_latest_bundle picks the most recent bundle with a manifest."""
        proposed = tmp_path / "proposed"
        proposed.mkdir()

        # Older bundle (no manifest)
        old = proposed / "reflect-20260101-100000"
        old.mkdir()

        # Newer bundle (with manifest)
        new = proposed / "reflect-20260206-140000"
        new.mkdir()
        (new / "manifest.json").write_text("{}")

        result = critic.find_latest_bundle(proposed)
        assert result == new

    def test_find_latest_bundle_empty(self, tmp_path):
        """No bundles → returns None."""
        proposed = tmp_path / "proposed"
        proposed.mkdir()

        result = critic.find_latest_bundle(proposed)
        assert result is None

    def test_full_critic_run(self, tmp_skills, tmp_path):
        """End-to-end: create a bundle, run the critic, verify report."""
        bundle = tmp_path / "proposed" / "reflect-20260206-143000"
        bundle.mkdir(parents=True)

        # Minimal manifest
        manifest = {
            "session_notes": "test",
            "generated_at": "2026-02-06T14:30:00",
            "proposals": [
                {
                    "file": str(tmp_skills / "global" / "STYLE.md"),
                    "risk": "medium",
                    "confidence": "medium",
                    "diff": "STYLE.md.diff",
                    "apply_allowed": True,
                },
            ],
        }
        (bundle / "manifest.json").write_text(json.dumps(manifest))

        # Reflection receipt with richer proposal data
        receipt = {
            "proposals": [
                {
                    "proposalId": "p_style_test01",
                    "targetFile": str(tmp_skills / "global" / "STYLE.md"),
                    "risk": "medium",
                    "evidence": [
                        {"source": "session_notes", "snippet": "Always use `prettier` to format code before committing to ensure consistency."},
                    ],
                },
            ],
        }
        (bundle / "reflection-receipt.json").write_text(json.dumps(receipt))

        # Run critic
        import subprocess
        result = subprocess.run(
            [
                sys.executable,
                str(pathlib.Path(__file__).resolve().parents[2] / "scripts" / "critic.py"),
                "--bundle", str(bundle),
                "--skills-root", str(tmp_skills),
            ],
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert "Critic report" in result.stdout

        # Verify report
        report_path = bundle / "critic-report.json"
        assert report_path.exists()

        report = json.loads(report_path.read_text())
        assert report["criticVersion"] == "v1.0"
        assert report["proposalsReviewed"] == 1
        assert len(report["reviews"]) == 1
        assert report["reviews"][0]["proposalId"] == "p_style_test01"
        assert report["reviews"][0]["verdict"] in (
            "recommend-merge", "needs-review", "recommend-reject", "recommend-skip",
        )
