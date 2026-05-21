# Wave 11g-v2 — Observability Catalog

All counters/histograms are namespaced `wave_11g2_*` and exported via
`prometheus_client` when available; otherwise via the in-memory snapshot.

## Counters

| Name | Labels | Units | Meaning |
| --- | --- | --- | --- |
| `wave_11g2_vector_pages_total` | — | pages | Pages classified VECTOR_NATIVE and processed. |
| `wave_11g2_raster_pages_total` | — | pages | Pages classified RASTER_SCAN. |
| `wave_11g2_hybrid_pages_total` | — | pages | Pages classified HYBRID. |
| `wave_11g2_motifs_discovered_total` | — | motifs | Frequent motifs surfaced by the miner. |
| `wave_11g2_unknown_motifs_total` | `bucket` ∈ {VECTOR_RECOVERABLE, RASTER_RECOVERABLE, TRUE_UNKNOWN} | motifs | Motifs the resolver could not classify, bucketed. |
| `wave_11g2_legend_matches_total` | — | motifs | Motifs resolved via legend_anchor. |
| `wave_11g2_retrieval_hit_rate` | `source` ∈ {legend, tenant_memory, ocr, unresolved} | events | Resolution events tagged by source. |
| `wave_11g2_entropy_rejections_total` | — | motifs | Motifs rejected by the entropy gate (background noise). |
| `wave_11g2_yolo_training_samples_exported_total` | — | samples | Total samples emitted by the YOLO exporter. |

## Histograms

| Name | Labels | Units | Buckets | Meaning |
| --- | --- | --- | --- | --- |
| `wave_11g2_topology_match_latency_ms` | — | ms | 1, 5, 10, 25, 50, 100, 250, 500, 1k, 5k | Latency to match a candidate motif against tenant memory. |

## Correlation IDs

- Every public function in 11g-v2 modules accepts `correlation_id: str`.
- Correlation IDs flow through logs and receipts but **never** become Prometheus
  labels (would explode cardinality). Use Grafana → Loki/Tempo linkage to
  correlate metrics ↔ logs ↔ traces by trace ID instead.

## Suggested Grafana panels (free tier)

1. **Pipeline throughput** — `rate(wave_11g2_vector_pages_total[5m])` etc., one line per mode.
2. **Resolution mix** — `rate(wave_11g2_retrieval_hit_rate{source="legend"}[5m])` vs `unresolved`.
3. **TRUE_UNKNOWN rate** — `rate(wave_11g2_unknown_motifs_total{bucket="TRUE_UNKNOWN"}[15m])` — alert if > N/min for a tenant.
4. **Match latency p95** — `histogram_quantile(0.95, rate(wave_11g2_topology_match_latency_ms_bucket[5m]))`.
5. **Training corpus growth** — `wave_11g2_yolo_training_samples_exported_total` cumulative.

## Alertmanager rules (suggested)

```yaml
- alert: Wave11g2TrueUnknownSpike
  expr: rate(wave_11g2_unknown_motifs_total{bucket="TRUE_UNKNOWN"}[15m]) > 5
  for: 10m
  annotations:
    summary: TRUE_UNKNOWN bucket growing fast — investigate novel-architect ingest
    runbook: WAVE_11G2_ROLLOUT.md#true_unknown_spike

- alert: Wave11g2MatchLatencyP95High
  expr: histogram_quantile(0.95, rate(wave_11g2_topology_match_latency_ms_bucket[5m])) > 500
  for: 5m
  annotations:
    summary: Topology match p95 latency above 500ms
    runbook: WAVE_11G2_ROLLOUT.md#match_latency
```

## Fail-open behaviour

If `prometheus_client` is not installed, `Wave11g2Metrics` falls back to a
thread-safe in-memory dict. The pipeline never breaks on a missing optional
dependency. `snapshot()` always returns a flat `dict[str, float]` suitable
for the `/health/metrics` JSON endpoint.
