"""Wave 17b.3 — SEE naming pipeline tests.

Verifies:
  * COCO blacklist + 'unmapped:*' prefix suppression.
  * Real construction classes pass through untouched.
  * Interpretable fallback uses discipline + confidence band.
  * Tenant-memory hit (via VGSE pipeline) takes precedence over fallback.
  * VGSE row builder preserves original class + signature_hash in bbox.
"""
from __future__ import annotations

import pytest

from aspire_orchestrator.services.blueprint.see_naming import (
    COCO_SUPPRESS_SET,
    interpretable_fallback,
    is_opaque_name,
    should_suppress_yolo_class,
)
from aspire_orchestrator.services.blueprint.vgse_pipeline import (
    detections_to_blueprint_symbol_rows,
)


# ---------------------------------------------------------------------------
# Suppression: COCO blacklist + 'unmapped:*' prefix
# ---------------------------------------------------------------------------

class TestSuppression:
    def test_unmapped_door_suppressed(self) -> None:
        suppress, reason = should_suppress_yolo_class("unmapped:door")
        assert suppress is True
        assert reason == "unmapped_prefix"

    def test_unmapped_plumbing_fixture_lav_suppressed(self) -> None:
        suppress, reason = should_suppress_yolo_class("unmapped:plumbing_fixture_lav")
        assert suppress is True
        assert reason == "unmapped_prefix"

    def test_raw_coco_person_suppressed(self) -> None:
        suppress, reason = should_suppress_yolo_class("person")
        assert suppress is True
        assert reason == "coco_blacklist"

    def test_raw_coco_chair_suppressed(self) -> None:
        suppress, reason = should_suppress_yolo_class("chair")
        assert suppress is True
        assert reason == "coco_blacklist"

    def test_raw_coco_car_suppressed(self) -> None:
        suppress, _ = should_suppress_yolo_class("car")
        assert suppress is True

    def test_empty_class_suppressed(self) -> None:
        suppress, reason = should_suppress_yolo_class("")
        assert suppress is True
        assert reason == "empty_class"

    @pytest.mark.parametrize("name", ["GFCI", "duplex_receptacle", "circular_callout",
                                       "detail_callout", "rectangular_block", "legend_block",
                                       "PLUMBING", "ACCESSIBLE ENTRY"])
    def test_real_classes_pass_through(self, name: str) -> None:
        suppress, _ = should_suppress_yolo_class(name)
        assert suppress is False

    def test_suppress_set_includes_known_offenders(self) -> None:
        # Sanity check the blacklist hasn't been silently emptied.
        for offender in ("person", "chair", "door", "car", "bicycle", "dog"):
            assert offender in COCO_SUPPRESS_SET


# ---------------------------------------------------------------------------
# Interpretable fallback
# ---------------------------------------------------------------------------

class TestInterpretableFallback:
    def test_unknown_motif_high_conf_electrical(self) -> None:
        assert interpretable_fallback("unknown_motif_abc12345", "E", 0.91) == "electrical_symbol_high"

    def test_unknown_motif_mid_conf_plumbing(self) -> None:
        assert interpretable_fallback("unknown_motif_def67890", "P", 0.70) == "plumbing_symbol_mid"

    def test_unknown_motif_low_conf_architectural(self) -> None:
        assert interpretable_fallback("unknown_motif_00000000", "A", 0.55) == "architectural_symbol_low"

    def test_sig_prefix_also_handled(self) -> None:
        assert interpretable_fallback("sig:deadbeef", "M", 0.85) == "mechanical_symbol_high"

    def test_unknown_discipline_falls_back_to_general(self) -> None:
        assert interpretable_fallback("unknown_motif_x", None, 0.9) == "general_symbol_high"
        assert interpretable_fallback("unknown_motif_x", "Z", 0.5) == "general_symbol_low"

    def test_real_name_passes_through(self) -> None:
        # Already-meaningful names must not be rewritten by the fallback.
        assert interpretable_fallback("GFCI", "E", 0.9) == "GFCI"
        assert interpretable_fallback("duplex_receptacle", "E", 0.7) == "duplex_receptacle"
        assert interpretable_fallback("circular_callout", "A", 0.8) == "circular_callout"

    def test_band_boundaries(self) -> None:
        # >=0.85 high; >=0.65 mid; else low
        assert interpretable_fallback("unknown_motif_x", "E", 0.85) == "electrical_symbol_high"
        assert interpretable_fallback("unknown_motif_x", "E", 0.8499) == "electrical_symbol_mid"
        assert interpretable_fallback("unknown_motif_x", "E", 0.65) == "electrical_symbol_mid"
        assert interpretable_fallback("unknown_motif_x", "E", 0.6499) == "electrical_symbol_low"

    def test_is_opaque_name(self) -> None:
        assert is_opaque_name("unknown_motif_abc") is True
        assert is_opaque_name("sig:abcd1234") is True
        assert is_opaque_name("class_17") is True
        assert is_opaque_name("GFCI") is False
        assert is_opaque_name("circular_callout") is False
        assert is_opaque_name("") is False


