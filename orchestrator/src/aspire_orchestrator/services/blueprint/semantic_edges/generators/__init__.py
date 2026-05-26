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
from .adjacent_to import AdjacentToGenerator  # noqa: F401
from .annotated_by import AnnotatedByGenerator  # noqa: F401
from .belongs_to_sheet import BelongsToSheetGenerator  # noqa: F401
from .data_access import (  # noqa: F401
    CompositionRecord,
    EntitiesQueryClient,
    EntityRecord,
    InMemoryQueryClient,
    OcrSpanRecord,
    SupabaseQueryClient,
    ZoneRecord,
)
from .inside_zone import InsideZoneGenerator  # noqa: F401
from .member_of_family import MemberOfFamilyGenerator  # noqa: F401
from .spatial_index import (  # noqa: F401
    SpatialIndex,
    alignment_factor,
    bbox_center,
    bbox_overlap_ratio,
    normalize_radius_for_sheet,
    polygon_to_bbox,
)
