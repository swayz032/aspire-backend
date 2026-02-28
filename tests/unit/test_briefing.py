#!/usr/bin/env python3

"""
Unit tests for scripts/briefing.py

Covers: rule extraction, placeholder filtering, critic report parsing,
missing files handling, latest bundle selection, full briefing generation.
"""

import json
import pathlib
import sys
import textwrap

import pytest

# Add scripts/ to path so we can import briefing
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "scripts"))

import briefing


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_skills(tmp_path):
    """Create a minimal skills/ directory with known content."""
    skills = tmp_path / "skills"
    (skills / "global").mkdir(parents=True)
    (skills / "aspire").mkdir(parents=True)

    (skills / "global" / "SAFETY.md").write_text(textwrap.dedent("""\
        # Safety Skill

        ## Never do
        - Never log secrets, API keys, auth tokens, or full user PII.
        - Never disable security checks to "make it work."
        - Never auto-modify governance without explicit human approval.

        ## Data handling
        - Minimize data collected.
        - Redact PII in logs, receipts, and debug traces.

        ## Changelog
        - (append entries here)
    """), encoding="utf-8")

    (skills / "global" / "STYLE.md").write_text(textwrap.dedent("""\
        # Style Skill

        ## Non-negotiables
        - Prefer clarity over cleverness.
        - No silent behavior changes: explain intent in the smallest number of words necessary.

        ## Naming
        - Use `kebab-case` for filenames, `PascalCase` for React components.

        ## Changelog
        - (append entries here)
    """), encoding="utf-8")

    (skills / "aspire" / "DEBUGGING.md").write_text(textwrap.dedent("""\
        # Debugging Skill

        ## Repeat-correction rule
        - If a correction is made twice, add it to the relevant skill file.

        ## Changelog
        - (append entries here)
    """), encoding="utf-8")

    (skills / "aspire" / "RECEIPTS.md").write_text(textwrap.dedent("""\
        # Receipts Skill

        ## Receipt invariants
        - Every run emits a receipt with a stable schema.
        - Every receipt includes a correlationId.
        - Receipts are append-only; no destructive edits in-place.

        ## Changelog
        - (append entries here)
    """), encoding="utf-8")

    return skills


@pytest.fixture
def tmp_proposed(tmp_path):
    """Create a proposed/ directory with a critic report."""
    proposed = tmp_path / "proposed"
    proposed.mkdir()
    return proposed


def make_bundle(proposed_dir, name, reviews):
    """Helper: create a reflect-* bundle with a critic report."""
    bundle = proposed_dir / name
    bundle.mkdir(parents=True, exist_ok=True)
    report = {
        "criticVersion": "v1.0",
        "timestamp": "2026-02-06T14:30:00Z",
        "bundleDir": str(bundle),
        "proposalsReviewed": len(reviews),
        "summary": {},
        "reviews": reviews,
    }
    (bundle / "critic-report.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8",
    )
    return bundle


# ---------------------------------------------------------------------------
# extract_rules
# ---------------------------------------------------------------------------

class TestExtractRules:
    """Tests for briefing.extract_rules()."""

    def test_extracts_all_bullet_rules(self, tmp_skills):
        """Rules from all sections are captured, not just Changelog."""
        rules = briefing.extract_rules(tmp_skills / "global" / "SAFETY.md")
        assert len(rules) == 5
        assert "Never log secrets, API keys, auth tokens, or full user PII." in rules
        assert "Minimize data collected." in rules
        assert "Redact PII in logs, receipts, and debug traces." in rules

    def test_skips_placeholders(self, tmp_skills):
        """Placeholder lines like '(append entries here)' are filtered."""
        rules = briefing.extract_rules(tmp_skills / "global" / "SAFETY.md")
        for rule in rules:
            assert "append entries here" not in rule.lower()

    def test_skips_short_bullets(self, tmp_path):
        """Bullets shorter than 4 chars are skipped."""
        f = tmp_path / "short.md"
        f.write_text("- ab\n- abcd\n- A real rule here.\n", encoding="utf-8")
        rules = briefing.extract_rules(f)
        assert len(rules) == 2
        assert "abcd" in rules
        assert "A real rule here." in rules

    def test_missing_file_returns_empty(self, tmp_path):
        """Non-existent file returns empty list, no crash."""
        rules = briefing.extract_rules(tmp_path / "nonexistent.md")
        assert rules == []

    def test_empty_file_returns_empty(self, tmp_path):
        """Empty file returns empty list."""
        f = tmp_path / "empty.md"
        f.write_text("", encoding="utf-8")
        rules = briefing.extract_rules(f)
        assert rules == []

    def test_file_with_only_placeholders(self, tmp_path):
        """File with only placeholder bullets returns empty list."""
        f = tmp_path / "placeholders.md"
        f.write_text(textwrap.dedent("""\
            # Skill

            ## Changelog
            - (append entries here)
            - (add new rules here)
            - (TBD)
            - (todo: fill in)
        """), encoding="utf-8")
        rules = briefing.extract_rules(f)
        assert rules == []

    def test_various_placeholder_patterns(self, tmp_path):
        """Multiple placeholder patterns are all filtered."""
        f = tmp_path / "mixed.md"
        f.write_text(textwrap.dedent("""\
            # Skill
            - (append entries here)
            - (Add new items here)
            - (PLACEHOLDER for future rules)
            - (TODO: define rules)
            - (TBD)
            - (fixme: needs content)
            - This is a real rule that should be kept.
        """), encoding="utf-8")
        rules = briefing.extract_rules(f)
        assert rules == ["This is a real rule that should be kept."]


