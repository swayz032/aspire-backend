"""Wave 11g-v2 — YOLO Training Exporter.

Exports labeled symbols from ``blueprint_symbol_memory`` (Wave 11i/11g-v2) as a
YOLO-format dataset suitable for fine-tuning YOLOv11.

Output layout::

    <output_dir>/
    ├── images/
    │   ├── train/
    │   ├── val/
    │   └── test/
    ├── labels/
    │   ├── train/   *.txt  (one line per box: ``class_id cx cy w h`` normalized)
    │   ├── val/
    │   └── test/
    ├── data.yaml       — Ultralytics config (path, train/val/test, nc, names)
    └── manifest.json   — provenance, version, hash, exclusions

Strict include rules (Law #3 — fail closed):
    * source ∈ {legend, confirmation, tenant_memory}
    * confidence >= ``min_confidence`` (default 0.85)
    * confirmation_count >= ``min_confirmations`` OR source == 'legend'
    * deduplicate by ``graph_hash`` (one class per unique topology)

Strict exclude rules:
    * TRUE_UNKNOWN bucket
    * VECTOR_RECOVERABLE without resolution
    * VLM guesses below threshold
    * unresolved motifs

The exporter does NOT call any model — it only assembles existing labeled data.
Image bytes are loaded from the ``image_path`` field on the row (set by Track A
when crops are persisted to storage). Rows without an image_path are excluded.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from aspire_orchestrator.services.blueprint.wave_11g2_metrics import get_metrics

logger = logging.getLogger(__name__)

MODEL_VERSION = "wave_11g2.exporter.v1"

# ---------------------------------------------------------------------------
# Public dataclasses.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExportManifest:
    """Result of an export run — written to ``manifest.json`` and returned."""

    total_samples: int
    class_count: int
    train_count: int
    val_count: int
    test_count: int
    classes: list[str]
    excluded_samples: dict[str, int]
    output_path: str
    export_hash: str
    model_version: str
    created_at: str


@dataclass
class _SymbolRecord:
    """In-memory view of a memory row as consumed by the exporter."""

    suite_id: str
    graph_hash: str
    canonical_type: str | None
    canonical_name: str
    source: str
    confidence: float
    confirmation_count: int
    image_path: str | None
    bbox: tuple[float, float, float, float] | None  # (cx, cy, w, h) normalized
    rotation_orbit: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def export_yolo_dataset(
    *,
    suite_id: str | None = None,
    output_dir: str | Path,
    train_split: float = 0.70,
    val_split: float = 0.20,
    test_split: float = 0.10,
    min_confidence: float = 0.85,
    min_confirmations: int = 1,
    augmentation_hint: bool = True,
    rows: Iterable[dict[str, Any]] | None = None,
    rng_seed: int = 11_92,
    correlation_id: str | None = None,
) -> ExportManifest:
    """Materialise a YOLO training dataset from symbol memory.

    Parameters
    ----------
    suite_id:
        Filter to a single tenant. ``None`` = all tenants (platform-wide
        training corpus — used by Aspire's foundational model fine-tune).
    output_dir:
        Destination directory. Will be created. **Existing contents are wiped**
        to guarantee export reproducibility.
    train_split, val_split, test_split:
        Must sum to ~1.0; values are renormalized.
    min_confidence:
        Hard floor on row.confidence. Defaults to 0.85 per Law #3 fail-closed.
    min_confirmations:
        Hard floor on confirmation_count, UNLESS source='legend' (legend
        anchors are always trusted because the architect drew them).
    augmentation_hint:
        If True, emit the 4-rotation orbit in the manifest so downstream
        training scripts can do free augmentation without recomputation.
    rows:
        Optional pre-loaded iterable of memory rows. If None, the function
        loads from Supabase via the symbol_memory service. Tests pass a list
        directly to avoid network IO.
    rng_seed:
        Deterministic split shuffler. Same seed + same input = same split.
    correlation_id:
        Propagated to logs and receipts.

    Returns
    -------
    ExportManifest — also written to ``<output_dir>/manifest.json``.
    """
    metrics = get_metrics()
    output_path = Path(output_dir)

    splits = _normalise_splits(train_split, val_split, test_split)
    if rows is None:  # pragma: no cover — Supabase IO not exercised in tests
        rows = _load_rows_from_supabase(suite_id=suite_id, correlation_id=correlation_id)

    excluded: dict[str, int] = {
        "no_image_path": 0,
        "low_confidence": 0,
        "insufficient_confirmations": 0,
        "wrong_source": 0,
        "true_unknown": 0,
        "missing_canonical_type": 0,
        "duplicate_graph_hash": 0,
    }

    valid_sources = {"legend", "confirmation", "tenant_memory"}
    seen_graph_hashes: set[str] = set()
    records: list[_SymbolRecord] = []

    for raw in rows:
        rec = _coerce_row(raw)
        if rec is None:
            excluded["wrong_source"] += 1
            continue
        if rec.source not in valid_sources:
            excluded["wrong_source"] += 1
            continue
        if rec.source == "unresolved" or rec.canonical_type is None:
            excluded["true_unknown"] += 1
            continue
        if rec.confidence < min_confidence:
            excluded["low_confidence"] += 1
            continue
        if rec.source != "legend" and rec.confirmation_count < min_confirmations:
            excluded["insufficient_confirmations"] += 1
            continue
        if rec.canonical_type is None:
            excluded["missing_canonical_type"] += 1
            continue
        if not rec.image_path:
            excluded["no_image_path"] += 1
            continue
        if rec.graph_hash in seen_graph_hashes:
            # Dedup at the graph_hash level — same topology stays as one class.
            # We keep the first valid representative; alternatives are usable
            # for augmentation via rotation_orbit but not separate samples.
            excluded["duplicate_graph_hash"] += 1
            continue
        seen_graph_hashes.add(rec.graph_hash)
        records.append(rec)

    # Class registry — sorted for deterministic class_id assignment.
    classes = sorted({rec.canonical_type for rec in records if rec.canonical_type})
    class_to_id = {cls: idx for idx, cls in enumerate(classes)}

    rng = random.Random(rng_seed)
    rng.shuffle(records)
    n_total = len(records)
    n_train = math.floor(n_total * splits[0])
    n_val = math.floor(n_total * splits[1])
    # remaining → test (catches rounding crumbs)
    train_records = records[:n_train]
    val_records = records[n_train : n_train + n_val]
    test_records = records[n_train + n_val :]

    # Materialise directory layout.
    _ensure_clean_dir(output_path)
    for split in ("train", "val", "test"):
        (output_path / "images" / split).mkdir(parents=True, exist_ok=True)
        (output_path / "labels" / split).mkdir(parents=True, exist_ok=True)

    _write_split(output_path, "train", train_records, class_to_id)
    _write_split(output_path, "val", val_records, class_to_id)
    _write_split(output_path, "test", test_records, class_to_id)

    # data.yaml — Ultralytics format.
    data_yaml = _render_data_yaml(output_path=output_path, classes=classes)
    (output_path / "data.yaml").write_text(data_yaml, encoding="utf-8")

    # Hash of dataset (deterministic provenance).
    export_hash = _hash_dataset(records, classes)

    manifest = ExportManifest(
        total_samples=n_total,
        class_count=len(classes),
        train_count=len(train_records),
        val_count=len(val_records),
        test_count=len(test_records),
        classes=classes,
        excluded_samples=excluded,
        output_path=str(output_path),
        export_hash=export_hash,
        model_version=MODEL_VERSION,
        created_at=datetime.now(timezone.utc).isoformat(),
    )

    manifest_payload: dict[str, Any] = asdict(manifest)
    if augmentation_hint:
        manifest_payload["augmentation_orbit"] = {
            rec.graph_hash: rec.rotation_orbit for rec in records if rec.rotation_orbit
        }
    (output_path / "manifest.json").write_text(
        json.dumps(manifest_payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    metrics.inc(
        "yolo_training_samples_exported_total",
        value=float(n_total),
        correlation_id=correlation_id,
    )
    logger.info(
        "wave_11g2.training_exporter: exported %d samples (%d classes) to %s "
        "[train=%d val=%d test=%d] correlation_id=%s",
        n_total,
        len(classes),
        output_path,
        len(train_records),
        len(val_records),
        len(test_records),
        correlation_id,
    )

    return manifest


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _normalise_splits(train: float, val: float, test: float) -> tuple[float, float, float]:
    total = train + val + test
    if total <= 0:
        raise ValueError("training_exporter: splits sum to zero")
    return (train / total, val / total, test / total)


def _coerce_row(raw: dict[str, Any]) -> _SymbolRecord | None:
    try:
        bbox = raw.get("bbox")
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            bbox_tuple: tuple[float, float, float, float] | None = (
                float(bbox[0]),
                float(bbox[1]),
                float(bbox[2]),
                float(bbox[3]),
            )
        else:
            bbox_tuple = None
        return _SymbolRecord(
            suite_id=str(raw.get("suite_id", "")),
            graph_hash=str(raw.get("graph_hash") or raw.get("signature_hash") or ""),
            canonical_type=raw.get("canonical_type"),
            canonical_name=str(raw.get("canonical_name", "")),
            source=str(raw.get("source", "")),
            confidence=float(raw.get("confidence") or 0.0),
            confirmation_count=int(raw.get("confirmation_count") or 0),
            image_path=raw.get("image_path"),
            bbox=bbox_tuple,
            rotation_orbit=list(raw.get("wl_rotation_orbit") or []),
        )
    except (TypeError, ValueError):
        return None


def _ensure_clean_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _write_split(
    output_path: Path,
    split: str,
    records: list[_SymbolRecord],
    class_to_id: dict[str, int],
) -> None:
    images_dir = output_path / "images" / split
    labels_dir = output_path / "labels" / split
    for idx, rec in enumerate(records):
        stem = f"{rec.graph_hash[:16]}_{idx:06d}"
        # Image — copy if local path exists, else write a sidecar pointer.
        src_image = Path(rec.image_path) if rec.image_path else None
        if src_image and src_image.exists():
            dst_image = images_dir / f"{stem}{src_image.suffix or '.png'}"
            shutil.copy2(src_image, dst_image)
        else:
            # Remote URI — write a pointer file the training script must resolve.
            (images_dir / f"{stem}.uri").write_text(rec.image_path or "", encoding="utf-8")

        # Label — YOLO format: class_id cx cy w h (normalized).
        class_id = class_to_id.get(rec.canonical_type or "", -1)
        if class_id < 0:
            continue
        if rec.bbox is None:
            # Whole-image label (single object centered & full size).
            label_line = f"{class_id} 0.5 0.5 1.0 1.0"
        else:
            cx, cy, w, h = rec.bbox
            label_line = f"{class_id} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}"
        (labels_dir / f"{stem}.txt").write_text(label_line + "\n", encoding="utf-8")


def _render_data_yaml(*, output_path: Path, classes: list[str]) -> str:
    # Hand-rendered YAML so we don't depend on PyYAML.
    names_lines = "\n".join(f"  {idx}: {cls}" for idx, cls in enumerate(classes))
    return (
        f"# Auto-generated by {MODEL_VERSION} on {datetime.now(timezone.utc).isoformat()}\n"
        f"path: {output_path.as_posix()}\n"
        "train: images/train\n"
        "val: images/val\n"
        "test: images/test\n"
        f"nc: {len(classes)}\n"
        "names:\n"
        f"{names_lines}\n"
    )


def _hash_dataset(records: list[_SymbolRecord], classes: list[str]) -> str:
    h = hashlib.sha1()
    h.update(MODEL_VERSION.encode())
    for cls in classes:
        h.update(cls.encode())
    for rec in records:
        h.update(rec.graph_hash.encode())
        h.update((rec.canonical_type or "").encode())
    return h.hexdigest()


def _load_rows_from_supabase(
    *,
    suite_id: str | None,
    correlation_id: str | None,
) -> list[dict[str, Any]]:  # pragma: no cover — production-only IO path
    """Load eligible rows from blueprint_symbol_memory via Supabase.

    Kept thin so tests can inject ``rows=...`` directly. Real wiring imports
    ``supabase_client`` inside this function to keep test runs IO-free.
    """
    from aspire_orchestrator.services.supabase_client import supabase_select

    filters = "confidence=gte.0.85"
    if suite_id:
        filters = f"suite_id=eq.{suite_id}&{filters}"

    try:
        rows = await_supabase_select("blueprint_symbol_memory", filters)
    except Exception as exc:
        logger.warning(
            "training_exporter: supabase load failed (correlation_id=%s): %s",
            correlation_id,
            exc,
        )
        return []
    return rows or []


def await_supabase_select(table: str, filters: str) -> list[dict[str, Any]]:  # pragma: no cover
    """Tiny sync wrapper — production hands this off to the async select.

    Kept as a seam so the public ``export_yolo_dataset`` stays synchronous; the
    async Supabase client is awaited only when ``rows`` was not pre-provided.
    """
    import asyncio

    from aspire_orchestrator.services.supabase_client import supabase_select

    return asyncio.run(supabase_select(table, filters=filters, limit=100_000)) or []


__all__ = ["ExportManifest", "export_yolo_dataset", "MODEL_VERSION"]
