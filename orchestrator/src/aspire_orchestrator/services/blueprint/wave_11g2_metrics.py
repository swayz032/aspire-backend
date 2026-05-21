"""Wave 11g-v2 — Observability metrics for the WL motif pipeline.

This module exposes a single ``Wave11g2Metrics`` class that every Track A,
Track B, and Track C module uses to emit counters and histograms.

Design constraints:
    * Fail-open if the Prometheus client is missing — fall back to a thread-safe
      in-memory dict so the pipeline never breaks on a missing optional dep.
    * Every counter and histogram is namespaced under ``wave_11g2_``.
    * Correlation IDs are carried per-call, not per-metric — callers must pass
      ``correlation_id=...`` to every public function in the 11g-v2 modules
      and emit it on receipts/logs.
    * No PII, no suite_id in label cardinality — labels are coarse buckets
      only (``source``, ``bucket``, ``mode``) to keep Prometheus series count
      bounded.

Public API:
    metrics = get_metrics()
    metrics.inc("vector_pages_total")
    metrics.inc("unknown_motifs_total", labels={"bucket": "TRUE_UNKNOWN"})
    metrics.observe("topology_match_latency_ms", value=12.4)
    snapshot = metrics.snapshot()  # dict[str, int|float] for log dumps

The snapshot view is what the FastAPI /health/metrics endpoint and acceptance
tests inspect; the Prometheus view (if available) is what Grafana scrapes.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional Prometheus client — fail-open if missing.
# ---------------------------------------------------------------------------
try:  # pragma: no cover — import side-effect, exercised in production only
    from prometheus_client import Counter as _PromCounter
    from prometheus_client import Histogram as _PromHistogram

    _PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PromCounter = None  # type: ignore[assignment]
    _PromHistogram = None  # type: ignore[assignment]
    _PROMETHEUS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Catalog — every counter / histogram declared in one place.
# Keeping the catalog explicit avoids accidental label-cardinality blowups.
# ---------------------------------------------------------------------------

_COUNTER_CATALOG: dict[str, tuple[str, tuple[str, ...]]] = {
    # name: (help_text, label_names)
    "vector_pages_total": (
        "Pages classified VECTOR_NATIVE and processed by vector_extractor.",
        (),
    ),
    "raster_pages_total": (
        "Pages classified RASTER_SCAN and processed by raster fallback.",
        (),
    ),
    "hybrid_pages_total": (
        "Pages classified HYBRID (vector primitives + raster OCR overlay).",
        (),
    ),
    "motifs_discovered_total": (
        "Frequent motifs returned by the WL miner.",
        (),
    ),
    "unknown_motifs_total": (
        "Motifs the resolver could not classify, bucketed by recoverability.",
        ("bucket",),  # VECTOR_RECOVERABLE | RASTER_RECOVERABLE | TRUE_UNKNOWN
    ),
    "legend_matches_total": (
        "Motifs resolved via legend_anchor (architect's explicit legend region).",
        (),
    ),
    "retrieval_hit_rate": (
        "Successful retrieval resolutions, by source.",
        ("source",),  # legend | tenant_memory | ocr | unresolved
    ),
    "entropy_rejections_total": (
        "Motifs rejected for low geometric entropy (likely background noise).",
        (),
    ),
    "yolo_training_samples_exported_total": (
        "Symbol rows emitted to YOLO training datasets by the exporter.",
        (),
    ),
}

_HISTOGRAM_CATALOG: dict[str, tuple[str, tuple[str, ...], tuple[float, ...]]] = {
    "topology_match_latency_ms": (
        "Latency to match a candidate motif against tenant memory (ms).",
        (),
        (1.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1_000.0, 5_000.0),
    ),
}


# ---------------------------------------------------------------------------
# Implementation.
# ---------------------------------------------------------------------------


class Wave11g2Metrics:
    """Thread-safe metrics emitter for the Wave 11g-v2 pipeline.

    Use ``get_metrics()`` to retrieve the process-wide singleton — do not
    instantiate directly.
    """

    _NAMESPACE = "wave_11g2_"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], list[float]] = defaultdict(list)
        self._prom_counters: dict[str, Any] = {}
        self._prom_histograms: dict[str, Any] = {}

        if _PROMETHEUS_AVAILABLE:
            self._init_prometheus()

    # -- Prometheus initialisation -----------------------------------------

    def _init_prometheus(self) -> None:
        for name, (help_text, labels) in _COUNTER_CATALOG.items():
            try:
                self._prom_counters[name] = _PromCounter(
                    f"{self._NAMESPACE}{name}",
                    help_text,
                    list(labels),
                )
            except ValueError:
                # Counter already registered — re-import scenario (e.g. tests).
                logger.debug("wave_11g2_metrics: counter %s already registered", name)

        for name, (help_text, labels, buckets) in _HISTOGRAM_CATALOG.items():
            try:
                self._prom_histograms[name] = _PromHistogram(
                    f"{self._NAMESPACE}{name}",
                    help_text,
                    list(labels),
                    buckets=buckets,
                )
            except ValueError:
                logger.debug("wave_11g2_metrics: histogram %s already registered", name)

    # -- Public API --------------------------------------------------------

    def inc(
        self,
        name: str,
        *,
        value: float = 1.0,
        labels: dict[str, str] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        """Increment counter ``name``. Fail-open if catalog miss."""
        if name not in _COUNTER_CATALOG:
            logger.warning("wave_11g2_metrics: unknown counter %s (correlation_id=%s)", name, correlation_id)
            return

        labels = labels or {}
        label_tuple = tuple(sorted(labels.items()))

        with self._lock:
            self._counters[(name, label_tuple)] += value

        if _PROMETHEUS_AVAILABLE and name in self._prom_counters:
            try:
                counter = self._prom_counters[name]
                if labels:
                    counter.labels(**labels).inc(value)
                else:
                    counter.inc(value)
            except (ValueError, KeyError) as exc:  # pragma: no cover
                logger.debug("wave_11g2_metrics: prom inc failed for %s: %s", name, exc)

    def observe(
        self,
        name: str,
        *,
        value: float,
        labels: dict[str, str] | None = None,
        correlation_id: str | None = None,
    ) -> None:
        """Observe a histogram sample. Fail-open if catalog miss."""
        if name not in _HISTOGRAM_CATALOG:
            logger.warning("wave_11g2_metrics: unknown histogram %s (correlation_id=%s)", name, correlation_id)
            return

        labels = labels or {}
        label_tuple = tuple(sorted(labels.items()))

        with self._lock:
            self._histograms[(name, label_tuple)].append(value)

        if _PROMETHEUS_AVAILABLE and name in self._prom_histograms:
            try:
                hist = self._prom_histograms[name]
                if labels:
                    hist.labels(**labels).observe(value)
                else:
                    hist.observe(value)
            except (ValueError, KeyError) as exc:  # pragma: no cover
                logger.debug("wave_11g2_metrics: prom observe failed for %s: %s", name, exc)

    def snapshot(self) -> dict[str, float]:
        """Return a flat dict snapshot suitable for logs and acceptance tests.

        Counter keys are ``"<name>{label1=val1,label2=val2}"``; histogram keys
        are ``"<name>_count{...}"``, ``"<name>_sum{...}"``, ``"<name>_avg{...}"``.
        """
        out: dict[str, float] = {}
        with self._lock:
            for (name, label_tuple), value in self._counters.items():
                out[_format_key(name, label_tuple)] = value

            for (name, label_tuple), values in self._histograms.items():
                key_root = _format_key(name, label_tuple)
                out[f"{key_root}__count"] = float(len(values))
                out[f"{key_root}__sum"] = float(sum(values))
                out[f"{key_root}__avg"] = float(sum(values) / len(values)) if values else 0.0
        return out

    def reset_for_tests(self) -> None:
        """ONLY for tests — wipes in-memory counters. Prom client unaffected."""
        with self._lock:
            self._counters.clear()
            self._histograms.clear()


def _format_key(name: str, label_tuple: tuple[tuple[str, str], ...]) -> str:
    if not label_tuple:
        return name
    label_str = ",".join(f"{k}={v}" for k, v in label_tuple)
    return f"{name}{{{label_str}}}"


# ---------------------------------------------------------------------------
# Process-wide singleton.
# ---------------------------------------------------------------------------

_METRICS_SINGLETON: Wave11g2Metrics | None = None
_SINGLETON_LOCK = threading.Lock()


def get_metrics() -> Wave11g2Metrics:
    """Return the process-wide Wave11g2Metrics singleton."""
    global _METRICS_SINGLETON
    if _METRICS_SINGLETON is None:
        with _SINGLETON_LOCK:
            if _METRICS_SINGLETON is None:
                _METRICS_SINGLETON = Wave11g2Metrics()
    return _METRICS_SINGLETON


__all__ = ["Wave11g2Metrics", "get_metrics"]
