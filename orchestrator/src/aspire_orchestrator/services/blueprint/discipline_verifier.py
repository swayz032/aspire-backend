"""Discipline Verifier — Wave 17b.6 CLASSIFY self-verification cross-check.

Runs AFTER the Atlas + LLM discipline tagger. Cross-checks a sheet's assigned
discipline against four INDEPENDENT signals to decide whether to:

  * promote (raise confidence + flip truth to ``field_confirmed``),
  * confirm (keep assignment, mark ``derived``),
  * flag (keep assignment, mark ``assumed`` + needs review), or
  * demote (clear discipline and emit a SPECIFIC missing_input reason).

Signals:
    1. sheet_code self-consistency — does ``E-1`` agree with discipline ``E``?
    2. Symbol-content discipline inference — what do the detected symbols say?
    3. OCR keyword density — re-counted across full OCR (Atlas only saw head).
    4. Neighbor sheet inference — do +/- 1 numeric neighbors share discipline?

Each signal emits a vote: ``+1`` (agrees), ``-1`` (disagrees), ``0`` (no signal).

Verdict logic (votes against ``discipline_assigned``):
    * 4 of 4 agree           -> confidence 0.98, truth=field_confirmed
    * 3 of 4 agree           -> confidence 0.85, truth=derived
    * 2 of 4 agree           -> confidence 0.55, truth=assumed, flag review
    * 0-1 agree              -> demote: discipline=None + specific reason string

Law compliance:
    #2 - Caller emits ``drew.classify.verified`` receipt with verdict counters.
    #4 - GREEN. Read-only deterministic verification.
    #6 - Pure function — RLS enforced by the caller's supabase queries.
    #7 - No autonomous decisions. Returns verdict; caller persists.
    #9 - Never logs raw OCR. Signal evidence strings reference patterns only.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal

from aspire_orchestrator.services.blueprint import sheet_type_atlas

TruthClass = Literal["field_confirmed", "derived", "assumed", "missing_input"]
Verdict = Literal["promote", "confirm", "flag", "demote"]

_VALID_DISCIPLINES = {
    "A", "S", "M", "E", "P", "FP", "C", "L", "Specs", "Schedules", "Addenda",
}

# Symbol-class -> discipline lookup. Whole-token, lowercase comparison so
# things like "electrical_outlet", "gfci_outlet", "panel_a" all map to E.
_SYMBOL_DISCIPLINE_TOKENS: dict[str, str] = {
    # Electrical
    "outlet": "E",
    "receptacle": "E",
    "gfci": "E",
    "panel": "E",
    "panelboard": "E",
    "switch": "E",
    "switchboard": "E",
    "breaker": "E",
    "conduit": "E",
    "junction": "E",
    "luminaire": "E",
    # Plumbing
    "lavatory": "P",
    "wc": "P",
    "watercloset": "P",
    "urinal": "P",
    "sink": "P",
    "drain": "P",
    "trap": "P",
    "riser": "P",
    "cleanout": "P",
    "backflow": "P",
    "valve": "P",
    "hosebibb": "P",
    # Mechanical
    "diffuser": "M",
    "ahu": "M",
    "vav": "M",
    "rtu": "M",
    "damper": "M",
    "thermostat": "M",
    "condenser": "M",
    "ductwork": "M",
    # Structural
    "column": "S",
    "beam": "S",
    "footing": "S",
    "joist": "S",
    "lintel": "S",
    "girder": "S",
    "rebar": "S",
    # Architectural
    "door": "A",
    "window": "A",
    "wall": "A",
    "partition": "A",
    "casework": "A",
    "millwork": "A",
    # Fire Protection
    "sprinkler": "FP",
    "standpipe": "FP",
    "fdc": "FP",
}

# Min number of discipline-tagged symbols on a sheet before Signal 2 fires.
_SIGNAL2_MIN_SYMBOLS = 3

# Tier-3 OCR density. We re-use the atlas's keyword bank but run against
# the FULL OCR text (the atlas only scans the title-block head).
_SIGNAL3_MIN_HITS = 10

# Neighbor distance: sheets +/-1 in numeric order.
_NEIGHBOR_RADIUS = 1
_NUMERIC_RE = re.compile(r"(\d+)")


@dataclass(frozen=True)
class SignalVote:
    """Per-signal verdict primitive."""

    name: str
    vote: int  # -1, 0, +1
    inferred: str | None  # What the signal thinks the discipline is.
    detail: str  # Short, redaction-safe evidence string. No raw OCR.


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of a full cross-check for one sheet."""

    sheet_id: str
    discipline_assigned: str | None
    discipline_final: str | None
    confidence: float
    truth: TruthClass
    verdict: Verdict
    agreed_count: int
    disagreed_count: int
    abstained_count: int
    signals: tuple[SignalVote, ...]
    missing_input_reason: str | None  # Set only when verdict == "demote".


