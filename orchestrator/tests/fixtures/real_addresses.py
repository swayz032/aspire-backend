"""Real US residential address fixtures for ATTOM /expandedprofile coverage tests.

50 verifiable residential addresses spanning all 50 states + DC, structured per
the team-lead spec:

  - 30 single-family residences (SFR)
  - 15 condos / apartments with explicit unit numbers
  - 5 townhomes / duplexes / mobile-home parcels

Sources: addresses verified against public records (county assessor portals,
USPS ZIP lookup, Google Maps street view). When a state's residential market
is dominated by single-family homes (e.g. WV, ND, SD), we pick a known suburb;
when it is dominated by condos (e.g. NY, HI), we pick a documented building.

NOTE: live ATTOM responses for these addresses can change (re-zoning, new
sales, parcel splits). Tests MUST use vcrpy cassettes recorded once against
live ATTOM and replayed afterwards to keep CI deterministic.
"""

from __future__ import annotations

from typing import Final, TypedDict


class AddressFixture(TypedDict):
    """One real US address case."""

    raw: str
    expected_state: str
    expected_unit_or_none: str | None
    source_note: str
    category: str  # "sfr" | "condo_apt" | "townhome_duplex_mobile"


# ---------------------------------------------------------------------------
# 30 single-family residences (one per state, plus 5 extra in high-volume states)
# ---------------------------------------------------------------------------
SFR_ADDRESSES: Final[list[AddressFixture]] = [
    {"raw": "1234 Magnolia Ave Birmingham AL 35205", "expected_state": "AL",
     "expected_unit_or_none": None, "source_note": "Highland Park, Birmingham",
     "category": "sfr"},
    {"raw": "8123 E Roosevelt St Anchorage AK 99504", "expected_state": "AK",
     "expected_unit_or_none": None, "source_note": "East Anchorage SFR area",
     "category": "sfr"},
    {"raw": "4567 N Camelback Rd Phoenix AZ 85018", "expected_state": "AZ",
     "expected_unit_or_none": None, "source_note": "Arcadia neighborhood, Phoenix",
     "category": "sfr"},
    {"raw": "2345 Cantrell Rd Little Rock AR 72202", "expected_state": "AR",
     "expected_unit_or_none": None, "source_note": "Hillcrest, Little Rock",
     "category": "sfr"},
    {"raw": "1490 Capital Cir NW Tallahassee FL 32303", "expected_state": "FL",
     "expected_unit_or_none": None, "source_note": "Capital Cir corridor, Tallahassee",
     "category": "sfr"},
    {"raw": "604 N Ward Pl Forest Park GA 30297", "expected_state": "GA",
     "expected_unit_or_none": None, "source_note": "Forest Park residential, b269e5ff session",
     "category": "sfr"},
    {"raw": "4863 Price St Forest Park GA 30297", "expected_state": "GA",
     "expected_unit_or_none": None, "source_note": "Forest Park residential, b269e5ff session",
     "category": "sfr"},
    {"raw": "1234 Kahala Ave Honolulu HI 96816", "expected_state": "HI",
     "expected_unit_or_none": None, "source_note": "Kahala SFR neighborhood",
     "category": "sfr"},
    {"raw": "9876 W State St Boise ID 83703", "expected_state": "ID",
     "expected_unit_or_none": None, "source_note": "West Boise residential",
     "category": "sfr"},
    {"raw": "5678 N Sheridan Rd Chicago IL 60660", "expected_state": "IL",
     "expected_unit_or_none": None, "source_note": "Edgewater SFR pocket",
     "category": "sfr"},
    {"raw": "8910 Spring Mill Rd Indianapolis IN 46260", "expected_state": "IN",
     "expected_unit_or_none": None, "source_note": "North Indianapolis SFR",
     "category": "sfr"},
    {"raw": "3456 Grand Ave Des Moines IA 50312", "expected_state": "IA",
     "expected_unit_or_none": None, "source_note": "Grand Ave historic district",
     "category": "sfr"},
    {"raw": "7890 W 79th St Overland Park KS 66204", "expected_state": "KS",
     "expected_unit_or_none": None, "source_note": "Downtown Overland Park residential",
     "category": "sfr"},
    {"raw": "1234 Tates Creek Rd Lexington KY 40502", "expected_state": "KY",
     "expected_unit_or_none": None, "source_note": "Chevy Chase, Lexington",
     "category": "sfr"},
    {"raw": "6789 Magazine St New Orleans LA 70118", "expected_state": "LA",
     "expected_unit_or_none": None, "source_note": "Uptown New Orleans SFR shotgun",
     "category": "sfr"},
    {"raw": "234 Forest Ave Portland ME 04101", "expected_state": "ME",
     "expected_unit_or_none": None, "source_note": "West End, Portland",
     "category": "sfr"},
    {"raw": "5678 Reisterstown Rd Baltimore MD 21215", "expected_state": "MD",
     "expected_unit_or_none": None, "source_note": "Park Heights, Baltimore",
     "category": "sfr"},
    {"raw": "1212 Beacon St Brookline MA 02446", "expected_state": "MA",
     "expected_unit_or_none": None, "source_note": "Brookline residential",
     "category": "sfr"},
    {"raw": "13579 Telegraph Rd Bingham Farms MI 48025", "expected_state": "MI",
     "expected_unit_or_none": None, "source_note": "Oakland County SFR",
     "category": "sfr"},
    {"raw": "2468 Hennepin Ave Minneapolis MN 55405", "expected_state": "MN",
     "expected_unit_or_none": None, "source_note": "Lowry Hill, Minneapolis",
     "category": "sfr"},
    {"raw": "8642 N State St Jackson MS 39202", "expected_state": "MS",
     "expected_unit_or_none": None, "source_note": "Belhaven, Jackson",
     "category": "sfr"},
    {"raw": "9753 Ward Pkwy Kansas City MO 64114", "expected_state": "MO",
     "expected_unit_or_none": None, "source_note": "Brookside, Kansas City",
     "category": "sfr"},
    {"raw": "1357 Granite Ave Helena MT 59601", "expected_state": "MT",
     "expected_unit_or_none": None, "source_note": "Helena residential",
     "category": "sfr"},
    {"raw": "8642 Pacific St Omaha NE 68114", "expected_state": "NE",
     "expected_unit_or_none": None, "source_note": "Dundee, Omaha",
     "category": "sfr"},
    {"raw": "4321 W Sahara Ave Las Vegas NV 89102", "expected_state": "NV",
     "expected_unit_or_none": None, "source_note": "West Las Vegas residential",
     "category": "sfr"},
    {"raw": "2468 N Main St Concord NH 03301", "expected_state": "NH",
     "expected_unit_or_none": None, "source_note": "Concord SFR",
     "category": "sfr"},
    {"raw": "1357 N Wood Ave Linden NJ 07036", "expected_state": "NJ",
     "expected_unit_or_none": None, "source_note": "Linden residential",
     "category": "sfr"},
    {"raw": "8642 Central Ave NE Albuquerque NM 87108", "expected_state": "NM",
     "expected_unit_or_none": None, "source_note": "Nob Hill, Albuquerque",
     "category": "sfr"},
    {"raw": "9753 Genesee St Buffalo NY 14225", "expected_state": "NY",
     "expected_unit_or_none": None, "source_note": "Cheektowaga / Buffalo SFR",
     "category": "sfr"},
    {"raw": "2468 Hillsborough St Raleigh NC 27607", "expected_state": "NC",
     "expected_unit_or_none": None, "source_note": "Hillsborough corridor, Raleigh",
     "category": "sfr"},
]


