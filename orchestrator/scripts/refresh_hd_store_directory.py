"""Refresh the static Home Depot store directory from SerpApi's canonical JSON.

Source: https://serpapi.com/home-depot-stores-us.json (no auth required, ~1776 stores).
Source shape: [{store_id, postal_code, address}, ...]. Address embeds city + 2-letter
state at the tail.

This script:
  1. Downloads the raw SerpApi JSON.
  2. Parses city + state from each address (handles glued/duplicated state quirks).
  3. Derives a `name` field as "Home Depot - {city}".
  4. Writes the normalized record set to
     `src/aspire_orchestrator/services/adam/data/home_depot_stores_us.json`.

Run manually:
  python scripts/refresh_hd_store_directory.py

Run by CI:
  .github/workflows/refresh-hd-store-directory.yml (monthly cron).
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

SOURCE_URL = "https://serpapi.com/home-depot-stores-us.json"
DEFAULT_OUTPUT = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "aspire_orchestrator"
    / "services"
    / "adam"
    / "data"
    / "home_depot_stores_us.json"
)

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "PR", "VI",
}

_GLUED_STATE_RE = re.compile(r"([A-Za-z0-9\)\]])([A-Z]{2})\s*$")
_DUP_STATE_RE = re.compile(r"[ ,]([A-Z]{2})\s+([A-Z]{2})\s*$")
_COMMA_STATE_RE = re.compile(r",([A-Z]{2})\s*$")


def normalize_one(entry: dict) -> dict:
    """Extract {store_id, name, address, city, state, postal_code} from a raw record."""
    addr_full = (entry.get("address") or "").strip().rstrip(",").strip()

    state = ""
    city = ""
    street = addr_full

    glued = _GLUED_STATE_RE.search(addr_full)
    if glued and glued.group(2) in US_STATES:
        addr_full = addr_full[: glued.start(2)].rstrip() + " " + glued.group(2)

    dup = _DUP_STATE_RE.search(addr_full)
    if dup and dup.group(1) in US_STATES and dup.group(2) in US_STATES:
        addr_full = addr_full[: dup.start(2)].rstrip()

    comma = _COMMA_STATE_RE.search(addr_full)
    if comma and comma.group(1) in US_STATES:
        addr_full = addr_full[: comma.start()] + " " + comma.group(1)

    tokens = addr_full.split()
    if tokens and tokens[-1] in US_STATES:
        state = tokens[-1]
        rest = " ".join(tokens[:-1]).rstrip(",").strip()
        if "," in rest:
            comma_idx = rest.rfind(",")
            street = rest[:comma_idx].strip().rstrip(",")
            city_raw = rest[comma_idx + 1:].strip().rstrip(",").strip()
            city_raw = re.sub(r",\s*[A-Z]{2}$", "", city_raw).strip()
            if city_raw.upper() == state:
                rest_tokens = rest.split()
                city = rest_tokens[-1] if rest_tokens else ""
                street = " ".join(rest_tokens[:-1]).rstrip(",").strip()
            else:
                city = city_raw
        else:
            rest_tokens = rest.split()
            if len(rest_tokens) > 1:
                city = rest_tokens[-1]
                street = " ".join(rest_tokens[:-1]).strip()
            else:
                city = rest
                street = ""

    name = f"Home Depot - {city}" if city else "Home Depot"
    return {
        "store_id": entry.get("store_id", ""),
        "name": name,
        "address": street,
        "city": city,
        "state": state,
        "postal_code": entry.get("postal_code", ""),
    }


def fetch_raw() -> list[dict]:
    with urllib.request.urlopen(SOURCE_URL, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main(out_path: Path) -> int:
    raw = fetch_raw()
    if not isinstance(raw, list) or len(raw) < 1500:
        print(
            f"FATAL: SerpApi response unexpected — got {type(raw).__name__} "
            f"len={len(raw) if hasattr(raw, '__len__') else 'n/a'}; refusing to write.",
            file=sys.stderr,
        )
        return 2

    normalized = [normalize_one(e) for e in raw]

    missing_state = sum(1 for n in normalized if not n["state"])
    missing_city = sum(1 for n in normalized if not n["city"])
    missing_id = sum(1 for n in normalized if not n["store_id"])

    if missing_id or missing_state > 50 or missing_city > 50:
        print(
            f"FATAL: parse quality dropped — state_missing={missing_state} "
            f"city_missing={missing_city} id_missing={missing_id}",
            file=sys.stderr,
        )
        return 3

    if missing_state or missing_city:
        print(
            f"WARN: minor parse gaps — state_missing={missing_state} "
            f"city_missing={missing_city}",
            file=sys.stderr,
        )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(normalized, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(normalized)} stores to {out_path}")
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUTPUT
    sys.exit(main(target))