# ---------------------------------------------------------------------------
# VGSE row builder: memory > legend > fallback, original_class preserved
# ---------------------------------------------------------------------------

class TestVGSERowBuilder:
    def _det(self, *, sheet_id: str, canonical: str, name_source: str,
             sig: str = "deadbeef" * 8, count: int = 3) -> dict:
        return {
            "sheet_id": sheet_id,
            "suite_id": "suite-a",
            "office_id": None,
            "signature_hash": sig,
            "canonical_name": canonical,
            "name_source": name_source,
            "count": count,
            "bbox_pt": [10.0, 20.0, 30.0, 40.0],
            "spatial_entropy": 1.8,
            "item_count": 5,
            "page_number": 1,
        }

    def test_memory_hit_overrides_fallback(self) -> None:
        det = self._det(sheet_id="sheet-1", canonical="GFCI", name_source="tenant_memory")
        rows = detections_to_blueprint_symbol_rows(
            [det],
            discipline_by_sheet_id={"sheet-1": "E"},
        )
        assert len(rows) == 1
        # Memory hit wins — class is the real name, NOT 'electrical_symbol_*'.
        assert rows[0]["class"] == "GFCI"
        assert rows[0]["bbox"]["original_class"] == "GFCI"
        assert rows[0]["bbox"]["name_source"] == "tenant_memory"
        assert rows[0]["confidence"] == 0.95

    def test_legend_hit_overrides_fallback(self) -> None:
        det = self._det(sheet_id="sheet-1", canonical="DUPLEX RECEPTACLE", name_source="legend")
        rows = detections_to_blueprint_symbol_rows(
            [det],
            discipline_by_sheet_id={"sheet-1": "E"},
        )
        assert rows[0]["class"] == "DUPLEX RECEPTACLE"
        assert rows[0]["bbox"]["name_source"] == "legend"

    def test_unanchored_motif_gets_fallback_name(self) -> None:
        # No canonical_name set → builder synthesizes 'sig:<8>' which is opaque
        # → fallback rewrites it using discipline + confidence band (0.60 = low).
        det = self._det(sheet_id="sheet-1", canonical="", name_source="unanchored",
                        sig="abcdef0123456789" * 4)
        rows = detections_to_blueprint_symbol_rows(
            [det],
            discipline_by_sheet_id={"sheet-1": "E"},
        )
        assert rows[0]["class"] == "electrical_symbol_low"
        # Original opaque name + signature_hash preserved for Wave 18.
        assert rows[0]["bbox"]["original_class"].startswith("sig:")
        assert rows[0]["bbox"]["signature_hash"] == "abcdef0123456789" * 4

    def test_fallback_varies_by_discipline(self) -> None:
        det_e = self._det(sheet_id="e", canonical="", name_source="unanchored")
        det_p = self._det(sheet_id="p", canonical="", name_source="unanchored")
        rows = detections_to_blueprint_symbol_rows(
            [det_e, det_p],
            discipline_by_sheet_id={"e": "E", "p": "P"},
        )
        classes = {r["sheet_id"]: r["class"] for r in rows}
        assert classes["e"].startswith("electrical_symbol_")
        assert classes["p"].startswith("plumbing_symbol_")

    def test_missing_discipline_map_uses_general(self) -> None:
        det = self._det(sheet_id="sheet-x", canonical="", name_source="unanchored")
        rows = detections_to_blueprint_symbol_rows([det])  # no map
        assert rows[0]["class"].startswith("general_symbol_")

    def test_signature_preserved_for_dedup(self) -> None:
        det = self._det(sheet_id="sheet-1", canonical="GFCI", name_source="tenant_memory",
                        sig="cafebabe" * 8)
        rows = detections_to_blueprint_symbol_rows(
            [det], discipline_by_sheet_id={"sheet-1": "E"},
        )
        assert rows[0]["bbox"]["signature_hash"] == "cafebabe" * 8