# ---------------------------------------------------------------------------
# find_latest_bundle
# ---------------------------------------------------------------------------

class TestFindLatestBundle:
    """Tests for briefing.find_latest_bundle()."""

    def test_finds_latest_by_sort_order(self, tmp_proposed):
        """Latest bundle (by name sort) is returned."""
        make_bundle(tmp_proposed, "reflect-20260205-100000", [
            {"verdict": "needs-review", "targetFile": "STYLE.md",
             "scores": {"total": 12}, "reasoning": "Old bundle."},
        ])
        latest = make_bundle(tmp_proposed, "reflect-20260206-143000", [
            {"verdict": "recommend-merge", "targetFile": "SAFETY.md",
             "scores": {"total": 17}, "reasoning": "Latest bundle."},
        ])
        result = briefing.find_latest_bundle(tmp_proposed)
        assert result == latest

    def test_returns_none_for_empty_dir(self, tmp_proposed):
        """Empty proposed/ returns None."""
        result = briefing.find_latest_bundle(tmp_proposed)
        assert result is None

    def test_returns_none_for_missing_dir(self, tmp_path):
        """Non-existent directory returns None."""
        result = briefing.find_latest_bundle(tmp_path / "nope")
        assert result is None

    def test_skips_bundles_without_critic_report(self, tmp_proposed):
        """Bundles without critic-report.json are skipped."""
        # Bundle with manifest but no critic report
        incomplete = tmp_proposed / "reflect-20260206-200000"
        incomplete.mkdir()
        (incomplete / "manifest.json").write_text("{}", encoding="utf-8")

        # Bundle with critic report
        complete = make_bundle(tmp_proposed, "reflect-20260205-100000", [
            {"verdict": "needs-review", "targetFile": "STYLE.md",
             "scores": {"total": 12}, "reasoning": "Has report."},
        ])

        result = briefing.find_latest_bundle(tmp_proposed)
        assert result == complete


# ---------------------------------------------------------------------------
# load_critic_report
# ---------------------------------------------------------------------------

class TestLoadCriticReport:
    """Tests for briefing.load_critic_report()."""

    def test_loads_valid_report(self, tmp_proposed):
        """Valid critic-report.json is loaded correctly."""
        bundle = make_bundle(tmp_proposed, "reflect-20260206-143000", [
            {"verdict": "recommend-merge", "targetFile": "SAFETY.md",
             "scores": {"total": 17}, "reasoning": "Good."},
        ])
        report = briefing.load_critic_report(bundle)
        assert report is not None
        assert report["proposalsReviewed"] == 1
        assert report["reviews"][0]["verdict"] == "recommend-merge"

    def test_returns_none_for_missing_report(self, tmp_path):
        """Missing critic-report.json returns None."""
        bundle = tmp_path / "reflect-fake"
        bundle.mkdir()
        assert briefing.load_critic_report(bundle) is None

    def test_returns_none_for_malformed_json(self, tmp_path):
        """Malformed JSON returns None, no crash."""
        bundle = tmp_path / "reflect-bad"
        bundle.mkdir()
        (bundle / "critic-report.json").write_text("{{invalid json", encoding="utf-8")
        assert briefing.load_critic_report(bundle) is None