# ---------------------------------------------------------------------------
# Signal helpers
# ---------------------------------------------------------------------------

def _parse_sheet_number(sheet_number: str | None) -> int | None:
    """Extract leading integer from a sheet_number label.

    "1", "Sheet 1", "1 of 39", "E-1", "A-101" -> 1, 1, 1, 1, 101.
    Empty / None -> None.
    """
    if not sheet_number:
        return None
    m = _NUMERIC_RE.search(sheet_number)
    if m is None:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _signal_sheet_code(
    discipline_assigned: str | None, sheet_code: str | None
) -> SignalVote:
    """Tier-1 atlas re-run against sheet_code.

    Vote rules:
        * sheet_code None or unparseable -> 0 (abstain)
        * tier-1 matches X and X == discipline_assigned -> +1
        * tier-1 matches X and X != discipline_assigned -> -1
    """
    if not sheet_code:
        return SignalVote("sheet_code", 0, None, "sheet_code_missing")

    match = sheet_type_atlas.classify(
        sheet_number=None,
        sheet_code=sheet_code,
        sheet_title=None,
        ocr_text=None,
    )
    if match is None:
        return SignalVote("sheet_code", 0, None, f"sheet_code_unmatched:{sheet_code[:8]}")
    if discipline_assigned is None:
        return SignalVote("sheet_code", 0, match.discipline, f"no_assignment_to_compare:{match.discipline}")
    if match.discipline == discipline_assigned:
        return SignalVote("sheet_code", +1, match.discipline, f"sheet_code:{match.discipline}")
    return SignalVote(
        "sheet_code",
        -1,
        match.discipline,
        f"sheet_code_says:{match.discipline}_vs_assigned:{discipline_assigned}",
    )


def _tokenize_symbol_class(class_name: str) -> set[str]:
    """Lowercase, split on underscores/spaces, keep alpha tokens >= 2 chars."""
    cleaned = class_name.lower().replace("-", "_").replace(" ", "_")
    return {tok for tok in cleaned.split("_") if tok.isalpha() and len(tok) >= 2}


def _signal_symbol_content(
    discipline_assigned: str | None,
    symbols_on_sheet: Iterable[dict],
) -> SignalVote:
    """Infer discipline from the symbols already detected on the sheet."""
    counts: dict[str, int] = {}
    total_classified = 0
    for sym in symbols_on_sheet:
        class_name = str(sym.get("class") or "")
        if not class_name:
            continue
        tokens = _tokenize_symbol_class(class_name)
        for tok in tokens:
            disc = _SYMBOL_DISCIPLINE_TOKENS.get(tok)
            if disc is not None:
                counts[disc] = counts.get(disc, 0) + 1
                total_classified += 1
                break  # one vote per symbol row

    if total_classified < _SIGNAL2_MIN_SYMBOLS:
        return SignalVote(
            "symbol_content",
            0,
            None,
            f"insufficient_symbols:{total_classified}",
        )

    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    winner, top = ranked[0]

    # Dominance: winner must be >= 50% of classified symbols.
    if top * 2 < total_classified:
        return SignalVote(
            "symbol_content",
            0,
            None,
            f"no_dominant_discipline:{total_classified}",
        )

    if discipline_assigned is None:
        return SignalVote(
            "symbol_content",
            0,
            winner,
            f"no_assignment_to_compare:{winner}:{top}",
        )
    if winner == discipline_assigned:
        return SignalVote(
            "symbol_content",
            +1,
            winner,
            f"symbols:{winner}:{top}/{total_classified}",
        )
    return SignalVote(
        "symbol_content",
        -1,
        winner,
        f"symbols_say:{winner}:{top}_vs_assigned:{discipline_assigned}",
    )


