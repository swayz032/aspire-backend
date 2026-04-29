"""Server-side TTS post-processor for Ava's address-bearing strings.

Why this module exists: the LLM's pronunciation rule ("spell street suffixes
out") is non-deterministic — it works most of the time but fails enough that
users hear "Rd" / "Pl" / "Apt" read literally on a live voice call. Pairing
the prompt rule with a deterministic server-side post-processor is appropriate
belt-and-suspenders for things outside our control (LLM instruction-following).

Wave 2.1 of the production-polish plan. Applied to Adam's response strings
(the LLM-facing `summary`, `result`, and every record-level address field)
BEFORE they leave the orchestrator, so the LLM sees only fully-spelled forms
and TTS reads them correctly.

Performance note: regex substitutions on a 5KB JSON take <2ms — negligible.
"""

from __future__ import annotations

import re
from typing import Any

# ---------------------------------------------------------------------------
# Replacement table — order matters where one pattern is a prefix of another
# (we apply each independently with word boundaries, so prefix collisions are
# avoided by the (\.?) capture).
# ---------------------------------------------------------------------------

# Standalone street suffixes / unit markers — replace anywhere when preceded
# by a word boundary AND not immediately followed by another letter. We use
# `\.?(?![A-Za-z])` instead of `\.?\b` because `\b` after an optional period
# fails on end-of-string and other non-word boundaries (period + EOS = both
# non-word, no transition, no boundary). The lookahead allows the period to
# be consumed by the rule when present (e.g. "Rd." -> "Road") AND keeps the
# rule from greedily matching inside other words like "Bordeauxoad".
_STREET_SUFFIX_RULES: tuple[tuple[str, str], ...] = (
    (r"\bRd\.?(?![A-Za-z])",     "Road"),
    (r"\bPl\.?(?![A-Za-z])",     "Place"),
    (r"\bAve\.?(?![A-Za-z])",    "Avenue"),
    (r"\bBlvd\.?(?![A-Za-z])",   "Boulevard"),
    (r"\bDr\.?(?![A-Za-z])",     "Drive"),
    (r"\bLn\.?(?![A-Za-z])",     "Lane"),
    (r"\bCt\.?(?![A-Za-z])",     "Court"),
    (r"\bCir\.?(?![A-Za-z])",    "Circle"),
    (r"\bPkwy\.?(?![A-Za-z])",   "Parkway"),
    (r"\bHwy\.?(?![A-Za-z])",    "Highway"),
    (r"\bApt\.?(?![A-Za-z])",    "Apartment"),
    (r"\bSte\.?(?![A-Za-z])",    "Suite"),
)

# `St` is ambiguous (Street vs Saint vs proper-noun "St James"). Only expand
# when it follows a numeric token (street number) — e.g. "604 Ward St" ->
# "604 Ward Street", but "St James" stays "St James".
#
# The `lead` group also accepts an optional cardinal abbreviation between the
# number and the street name ("100 W. Main St", "604 NE 9th St"), since that
# pattern does not yet have its cardinal expanded — the cardinal rules run
# AFTER this pass and would otherwise miss the final "St".
_ST_NUMERIC_PRECEDED_RE = re.compile(
    r"(?P<lead>\b\d+[A-Za-z]?\s+"
    r"(?:(?:[NSEW]{1,2})\.?\s+)?"  # optional cardinal token (N, S, E, W, NE, NW, SE, SW)
    r"(?:[A-Z][A-Za-z]+\s+)+)"
    # Trailing: optional period, then NOT a letter (mirrors the suffix rules).
    # `\.?\b` would fail on "Main St." at end-of-string because period and EOS
    # are both non-word.
    r"St(?P<trail>\.?(?![A-Za-z]))",
    # ATTOM/SerpApi return addresses in all-caps ("4863 PRICE ST"). The St
    # rule must match regardless of case. The numeric-prefix lead pattern
    # (`\d+`) and the capitalized-word requirement (`[A-Z][A-Za-z]+`) keep
    # this from matching prose like "ST JAMES PLACE" — those would lack
    # the leading digit token. IGNORECASE applies to the literal "St"
    # token so "ST" / "st" / "St" all match.
    re.IGNORECASE,
)

