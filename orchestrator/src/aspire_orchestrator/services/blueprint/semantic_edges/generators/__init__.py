"""Wave 20.14.5L L-4+L-5 — concrete edge generators.

Subpackage hosts one module per generator. Each module exposes a single
EdgeGenerator subclass. Framework orchestration lives in the parent
package's generator.py — these modules only implement the per-stage
hooks.

Available (L-4):
  - belongs_to_sheet.BelongsToSheetGenerator
  - member_of_family.MemberOfFamilyGenerator

Pending (L-5):
  - annotated_by
  - inside_zone
  - adjacent_to
"""
from .belongs_to_sheet import BelongsToSheetGenerator  # noqa: F401
from .data_access import (  # noqa: F401
    EntitiesQueryClient,
    InMemoryQueryClient,
    SupabaseQueryClient,
)
from .member_of_family import MemberOfFamilyGenerator  # noqa: F401
