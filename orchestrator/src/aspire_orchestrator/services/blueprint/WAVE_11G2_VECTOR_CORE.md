# Wave 11g-v2 Track A — Vector Intelligence Core (Phases 1-3)

## Why this exists

Wave 11g VGSE hashed each motif by the **raw item sequence** of a PyMuPDF
drawing. When an outlet was drawn separately from its connecting wires
the hash was stable; when the outlet was merged into a path that also
described the wire run, the hash mutated. Result on the real GAVNN
project: 383 of 463 detected symbols ended up as
`unknown_motif_<sig8>`.

Track A replaces the brittle sequence-hash with a **rotation+scale-
invariant primitive ID + a graph topology** that Track B can lift into
Weisfeiler-Lehman neighbourhood labels. Two identical outlets connected
to different wire bundles now share the same primitive_id and produce
isomorphic local neighbourhoods.

## Three modules, one pipeline

```
PDF page
  │
  ▼
┌────────────────────────────┐
│ document_classifier.py     │  Phase 1
│ classify_page(page)        │  → PageClassification
└────────────────────────────┘
  │   page_mode ∈ {VECTOR_NATIVE, VECTOR_DEGRADED, HYBRID, RASTER_SCAN}
  ▼
┌────────────────────────────┐
│ vector_extractor.py        │  Phase 2
│ extract_primitives(page)   │  → list[VectorPrimitive]
└────────────────────────────┘
  │   primitive_id is translation+scale+rotation invariant
  │   transform_signature is rotation invariant (lex-min radial buckets)
  ▼
┌────────────────────────────┐
│ vector_graph_builder.py    │  Phase 3
│ build_graph(primitives)    │  → VectorGraph
└────────────────────────────┘
      nodes = canonical endpoints (after snap-merge)
      edges = primitives with metadata payload
      graph.graph = {page_number, total_primitives,
                     snap_tolerance, rotation_invariant}
```

Track B consumes `VectorGraph` and computes WL labels by repeatedly
hashing (`transform_signature`, sorted-neighbour-labels) tuples.

## Page-mode decision matrix (Phase 1)

| Mode             | drawing_count | raster_density   | Confidence shape |
|------------------|--------------:|------------------|------------------|
| VECTOR_NATIVE    | ≥ 30          | < 0.10           | grows with depth and gap below raster threshold |
| HYBRID           | ≥ 10          | ≥ 0.20           | grows with both depths |
| VECTOR_DEGRADED  | otherwise, low raster, < 30 drawings | low | bounded 0.3–0.8 |
| RASTER_SCAN      | ≤ 5  OR  vector_density < 0.05 | any  | high when raster strong, low when raster weak |

Fail-closed (Law #3): when `drawing_count == 0 AND raster_density == 0 AND
vector_density == 0`, returns `RASTER_SCAN` with `classification_confidence=0.0`
and emits a WARNING log line so operators can investigate. Never silently
fall through.

`vector_density` is normalised against page area: drawings per pt² /
0.01 (saturating). `raster_density` is sum-of-image-rect-area / page area.
Both clamped to [0, 1].

## Primitive type → graph mapping (Phase 3)

| Primitive type | Graph contribution                                      |
|---------------:|---------------------------------------------------------|
| line           | 1 edge (start → end)                                    |
| polyline       | 1 edge (first → last) — interior points collapse to length-along-edge metadata (Track B can expand later) |
| curve          | 1 edge (first → last anchor point)                      |
| rect           | 4 edges around the 4 corners                            |
| circle         | 1 degenerate node at centre, **0 edges** (single-node primitives still surface in WL hashing via node label) |
| path           | 1 edge (first → last)                                   |

All endpoints are merged into a single node when within `snap_tolerance`
(default 0.5pt). Snap lookup is a 3×3 grid neighbourhood scan, giving
amortised O(1) per query. We deliberately do **not** depend on
`scipy.spatial.cKDTree` because scipy is not in `pyproject.toml` — see
"Deps note" below.

## Invariances

* **Translation**: `_normalise()` translates points to the primitive's
  bbox origin before hashing — same shape at any position ⇒ same
  `primitive_id`.
* **Scale**: `_normalise()` divides by the bbox long-side — same shape
  at any size ⇒ same `primitive_id` (within quantisation grid 64).
* **Rotation**: `_rotation_invariant_signature()` builds a 36-bucket
  radial histogram from centroid, then picks the lex-min rotation of
  the bucket ring. Identical under 0/90/180/270 rotation exactly;
  approximately invariant under arbitrary rotation modulo bucketing.

## Provenance (Law #2)

Every record can be reconstructed from a receipt:

* `PageClassification` → `page_number`, `page_mode`,
  `classification_confidence`, all 4 densities and counts.
* `VectorPrimitive` → `primitive_id` (translation+scale invariant),
  `transform_signature` (rotation invariant), `hash` (full identity
  including stroke + fill), `page_number`.
* `VectorGraph.graph` → `page_number`, `total_primitives`,
  `snap_tolerance`, `rotation_invariant`. The build is reproducible
  byte-for-byte from these settings + the primitive list.

## PII / log discipline (Law #9)

No raw drawing coordinates are ever logged. Log messages use:

* `primitive_id[:8]` — 8-char hash prefix
* counts and page numbers
* density floats rounded to 3 decimals

## Deps note (no new external dependencies)

The brief permitted `networkx`, `scipy`, `sklearn` "since they are
likely already in pyproject.toml for VGSE clustering". They are **not**.
Adding any of them would violate the no-new-deps mandate, so Phase 3
ships a minimal pure-Python graph (`VectorGraph`) that exposes the
networkx-compatible read surface Track B needs:

```python
g.nodes                # list[int]
g.edges                # list[(u, v)]
g.graph                # dict[str, object]
g.neighbors(n)         # Iterator[int]
g.node_data[n]         # _NodeData (x, y, primitive_ids[])
g.edge_data[(u, v)]    # _EdgeData (primitive_id, type, transform_signature, ...)
g.connected_components()  # list[set[int]]
n in g, len(g), g.number_of_nodes(), g.number_of_edges()
```

If networkx is added to pyproject.toml later, Track B can swap
`VectorGraph` for `networkx.Graph` and the only changes are to the
attribute-access patterns inside `vector_graph_builder.build_graph` —
the public API stays.

## Files

```
orchestrator/src/aspire_orchestrator/services/blueprint/
  document_classifier.py
  vector_extractor.py
  vector_graph_builder.py
  WAVE_11G2_VECTOR_CORE.md   (this file)

orchestrator/tests/
  test_document_classifier.py
  test_vector_extractor.py
  test_vector_graph_builder.py
```

The existing `vgse.py` / `vgse_pipeline.py` are **untouched**. Wave
11g-v2 runs alongside Wave 11j for now; integration is a separate
downstream wave.