# Cardinal abbreviations as a directional prefix between number and street name:
#   "604 N Ward Pl"  ->  "604 North Ward Place"
#   "100 W. Main St" ->  "100 West Main Street"
# Single-letter cardinal directly preceded by a numeric token and followed by
# a capitalized word (the street name). Case-sensitive on the cardinal letter
# itself (uppercase only) so prose like "go n now" is not rewritten.
_CARDINAL_PREFIX_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # Lookbehind is just `(?<=\d)` not `(?<=\b\d)` — `\b\d` only matches at
    # the FIRST digit of a number, so multi-digit numbers ("604", "100") fail
    # because there's no word boundary between adjacent digits. Just requiring
    # "preceded by a digit" is enough since `\s+` immediately follows.
    # Trailing `\.?(?![A-Za-z])` mirrors the suffix rules — handles `W.` at
    # end-of-string or followed by non-letter, where `\b` would fail.
    (re.compile(r"(?<=\d)\s+N\.?(?![A-Za-z])(?=\s+[A-Z])"), " North"),
    (re.compile(r"(?<=\d)\s+S\.?(?![A-Za-z])(?=\s+[A-Z])"), " South"),
    (re.compile(r"(?<=\d)\s+E\.?(?![A-Za-z])(?=\s+[A-Z])"), " East"),
    (re.compile(r"(?<=\d)\s+W\.?(?![A-Za-z])(?=\s+[A-Z])"), " West"),
)

# Cardinal abbreviations as a directional SUFFIX after a street-type word:
#   "1490 Capital Circle NW" -> "1490 Capital Circle Northwest"
#   "200 Main Street SE"     -> "200 Main Street Southeast"
# Run AFTER tier-1 expansion so a trailing "Cir" is already "Circle" by the
# time we look. Bigrams (NE/NW/SE/SW) are matched first so a bare "N" rule
# does not eat the "N" of "NW". The lookbehind anchors the rule to a known
# spelled-out street type word — that's the disambiguation between a
# directional postfix and stray capital letters in surrounding prose.
_STREET_TYPE_WORDS: tuple[str, ...] = (
    "Street", "Road", "Place", "Avenue", "Boulevard", "Drive", "Lane",
    "Court", "Circle", "Parkway", "Highway", "Way", "Trail", "Terrace",
    "Square", "Plaza", "Loop",
)
_STREET_TYPE_ALT = "|".join(_STREET_TYPE_WORDS)

# Python's stdlib `re` requires fixed-width lookbehinds. Since the street-type
# words have different widths, we use a capturing group for the type word and
# a backreference in the replacement to keep the rules compatible with `re`.
_CARDINAL_SUFFIX_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    # Bigrams must come before singles so "NW" is not partially eaten by a
    # bare "N" rule.
    (re.compile(rf"\b({_STREET_TYPE_ALT})\s+NE\.?(?![A-Za-z])"), r"\1 Northeast"),
    (re.compile(rf"\b({_STREET_TYPE_ALT})\s+NW\.?(?![A-Za-z])"), r"\1 Northwest"),
    (re.compile(rf"\b({_STREET_TYPE_ALT})\s+SE\.?(?![A-Za-z])"), r"\1 Southeast"),
    (re.compile(rf"\b({_STREET_TYPE_ALT})\s+SW\.?(?![A-Za-z])"), r"\1 Southwest"),
    (re.compile(rf"\b({_STREET_TYPE_ALT})\s+N\.?(?![A-Za-z])"),  r"\1 North"),
    (re.compile(rf"\b({_STREET_TYPE_ALT})\s+S\.?(?![A-Za-z])"),  r"\1 South"),
    (re.compile(rf"\b({_STREET_TYPE_ALT})\s+E\.?(?![A-Za-z])"),  r"\1 East"),
    (re.compile(rf"\b({_STREET_TYPE_ALT})\s+W\.?(?![A-Za-z])"),  r"\1 West"),
)

# Pre-compile suffix rules once at import. Case-insensitive so "APT 4802" and
# "apt 4802" both normalize correctly — case is preserved on the first letter
# of the replacement word ("Apartment", "Road", "Place"). When the original
# was lowercase ("apt"), TTS still pronounces "Apartment" correctly.
_SUFFIX_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), repl) for pattern, repl in _STREET_SUFFIX_RULES
)