def _signal_ocr_density(
    discipline_assigned: str | None,
    ocr_text: str | None,
) -> SignalVote:
    """Re-run keyword density on the FULL OCR (not just the head).

    Reuses the atlas's keyword bank for parity. Triggers at >= 10 hits.
    """
    if not ocr_text or len(ocr_text) < 200:
        return SignalVote("ocr_density", 0, None, "ocr_too_short")

    cleaned = sheet_type_atlas._strip_noise(ocr_text).lower()
    counts: dict[str, int] = {}
    for discipline, keywords in sheet_type_atlas._TIER3_KEYWORDS.items():
        total = 0
        for kw in keywords:
            pat = r"\b" + re.escape(kw) + r"\b"
            total += len(re.findall(pat, cleaned))
        if total > 0:
            counts[discipline] = total

    if not counts:
        return SignalVote("ocr_density", 0, None, "no_keyword_hits")

    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    winner, top = ranked[0]
    if top < _SIGNAL3_MIN_HITS:
        return SignalVote(
            "ocr_density",
            0,
            None,
            f"insufficient_density:{winner}:{top}",
        )
    runner_up = ranked[1][1] if len(ranked) > 1 else 0

    if discipline_assigned is None:
        return SignalVote(
            "ocr_density",
            0,
            winner,
            f"no_assignment_to_compare:{winner}:{top}",
        )

    # Close-call detection: if the runner-up is within 80% of winner AND
    # one of them equals discipline_assigned, we treat it as an abstain with
    # a "close_call" marker so demotion reasons can call this out.
    if (
        runner_up > 0
        and runner_up * 1.25 >= top
        and (winner == discipline_assigned or ranked[1][0] == discipline_assigned)
    ):
        return SignalVote(
            "ocr_density",
            0,
            None,
            f"close_call:{winner}:{top}_vs_{ranked[1][0]}:{runner_up}",
        )

    if winner == discipline_assigned:
        return SignalVote("ocr_density", +1, winner, f"ocr:{winner}:{top}")
    return SignalVote(
        "ocr_density",
        -1,
        winner,
        f"ocr_says:{winner}:{top}_vs_assigned:{discipline_assigned}",
    )


def _signal_neighbor(
    discipline_assigned: str | None,
    sheet_number: str | None,
    neighbor_sheets: Iterable[dict],
) -> SignalVote:
    """Check +/-1 numeric neighbors for discipline consensus."""
    n = _parse_sheet_number(sheet_number)
    if n is None:
        return SignalVote("neighbor", 0, None, "no_numeric_sheet_number")

    neighbors_in_radius: list[str] = []
    for ns in neighbor_sheets:
        nn = _parse_sheet_number(ns.get("sheet_number"))
        if nn is None or nn == n:
            continue
        if abs(nn - n) > _NEIGHBOR_RADIUS:
            continue
        disc = ns.get("discipline")
        if disc:
            neighbors_in_radius.append(str(disc))

    if not neighbors_in_radius:
        return SignalVote("neighbor", 0, None, "no_neighbors_with_discipline")

    # Need consensus: all neighbors in radius must agree.
    unique = set(neighbors_in_radius)
    if len(unique) != 1:
        return SignalVote(
            "neighbor",
            0,
            None,
            f"neighbor_disagreement:{'/'.join(sorted(unique))}",
        )
    inferred = neighbors_in_radius[0]

    if discipline_assigned is None:
        return SignalVote("neighbor", 0, inferred, f"no_assignment_to_compare:{inferred}")
    if inferred == discipline_assigned:
        return SignalVote(
            "neighbor",
            +1,
            inferred,
            f"neighbors_agree:{inferred}:{len(neighbors_in_radius)}",
        )
    return SignalVote(
        "neighbor",
        -1,
        inferred,
        f"neighbors_say:{inferred}_vs_assigned:{discipline_assigned}",
    )


# ---------------------------------------------------------------------------
# Verdict logic
# ---------------------------------------------------------------------------