# ---------------------------------------------------------------------------
# format_skills_section
# ---------------------------------------------------------------------------

class TestFormatSkillsSection:
    """Tests for briefing.format_skills_section()."""

    def test_includes_all_skill_files(self, tmp_skills):
        """All 4 skill files appear in output."""
        output = briefing.format_skills_section(tmp_skills)
        assert "SAFETY (hard constraints):" in output
        assert "STYLE (conventions):" in output
        assert "DEBUGGING (workflow):" in output
        assert "RECEIPTS (invariants):" in output

    def test_includes_header(self, tmp_skills):
        """The skills section has the expected header."""
        output = briefing.format_skills_section(tmp_skills)
        assert "=== LEARNED SKILL RULES (ACTIVE" in output

    def test_includes_rule_content(self, tmp_skills):
        """Actual rule text appears in output."""
        output = briefing.format_skills_section(tmp_skills)
        assert "Never log secrets" in output
        assert "Prefer clarity over cleverness" in output
        assert "Receipts are append-only" in output

    def test_no_placeholders_in_output(self, tmp_skills):
        """Placeholder lines don't appear in output."""
        output = briefing.format_skills_section(tmp_skills)
        assert "append entries here" not in output

    def test_missing_skills_root_returns_empty(self, tmp_path):
        """Non-existent skills root returns empty string."""
        output = briefing.format_skills_section(tmp_path / "nope")
        assert output == ""

    def test_empty_skills_returns_empty(self, tmp_path):
        """Skills dir with only placeholder files returns empty."""
        skills = tmp_path / "skills"
        (skills / "global").mkdir(parents=True)
        (skills / "aspire").mkdir(parents=True)
        (skills / "global" / "SAFETY.md").write_text(
            "# Safety\n## Changelog\n- (append entries here)\n", encoding="utf-8",
        )
        (skills / "global" / "STYLE.md").write_text(
            "# Style\n## Changelog\n- (append entries here)\n", encoding="utf-8",
        )
        (skills / "aspire" / "DEBUGGING.md").write_text(
            "# Debug\n## Changelog\n- (append entries here)\n", encoding="utf-8",
        )
        (skills / "aspire" / "RECEIPTS.md").write_text(
            "# Receipts\n## Changelog\n- (append entries here)\n", encoding="utf-8",
        )
        output = briefing.format_skills_section(skills)
        assert output == ""


# ---------------------------------------------------------------------------
# format_proposals_section
# ---------------------------------------------------------------------------

class TestFormatProposalsSection:
    """Tests for briefing.format_proposals_section()."""

    def test_shows_verdicts_and_scores(self, tmp_proposed):
        """Verdicts and scores from critic report appear."""
        make_bundle(tmp_proposed, "reflect-20260206-143000", [
            {"verdict": "recommend-merge", "targetFile": "skills/global/STYLE.md",
             "scores": {"total": 17}, "reasoning": "Clear, actionable style rule."},
            {"verdict": "needs-review", "targetFile": "skills/aspire/DEBUGGING.md",
             "scores": {"total": 11}, "reasoning": "Moderate quality."},
            {"verdict": "recommend-reject", "targetFile": "skills/global/SAFETY.md",
             "scores": {"total": 5}, "reasoning": "Evidence too vague."},
        ])
        output = briefing.format_proposals_section(tmp_proposed)
        assert "PENDING PROPOSALS" in output
        assert "recommend-merge" in output
        assert "needs-review" in output
        assert "recommend-reject" in output
        assert "17/20" in output

    def test_shows_bundle_name(self, tmp_proposed):
        """Bundle directory name appears in output."""
        make_bundle(tmp_proposed, "reflect-20260206-143000", [
            {"verdict": "recommend-merge", "targetFile": "STYLE.md",
             "scores": {"total": 17}, "reasoning": "Good."},
        ])
        output = briefing.format_proposals_section(tmp_proposed)
        assert "reflect-20260206-143000" in output

    def test_no_bundles_returns_empty(self, tmp_proposed):
        """Empty proposed/ returns empty string."""
        output = briefing.format_proposals_section(tmp_proposed)
        assert output == ""

    def test_missing_dir_returns_empty(self, tmp_path):
        """Non-existent proposed/ returns empty string."""
        output = briefing.format_proposals_section(tmp_path / "nope")
        assert output == ""

    def test_empty_reviews_returns_empty(self, tmp_proposed):
        """Bundle with no reviews returns empty string."""
        make_bundle(tmp_proposed, "reflect-20260206-143000", [])
        output = briefing.format_proposals_section(tmp_proposed)
        assert output == ""