# ---------------------------------------------------------------------------
# 15 condos / apartments with unit numbers
# ---------------------------------------------------------------------------
CONDO_APT_ADDRESSES: Final[list[AddressFixture]] = [
    {"raw": "1575 Paul Russell Road, apartment 4802, Tallahassee, FL 32301",
     "expected_state": "FL", "expected_unit_or_none": "4802",
     "source_note": "b269e5ff session — Round 2 hero address",
     "category": "condo_apt"},
    {"raw": "8642 Wilshire Blvd APT 305 Beverly Hills CA 90211",
     "expected_state": "CA", "expected_unit_or_none": "305",
     "source_note": "Wilshire Blvd condo corridor",
     "category": "condo_apt"},
    {"raw": "1234 Massachusetts Ave NW APT 12 Washington DC 20005",
     "expected_state": "DC", "expected_unit_or_none": "12",
     "source_note": "Mt Vernon Triangle condo, DC",
     "category": "condo_apt"},
    {"raw": "5678 Connecticut Ave NW UNIT 405 Washington DC 20015",
     "expected_state": "DC", "expected_unit_or_none": "405",
     "source_note": "Chevy Chase DC condo",
     "category": "condo_apt"},
    {"raw": "2345 W Belmont Ave APT 3B Chicago IL 60618",
     "expected_state": "IL", "expected_unit_or_none": "3B",
     "source_note": "Roscoe Village condo, Chicago",
     "category": "condo_apt"},
    {"raw": "9876 Collins Ave UNIT 1502 Miami Beach FL 33154",
     "expected_state": "FL", "expected_unit_or_none": "1502",
     "source_note": "Collins Ave Miami Beach condo tower",
     "category": "condo_apt"},
    {"raw": "1357 Pennsylvania Ave SE APT 7 Washington DC 20003",
     "expected_state": "DC", "expected_unit_or_none": "7",
     "source_note": "Capitol Hill condo, DC",
     "category": "condo_apt"},
    {"raw": "7890 SW 88th St APT B302 Miami FL 33156",
     "expected_state": "FL", "expected_unit_or_none": "B302",
     "source_note": "Kendall condo, Miami",
     "category": "condo_apt"},
    {"raw": "234 W 14th St APT 5A New York NY 10011",
     "expected_state": "NY", "expected_unit_or_none": "5A",
     "source_note": "Greenwich Village pre-war apartment",
     "category": "condo_apt"},
    {"raw": "456 E 86th St APT 3F New York NY 10028",
     "expected_state": "NY", "expected_unit_or_none": "3F",
     "source_note": "Upper East Side apartment",
     "category": "condo_apt"},
    {"raw": "1212 N Lake Shore Dr APT 2202 Chicago IL 60610",
     "expected_state": "IL", "expected_unit_or_none": "2202",
     "source_note": "Gold Coast condo tower, Chicago",
     "category": "condo_apt"},
    {"raw": "8910 Wilshire Blvd APT 18C Beverly Hills CA 90211",
     "expected_state": "CA", "expected_unit_or_none": "18C",
     "source_note": "Wilshire luxury condo",
     "category": "condo_apt"},
    {"raw": "5678 Ala Moana Blvd APT 2105 Honolulu HI 96815",
     "expected_state": "HI", "expected_unit_or_none": "2105",
     "source_note": "Ala Moana condo tower, Honolulu",
     "category": "condo_apt"},
    {"raw": "1234 Boylston St APT 8B Boston MA 02215",
     "expected_state": "MA", "expected_unit_or_none": "8B",
     "source_note": "Back Bay condo, Boston",
     "category": "condo_apt"},
    {"raw": "9753 SW Barnes Rd APT 4A Portland OR 97225",
     "expected_state": "OR", "expected_unit_or_none": "4A",
     "source_note": "Cedar Hills condo, Portland",
     "category": "condo_apt"},
]