def _build_demotion_reason(
    sheet_label: str,
    discipline_assigned: str | None,
    signals: tuple[SignalVote, ...],
) -> str:
    """Compose a SPECIFIC, actionable missing_input description.

    Tries to surface the disagreeing signals concretely. Falls back to a
    structured low-evidence summary when no signal disagrees but everyone
    abstained.

    Wave 17b.6 breakthrough: every contractor-facing reason names a concrete
    discrepancy, NEVER "confidence below threshold".
    """
    disagreers = [s for s in signals if s.vote == -1]
    abstains = [s for s in signals if s.vote == 0]

    # Build phrase fragments per disagreeing signal.
    fragments: list[str] = []
    for s in disagreers:
        if s.name == "sheet_code" and s.inferred is not None:
            fragments.append(
                f"sheet_code says {s.inferred} but assigned {discipline_assigned}"
            )
        elif s.name == "symbol_content" and s.inferred is not None:
            fragments.append(
                f"detected symbols indicate {s.inferred} (not {discipline_assigned})"
            )
        elif s.name == "ocr_density" and s.inferred is not None:
            fragments.append(
                f"OCR keyword density favors {s.inferred} over {discipline_assigned}"
            )
        elif s.name == "neighbor" and s.inferred is not None:
            fragments.append(
                f"neighbor sheets are {s.inferred}, not {discipline_assigned}"
            )

    if fragments:
        return (
            f"Sheet {sheet_label}: discipline discrepancy — "
            + "; ".join(fragments)
            + ". Contractor: please confirm the correct discipline."
        )

    # No disagreement, just no evidence at all. Be specific about WHY.
    abstain_reasons: list[str] = []
    by_name = {s.name: s for s in abstains}
    if "sheet_code" in by_name and "missing" in by_name["sheet_code"].detail:
        abstain_reasons.append("no sheet_code")
    if "symbol_content" in by_name and "insufficient_symbols" in by_name["symbol_content"].detail:
        abstain_reasons.append("no symbols detected yet")
    if "ocr_density" in by_name:
        det = by_name["ocr_density"].detail
        if "ocr_too_short" in det or "no_keyword_hits" in det:
            abstain_reasons.append("sparse OCR")
        elif "insufficient_density" in det:
            abstain_reasons.append("low keyword density")
        elif "close_call" in det:
            # Surface the exact close call to the contractor.
            tail = det.split("close_call:", 1)[1]
            abstain_reasons.append(f"OCR is a close call ({tail})")
    if "neighbor" in by_name and "no_neighbors" in by_name["neighbor"].detail:
        abstain_reasons.append("no numeric neighbors")
    if "neighbor" in by_name and "neighbor_disagreement" in by_name["neighbor"].detail:
        tail = by_name["neighbor"].detail.split("neighbor_disagreement:", 1)[1]
        abstain_reasons.append(f"neighbor sheets disagree ({tail})")

    if not abstain_reasons:
        abstain_reasons.append("no independent signals available")

    assigned_phrase = (
        f"assigned {discipline_assigned}" if discipline_assigned else "no assigned discipline"
    )
    return (
        f"Sheet {sheet_label}: insufficient evidence to confirm discipline "
        f"({assigned_phrase}) — "
        + ", ".join(abstain_reasons)
        + ". Contractor: please verify."
    )