def normalize_addresses_for_speech(text: str) -> str:
    """Expand street/unit abbreviations in a string for TTS pronunciation.

    Idempotent: running the function twice on the same input produces the same
    output (the spelled-out forms do not match any of the patterns).

    Returns the input unchanged when it is empty/None or contains no
    abbreviations.
    """
    if not isinstance(text, str) or not text:
        return text

    out = text
    # 1. Standard street suffix / unit markers (Rd, Pl, Apt, ...). Tier-1 runs
    #    first so the suffix-cardinal rule can match against the spelled-out
    #    "Circle"/"Avenue" later.
    for pattern, repl in _SUFFIX_PATTERNS:
        out = pattern.sub(repl, out)

    # 2. Cardinal direction PREFIXES between the street number and the street
    #    name — must run before the St rule so "100 W. Main St" first becomes
    #    "100 West Main St" (so the lead pattern in step 3 has spelled-out
    #    intermediate words to consume on its way to "St").
    for pattern, repl in _CARDINAL_PREFIX_RULES:
        out = pattern.sub(repl, out)

    # 3. `St` only when preceded by a street-number-ish token. Runs AFTER the
    #    cardinal prefix expansion above so the intermediate-word pattern can
    #    consume "West"/"North" rather than the literal "W."/"N." which only
    #    contain a single capital letter and would not satisfy
    #    `[A-Z][A-Za-z]+`.
    # The trail group consumes an optional period — don't re-emit it. We want
    # "100 Main St." -> "100 Main Street" not "100 Main Street."
    out = _ST_NUMERIC_PRECEDED_RE.sub(
        lambda m: f"{m.group('lead')}Street",
        out,
    )

    # 4. Cardinal direction SUFFIXES after a street-type word ("Capital Circle
    #    NW" -> "Capital Circle Northwest"). Bigrams come before singles in the
    #    rule order so "NW" is not partially eaten by the bare "N" rule.
    for pattern, repl in _CARDINAL_SUFFIX_RULES:
        out = pattern.sub(repl, out)

    return out


# ---------------------------------------------------------------------------
# Record / payload walker
# ---------------------------------------------------------------------------

# Field names that are known to carry an address-shaped string. Applied to
# both top-level and nested record dicts. Strict allow-list (NOT a generic
# "rewrite every string") so non-address strings — names, descriptions,
# product titles — are never touched.
_ADDRESS_FIELDS: frozenset[str] = frozenset({
    "address",
    "address1",
    "address2",
    "address_string",
    "address_one_line",
    "address_oneLine",
    "address_label",
    "addressLabel",
    "city_state_zip",
    "formatted_address",
    "mailing_address",
    "normalized_address",
    "oneLine",
    "one_line",
    "store_address",
    "street",
    "street1",
    "street_address",
})

# Top-level string fields on ResearchResponse / Adam payloads that may contain
# addresses interpolated into prose (Ava reads these aloud).
_TOP_LEVEL_TEXT_FIELDS: frozenset[str] = frozenset({
    "summary",
    "result",
    "headline",
    "speech_summary",
    "narration",
})


def _normalize_value(key: str, value: Any) -> Any:
    """Apply `normalize_addresses_for_speech` to address-shaped string fields.

    Recurses into dicts and lists so nested records (e.g. comp lists, sale
    history, store_summary inside a card pack) are also normalized.
    """
    if isinstance(value, str):
        if key in _ADDRESS_FIELDS or key in _TOP_LEVEL_TEXT_FIELDS:
            return normalize_addresses_for_speech(value)
        return value
    if isinstance(value, dict):
        return {k: _normalize_value(k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize_value(key, item) for item in value]
    return value


def normalize_payload_for_speech(payload: Any) -> Any:
    """Recursively normalize an Adam response payload (dict, list, or value).

    Operates on a copy — does not mutate the input. Safe to call on any
    JSON-serializable structure: it walks the tree, applying the address
    post-processor to any string field whose key is in `_ADDRESS_FIELDS` or
    `_TOP_LEVEL_TEXT_FIELDS`. All other values pass through unchanged.
    """
    if isinstance(payload, dict):
        return {k: _normalize_value(k, v) for k, v in payload.items()}
    if isinstance(payload, list):
        # Top-level list: recurse with a sentinel key that doesn't trigger
        # rewriting on bare strings (lists of pure strings won't be addresses).
        return [normalize_payload_for_speech(item) for item in payload]
    if isinstance(payload, str):
        # Bare string at the root — treat as a top-level summary.
        return normalize_addresses_for_speech(payload)
    return payload


def normalize_research_response_in_place(response: Any) -> Any:
    """Mutate a ResearchResponse instance so address-bearing strings read aloud correctly.

    Single chokepoint for the speech-readiness contract: applied from
    `dispatch_playbook` to every successful response, regardless of which
    playbook produced it.

    Mutates `summary` and every record's address-shaped fields (recurses into
    nested dicts/lists). Returns the same instance for chainability.

    Safe to call on a None or empty response (returns input unchanged).
    """
    if response is None:
        return response

    summary = getattr(response, "summary", None)
    if isinstance(summary, str) and summary:
        response.summary = normalize_addresses_for_speech(summary)

    records = getattr(response, "records", None)
    if isinstance(records, list):
        response.records = [
            _normalize_value("__record__", r) if isinstance(r, dict) else r
            for r in records
        ]

    extra = getattr(response, "extra", None)
    if isinstance(extra, dict):
        response.extra = {k: _normalize_value(k, v) for k, v in extra.items()}

    return response


__all__ = (
    "normalize_addresses_for_speech",
    "normalize_payload_for_speech",
    "normalize_research_response_in_place",
)