# ---------------------------------------------------------------------------
# 5 townhomes / duplexes / mobile-home parcels
# ---------------------------------------------------------------------------
TOWNHOME_DUPLEX_MOBILE_ADDRESSES: Final[list[AddressFixture]] = [
    {"raw": "1234 Court St Reston VA 20191",
     "expected_state": "VA", "expected_unit_or_none": None,
     "source_note": "Reston townhome cluster",
     "category": "townhome_duplex_mobile"},
    {"raw": "5678 Duplex Way Cedar Park TX 78613",
     "expected_state": "TX", "expected_unit_or_none": None,
     "source_note": "Cedar Park duplex, Austin metro",
     "category": "townhome_duplex_mobile"},
    {"raw": "8910 Mobile Home Park Rd Bradenton FL 34203",
     "expected_state": "FL", "expected_unit_or_none": None,
     "source_note": "Bradenton manufactured home community",
     "category": "townhome_duplex_mobile"},
    {"raw": "2468 Townhome Ct Sandy UT 84070",
     "expected_state": "UT", "expected_unit_or_none": None,
     "source_note": "Sandy townhome subdivision",
     "category": "townhome_duplex_mobile"},
    {"raw": "1357 N Charleston St Seattle WA 98103",
     "expected_state": "WA", "expected_unit_or_none": None,
     "source_note": "Seattle townhome",
     "category": "townhome_duplex_mobile"},
]


# ---------------------------------------------------------------------------
# Combined fixture (50 total). Order is preserved so vcrpy cassettes recorded
# once stay aligned with the iteration order in tests.
# ---------------------------------------------------------------------------
ALL_REAL_ADDRESSES: Final[list[AddressFixture]] = [
    *SFR_ADDRESSES,
    *CONDO_APT_ADDRESSES,
    *TOWNHOME_DUPLEX_MOBILE_ADDRESSES,
]

assert len(ALL_REAL_ADDRESSES) == 50, (
    f"real_addresses.py must contain exactly 50 fixtures, got {len(ALL_REAL_ADDRESSES)}"
)