# ---------------------------------------------------------------------------
# format_reminders
# ---------------------------------------------------------------------------

class TestFormatReminders:
    """Tests for briefing.format_reminders()."""

    def test_includes_session_notes_instruction(self):
        """Reminds about session-notes.md."""
        output = briefing.format_reminders()
        assert "session-notes.md" in output

    def test_includes_format_hints(self):
        """Includes the Correction/Pattern/Never/Approval format."""
        output = briefing.format_reminders()
        assert "Correction:" in output
        assert "Pattern:" in output
        assert "Never:" in output

    def test_has_header(self):
        """Has the SESSION REMINDERS header."""
        output = briefing.format_reminders()
        assert "=== SESSION REMINDERS ===" in output


# ---------------------------------------------------------------------------
# generate_briefing (integration)
# ---------------------------------------------------------------------------

class TestGenerateBriefing:
    """Integration tests for briefing.generate_briefing()."""

    def test_full_briefing_with_skills_and_proposals(self, tmp_skills, tmp_proposed):
        """Full briefing includes skills, proposals, and reminders."""
        make_bundle(tmp_proposed, "reflect-20260206-143000", [
            {"verdict": "recommend-merge", "targetFile": "STYLE.md",
             "scores": {"total": 17}, "reasoning": "Clear rule."},
        ])
        output = briefing.generate_briefing(tmp_skills, tmp_proposed)
        assert "LEARNED SKILL RULES" in output
        assert "PENDING PROPOSALS" in output
        assert "SESSION REMINDERS" in output

    def test_briefing_without_proposals(self, tmp_skills, tmp_proposed):
        """Briefing without proposals still shows skills and reminders."""
        output = briefing.generate_briefing(tmp_skills, tmp_proposed)
        assert "LEARNED SKILL RULES" in output
        assert "PENDING PROPOSALS" not in output
        assert "SESSION REMINDERS" in output

    def test_briefing_with_missing_everything(self, tmp_path):
        """Missing skills and proposed dirs produce minimal output."""
        output = briefing.generate_briefing(
            tmp_path / "no-skills", tmp_path / "no-proposed",
        )
        # Should still have reminders at minimum
        assert "SESSION REMINDERS" in output
        # But no skills or proposals
        assert "LEARNED SKILL RULES" not in output
        assert "PENDING PROPOSALS" not in output

    def test_briefing_is_compact(self, tmp_skills, tmp_proposed):
        """Briefing stays under 3000 chars (compact for context injection)."""
        make_bundle(tmp_proposed, "reflect-20260206-143000", [
            {"verdict": "recommend-merge", "targetFile": "STYLE.md",
             "scores": {"total": 17}, "reasoning": "Clear rule."},
            {"verdict": "needs-review", "targetFile": "DEBUGGING.md",
             "scores": {"total": 11}, "reasoning": "Moderate."},
        ])
        output = briefing.generate_briefing(tmp_skills, tmp_proposed)
        assert len(output) < 3000, f"Briefing too long: {len(output)} chars"


# ---------------------------------------------------------------------------
# CLI (main)
# ---------------------------------------------------------------------------

class TestMain:
    """Tests for the CLI entry point."""

    def test_main_with_valid_args(self, tmp_skills, tmp_proposed, capsys):
        """main() prints output to stdout."""
        sys.argv = [
            "briefing.py",
            "--skills-root", str(tmp_skills),
            "--proposed", str(tmp_proposed),
        ]
        briefing.main()
        captured = capsys.readouterr()
        assert "LEARNED SKILL RULES" in captured.out
        assert "SESSION REMINDERS" in captured.out

    def test_main_with_missing_dirs(self, tmp_path, capsys):
        """main() with missing dirs still outputs reminders."""
        sys.argv = [
            "briefing.py",
            "--skills-root", str(tmp_path / "nope"),
            "--proposed", str(tmp_path / "nope2"),
        ]
        briefing.main()
        captured = capsys.readouterr()
        assert "SESSION REMINDERS" in captured.out
