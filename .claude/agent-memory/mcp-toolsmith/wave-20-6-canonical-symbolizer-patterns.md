---
name: wave-20-6-canonical-symbolizer-patterns
description: Wave 20.6 Stage 2 — Canonical Symbolization implementation lessons: global coordinate frame bug, WL hash edge-type discrimination, topology extraction patterns
metadata:
  type: project
---

## Wave 20.6 Stage 2 — Canonical Symbolization Patterns

### Critical Bug: Per-primitive unit-square normalization destroys shared topology

**Problem:** Calling `_to_unit_square()` per-primitive independently makes each line segment fill [0,1]×[0,1] on its own. A square's 4 corners collapse to 3 unique snap keys instead of 4 because adjacent lines share endpoints that become coincident at (0,0).

**Fix:** Rotate ALL points globally, compute one shared bounding box, then apply one translation+scale to every primitive. This preserves relative positions across primitives.

```python
# WRONG — destroys shared topology
for prim in primitives:
    rotated = _rotate_points(prim.points, angle)
    unit = _to_unit_square(rotated)  # each prim independently fills unit square

# CORRECT — global coordinate frame
all_rotated = _rotate_points(all_pts, angle)
x0 = min(p[0] for p in all_rotated)
y0 = min(p[1] for p in all_rotated)
span = max(max(p[0] for p in all_rotated) - x0, max(p[1] for p in all_rotated) - y0)
# Then apply same (x - x0) / span transform to each primitive's rotated points
```

### Critical Bug: WL hash using only node degree conflates arc-cycles with line-cycles

**Problem:** A 4-arc circle approximation (○) and a 4-line square (□) both form 4-cycles with degree-2 nodes. Pure degree-based WL hash produces identical signatures for both.

**Fix:** Incorporate edge primitive_type into the initial WL node label. Nodes with incident arc edges get a different starting label than nodes with incident line edges.

```python
# WRONG — degree-only initial label
labels = {n.node_id: str(n.degree) for n in nodes}

# CORRECT — degree + sorted incident edge types
edge_types_by_node = {n.node_id: [] for n in nodes}
for e in edges:
    edge_types_by_node[e.from_node].append(e.primitive_type)
    edge_types_by_node[e.to_node].append(e.primitive_type)

labels = {
    n.node_id: f"{n.degree}:{','.join(sorted(edge_types_by_node[n.node_id]))}"
    for n in nodes
}
```

Also propagate edge types through WL iterations:
```python
neighbour_labels = sorted(f"{labels[nbr]}:{etype}" for nbr, etype in adj[nid])
```

### Symmetry Detection: 85% reflection threshold works for construction symbols

- Test 4 candidate axes: vertical, horizontal, diagonal_pos, diagonal_neg
- For each axis: count how many nodes have a mirror partner within `tolerance` (default 0.05 of unit square)
- Score = matched / total. Threshold 0.85 catches real symmetry while ignoring noise.
- A square after normalization correctly gets 4 axes, an L-shape gets 0.

### Snap Grid for Node Merging

- Use 2% of unit square (0.02) as the snap grid for extracting topology nodes
- This collapses vector precision noise without merging genuinely distinct corners
- Implemented via `_snap_key(x, y, grid=0.02)` → `(int(round(x/grid)), int(round(y/grid)))`

### Graph Simplification: Signature Changes When Node Degrees Change

**Problem:** `simplify_graph()` changes the graph (merging nodes, removing dangling edges) which changes degrees. If you recompute WL from the new graph, the signature will differ from the original even for "no-op" simplification — unless you recompute degrees consistently from the new edge set.

**Fix:** Always recompute degrees from the final edge set before calling `_wl_hash()`. The test for "signature preserved" only holds when the graph is structurally unchanged — ensure `simplify_graph()` with no text regions and no noise produces the exact same node/edge set as the original, then the signature will match.

### numpy Availability Note

- numpy 2.4.5 is transitively available via ultralytics/opencv.
- Not declared explicitly in pyproject.toml.
- Safe to use for PCA (`np.cov`, `np.linalg.eigh`) in blueprint services.
- Stage 3 embedder should be aware of this implicit dependency.

### Test Location

Blueprint service tests go in:
`orchestrator/tests/services/blueprint/test_*.py`

Not in `tests/services/blueprint/` (that path doesn't exist in this repo).

### graph_signature Contract for Stage 3

- 40-char hex SHA1 from WL k=3 hash
- Invariant to: rotation, scale, stroke width
- Sensitive to: edge primitive_type (arc vs line), topology (node/edge counts, connectivity)
- Use as clustering key for Symbol Family Embeddings (Stage 3)
- `TopologyGraph.symmetry_axes` provides additional features for embedding enrichment
