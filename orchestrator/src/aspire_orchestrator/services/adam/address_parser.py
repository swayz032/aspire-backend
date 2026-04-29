"""Deterministic US address parser for ATTOM /expandedprofile.

Wraps `usaddress.tag` (CRF model trained on labeled US address corpus) and
normalizes output into the two-line ATTOM format:
  address1 = "{number} {pre_dir} {street_name} {post_type} {post_dir} {unit_type} {unit_num}"
  address2 = "{city}, {state} {zip}"

Why deterministic parsing matters:
- ATTOM /expandedprofile expects strictly normalized address1 + address2 strings.
- Any ambiguity (full-word "Apartment" vs USPS "APT", missing unit, etc.) causes
  ATTOM to fall back to street-level parcels and miss unit-level data.
- This module is the SINGLE primary path. There is no fallback. Per Aspire's
  no-fallback-design-principle: if parsing fails, raise ParseError and let the
  caller emit a Yellow-tier "more info needed" prompt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import usaddress


class ParseError(ValueError):
    """Raised when the input cannot be confidently parsed into a structured US address."""


@dataclass(frozen=True)
class ParsedAddress:
    """Normalized two-line ATTOM-ready address."""

    address1: str
    address2: str
    components: Mapping[str, str]
    raw: str


# USPS-style normalization. ATTOM accepts both forms but normalizing produces
# stable cassettes/cache keys and avoids subtle whitespace/case mismatches.
_STREET_TYPE_MAP = {
    "road": "Rd",
    "street": "St",
    "avenue": "Ave",
    "boulevard": "Blvd",
    "place": "Pl",
    "drive": "Dr",
    "lane": "Ln",
    "court": "Ct",
    "circle": "Cir",
    "parkway": "Pkwy",
    "highway": "Hwy",
    "terrace": "Ter",
    "way": "Way",
    "trail": "Trl",
    "square": "Sq",
}

_OCCUPANCY_TYPE_MAP = {
    "apartment": "APT",
    "apt": "APT",
    "suite": "STE",
    "ste": "STE",
    "unit": "UNIT",
    "#": "UNIT",
    "building": "BLDG",
    "bldg": "BLDG",
    "floor": "FL",
    "fl": "FL",
    "room": "RM",
    "rm": "RM",
}


def _norm_street_type(raw: str) -> str:
    if not raw:
        return ""
    cleaned = raw.strip().rstrip(".")
    return _STREET_TYPE_MAP.get(cleaned.lower(), cleaned)


def _norm_occupancy_type(raw: str) -> str:
    if not raw:
        return ""
    cleaned = raw.strip().rstrip(".").lower()
    return _OCCUPANCY_TYPE_MAP.get(cleaned, cleaned.upper())


def _strip_directional(raw: str) -> str:
    if not raw:
        return ""
    cleaned = raw.strip().rstrip(".").upper()
    if cleaned in {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}:
        return cleaned
    return cleaned


def parse_us_address(raw: str) -> ParsedAddress:
    """Parse a free-form US address into ATTOM /expandedprofile format.

    Returns a ParsedAddress with `address1` (street + unit) and `address2`
    (city, state, zip). All fields are normalized to USPS-compatible
    abbreviations (Rd / St / APT) for stable downstream lookups.

    Raises ParseError if street_number, street_name, city, or state are missing.
    ZIP is optional — many residential queries omit it and ATTOM still resolves.
    """
    if not raw or not raw.strip():
        raise ParseError("empty address")

    cleaned = raw.strip()

    try:
        tagged, addr_type = usaddress.tag(cleaned)
    except usaddress.RepeatedLabelError as exc:
        raise ParseError(f"ambiguous address (repeated labels): {exc}") from exc

    if addr_type not in {"Street Address", "Intersection", "PO Box"}:
        raise ParseError(f"input is not a street address (type={addr_type})")
    if addr_type != "Street Address":
        raise ParseError(f"only street addresses are supported (got {addr_type})")

    components: dict[str, str] = {k: str(v) for k, v in tagged.items()}

    number = components.get("AddressNumber", "").strip()
    pre_dir = _strip_directional(components.get("StreetNamePreDirectional", ""))
    street_name = components.get("StreetName", "").strip()
    post_type = _norm_street_type(components.get("StreetNamePostType", ""))
    post_dir = _strip_directional(components.get("StreetNamePostDirectional", ""))
    unit_type = _norm_occupancy_type(components.get("OccupancyType", ""))
    unit_num = components.get("OccupancyIdentifier", "").strip()
    city = components.get("PlaceName", "").strip()
    state = components.get("StateName", "").strip().upper().rstrip(".")
    zip_code = components.get("ZipCode", "").strip()

    missing: list[str] = []
    if not number:
        missing.append("street_number")
    if not street_name:
        missing.append("street_name")
    if not city:
        missing.append("city")
    if not state:
        missing.append("state")
    if missing:
        raise ParseError(
            f"address missing required components: {', '.join(missing)} (raw={raw!r})"
        )

    street_parts = [number]
    if pre_dir:
        street_parts.append(pre_dir)
    street_parts.append(street_name)
    if post_type:
        street_parts.append(post_type)
    if post_dir:
        street_parts.append(post_dir)
    if unit_num:
        if unit_type:
            street_parts.extend([unit_type, unit_num])
        else:
            street_parts.extend(["UNIT", unit_num])

    address1 = " ".join(part for part in street_parts if part)

    if zip_code:
        address2 = f"{city}, {state} {zip_code}"
    else:
        address2 = f"{city}, {state}"

    return ParsedAddress(
        address1=address1,
        address2=address2,
        components=components,
        raw=raw,
    )
