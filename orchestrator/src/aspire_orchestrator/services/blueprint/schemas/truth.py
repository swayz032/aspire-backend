"""Truth-class enum — mirrors the `truth_class` Postgres enum."""
from __future__ import annotations

from enum import Enum


class TruthClass(str, Enum):
    OBSERVED = "observed"
    DERIVED = "derived"
    ASSUMED = "assumed"
    FIELD_CONFIRMED = "field_confirmed"
    VENDOR_CONFIRMED = "vendor_confirmed"
    PERMIT_CONFIRMED = "permit_confirmed"


class TariffFlag(str, Enum):
    SECTION_232_STEEL = "section_232_steel"
    SECTION_232_ALUMINUM = "section_232_aluminum"
    SOFTWOOD_LUMBER = "softwood_lumber"
    NONE = "none"


class Discipline(str, Enum):
    A = "A"
    S = "S"
    M = "M"
    E = "E"
    P = "P"
    FP = "FP"
    C = "C"
    L = "L"
    SPECS = "Specs"
    SCHEDULES = "Schedules"
    ADDENDA = "Addenda"