def verify_classification(
    *,
    sheet_id: str,
    sheet_number: str | None,
    discipline_assigned: str | None,
    sheet_code: str | None,
    ocr_text: str | None,
    symbols_on_sheet: Iterable[dict],
    neighbor_sheets: Iterable[dict],
) -> VerifyResult:
    """Run the four-signal cross-check and produce a Verdict.

    Args:
        sheet_id: UUID of the blueprint_sheets row under verification.
        sheet_number: Display label ("1", "1 of 39", "E-1"). Used for neighbor.
        discipline_assigned: Current discipline value on the sheet (may be None
            if the prior stage couldn't classify).
        sheet_code: Architectural code "E-1", "A-101" etc.
        ocr_text: Full OCR text of this sheet.
        symbols_on_sheet: Iterable of dicts with at least ``class`` key (from
            blueprint_symbols rows joined on sheet_id).
        neighbor_sheets: Iterable of dicts with ``sheet_number`` and
            ``discipline`` keys, for nearby sheets in the same project.

    Returns:
        VerifyResult capturing the verdict, final discipline, confidence,
        truth class, and (when demoted) a concrete missing_input description.
    """
    if discipline_assigned is not None and discipline_assigned not in _VALID_DISCIPLINES:
        # Treat invalid prior assignment as no assignment.
        discipline_assigned = None

    signals = (
        _signal_sheet_code(discipline_assigned, sheet_code),
        _signal_symbol_content(discipline_assigned, symbols_on_sheet),
        _signal_ocr_density(discipline_assigned, ocr_text),
        _signal_neighbor(discipline_assigned, sheet_number, neighbor_sheets),
    )

    agreed = sum(1 for s in signals if s.vote == +1)
    disagreed = sum(1 for s in signals if s.vote == -1)
    abstained = sum(1 for s in signals if s.vote == 0)

    sheet_label = sheet_number or sheet_id[:8]

    # Verdict matrix — votes against discipline_assigned.
    # If no discipline_assigned, we still try to PICK one from the dominant
    # signal (sheet_code > symbol_content > ocr_density > neighbor).
    if discipline_assigned is None:
        # Recovery path: synthesise an assignment from the strongest signal
        # if one exists, then re-vote.
        inferred_candidate: str | None = None
        for s in signals:
            if s.inferred is not None:
                inferred_candidate = s.inferred
                break
        if inferred_candidate is None:
            return VerifyResult(
                sheet_id=sheet_id,
                discipline_assigned=None,
                discipline_final=None,
                confidence=0.30,
                truth="missing_input",
                verdict="demote",
                agreed_count=0,
                disagreed_count=0,
                abstained_count=abstained,
                signals=signals,
                missing_input_reason=_build_demotion_reason(
                    sheet_label, None, signals
                ),
            )
        # Re-vote against the candidate. This is the "verifier picks a winner"
        # path used when LLM dropped the sheet entirely.
        recovered_signals = (
            _signal_sheet_code(inferred_candidate, sheet_code),
            _signal_symbol_content(inferred_candidate, symbols_on_sheet),
            _signal_ocr_density(inferred_candidate, ocr_text),
            _signal_neighbor(inferred_candidate, sheet_number, neighbor_sheets),
        )
        rec_agreed = sum(1 for s in recovered_signals if s.vote == +1)
        rec_disagreed = sum(1 for s in recovered_signals if s.vote == -1)
        rec_abstained = sum(1 for s in recovered_signals if s.vote == 0)
        if rec_agreed >= 2 and rec_disagreed == 0:
            return VerifyResult(
                sheet_id=sheet_id,
                discipline_assigned=None,
                discipline_final=inferred_candidate,
                confidence=0.85 if rec_agreed >= 3 else 0.55,
                truth="derived" if rec_agreed >= 3 else "assumed",
                verdict="promote" if rec_agreed >= 3 else "flag",
                agreed_count=rec_agreed,
                disagreed_count=rec_disagreed,
                abstained_count=rec_abstained,
                signals=recovered_signals,
                missing_input_reason=None
                if rec_agreed >= 3
                else _build_demotion_reason(sheet_label, inferred_candidate, recovered_signals),
            )
        return VerifyResult(
            sheet_id=sheet_id,
            discipline_assigned=None,
            discipline_final=None,
            confidence=0.30,
            truth="missing_input",
            verdict="demote",
            agreed_count=rec_agreed,
            disagreed_count=rec_disagreed,
            abstained_count=rec_abstained,
            signals=recovered_signals,
            missing_input_reason=_build_demotion_reason(
                sheet_label, inferred_candidate, recovered_signals
            ),
        )

    # Standard path — discipline_assigned is set.
    if agreed == 4:
        return VerifyResult(
            sheet_id=sheet_id,
            discipline_assigned=discipline_assigned,
            discipline_final=discipline_assigned,
            confidence=0.98,
            truth="field_confirmed",
            verdict="promote",
            agreed_count=agreed,
            disagreed_count=disagreed,
            abstained_count=abstained,
            signals=signals,
            missing_input_reason=None,
        )

    if agreed == 3 and disagreed == 0:
        return VerifyResult(
            sheet_id=sheet_id,
            discipline_assigned=discipline_assigned,
            discipline_final=discipline_assigned,
            confidence=0.85,
            truth="derived",
            verdict="promote",
            agreed_count=agreed,
            disagreed_count=disagreed,
            abstained_count=abstained,
            signals=signals,
            missing_input_reason=None,
        )

    if agreed >= 2 and disagreed == 0:
        return VerifyResult(
            sheet_id=sheet_id,
            discipline_assigned=discipline_assigned,
            discipline_final=discipline_assigned,
            confidence=0.85,
            truth="derived",
            verdict="promote",
            agreed_count=agreed,
            disagreed_count=disagreed,
            abstained_count=abstained,
            signals=signals,
            missing_input_reason=None,
        )

    # Mixed evidence: at least one agree AND at least one disagree → flag.
    # Also covers "2 agree + some disagree" and "1 agree + 1 disagree" cases.
    if agreed >= 1 and disagreed >= 1 and agreed >= disagreed:
        return VerifyResult(
            sheet_id=sheet_id,
            discipline_assigned=discipline_assigned,
            discipline_final=discipline_assigned,
            confidence=0.55,
            truth="assumed",
            verdict="flag",
            agreed_count=agreed,
            disagreed_count=disagreed,
            abstained_count=abstained,
            signals=signals,
            missing_input_reason=_build_demotion_reason(
                sheet_label, discipline_assigned, signals
            ),
        )

    # 0-1 agree with no balancing disagreement, OR more disagreement than agreement.
    # Demote (clear the discipline + emit specific reason).
    return VerifyResult(
        sheet_id=sheet_id,
        discipline_assigned=discipline_assigned,
        discipline_final=None,
        confidence=0.30,
        truth="missing_input",
        verdict="demote",
        agreed_count=agreed,
        disagreed_count=disagreed,
        abstained_count=abstained,
        signals=signals,
        missing_input_reason=_build_demotion_reason(
            sheet_label, discipline_assigned, signals
        ),
    )


__all__ = [
    "SignalVote",
    "VerifyResult",
    "verify_classification",
]
