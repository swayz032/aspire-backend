---
name: wave-20-6-embedder-patterns
description: Wave 20.6.3 Symbol Family Embedder — 64-dim structural embedding design, TopologyGraph interface, serialization, clustering patterns
metadata:
  type: project
---

## Symbol Family Embedder (Stage 3) — Key Patterns

### TopologyGraph is defined in symbol_family_embedder.py
Stage 2 (canonical_symbolizer) imports TopologyGraph FROM symbol_family_embedder — one source of truth. Do NOT define it in Stage 2. This prevents circular imports and keeps the contract stable.

### 64-dim feature layout (all dims normalized to [0,1])
- Dims 0-15: Geometry (node/edge/face counts, primitive type histogram, length/degree stats, bbox metrics)
- Dims 16-31: Edge graph (degree distribution histogram 8 bins, edge-type co-occurrence 8 dims)
- Dims 32-47: Topology (Euler characteristic, Betti numbers b0/b1, planarity, genus)
- Dims 48-55: Enclosed regions (area quartiles, std/skew/count/coverage)
- Dims 56-63: Symmetry (axis count, rotation order, mirror flag, reflective score, 4 axis scores)

### Deterministic — no ML required
Construction symbols are vector glyphs with computable structural properties. A neural embedding would require training data we don't have. Computed features capture topology/geometry/symmetry that defines a symbol family.

### Serialization: base64 float32
serialize_embedding / deserialize_embedding use struct.pack(f"{64}f", ...) → base64. Exact lossless roundtrip. Stored in blueprint_project_legends.vector_signature (text column).

### Clustering: union-find within discipline only
cluster_legend_into_families uses union-find with cosine_similarity >= 0.92 (default). Only clusters within the same discipline field — never across disciplines (electrical symbols won't contaminate plumbing families). Returns only families with >= 2 members; singletons get no family_id.

### family_id format: 'fam_<sha1[:12] of root_id>'
Deterministic across re-runs — same input → same family_id. Stable for receipts and downstream references.

### Gulfstream validation caveat
blueprint_project_legends is populated by Stage 1 (extract_project_legends). Gulfstream has 39 intel rows / 17 with legend regions — upstream data is ready. The Gulfstream query returns 0 until Stage 1 is triggered. This is expected; the embedding pipeline is end-to-end correct.

### Railway CLI silent output on Windows
`railway run -- python -c "..."` produces no output on Windows PowerShell or Bash. Use Supabase MCP (execute_sql) for DB-state validation instead of railway run scripts.

**Why:** Railway CLI on Windows silently suppresses stdout/stderr for `railway run` subcommands. Known issue; use DB queries to validate pipeline state.
**How to apply:** Always validate DB state via mcp__supabase__execute_sql rather than railway run when running quick checks.

[[wave-20-6-legend-extractor-patterns]]
