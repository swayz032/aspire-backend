# Wave 11g-v2 Track B — WL Subgraph Mining + Retrieval Fusion + Unknown Resolver

Phases 4, 6, 7, 8 of the 12-phase plan.

Branch: `feat/wave-11g2-wl-mining`. **Not committed.**

## Files

- `orchestrator/src/aspire_orchestrator/services/blueprint/subgraph_miner.py` — Phase 4
- `orchestrator/src/aspire_orchestrator/services/blueprint/symbol_retrieval.py` — Phase 6
- `orchestrator/src/aspire_orchestrator/services/blueprint/legend_anchor_v2.py` — Phase 7
- `orchestrator/src/aspire_orchestrator/services/blueprint/unknown_motif_resolver.py` — Phase 8
- `orchestrator/tests/test_subgraph_miner.py`
- `orchestrator/tests/test_symbol_retrieval.py`
- `orchestrator/tests/test_legend_anchor_v2.py`
- `orchestrator/tests/test_unknown_motif_resolver.py`

## Why WL hashing is rotation/scale-invariant

The 1-Weisfeiler-Lehman colour-refinement algorithm produces a graph
fingerprint by iteratively recolouring every node with a hash of `(own_label,
sorted(neighbour_labels))`. After N rounds we hash the multiset of node
colours per round and concatenate. The output depends ONLY on the local
topology of the induced subgraph — never on:

- **Where the subgraph sits on the page** (no coordinates are read into the
  initial labels — only degree, primitive type, and a 12-bucket angle code).
- **What's connected to the boundary** because we mine over the k-hop
  *induced* subgraph; edges leaving the neighborhood are explicitly dropped
  in `_wl_hash_subgraph` before iteration.
- **Scale.** Length is read into the embedding (for the retrieval cascade),
  not into the WL hash itself. Two GFCIs drawn at different scales share
  the same hash.

For rotation: we compute the WL hash four times — adding 0°, 90°, 180°, 270°
to every node's angle — and store all four hashes in `rotation_orbit`. The
canonical key is `min(orbit)` (deterministic). A drawing rotated 90° in
production matches against an orbit member, not the canonical hash directly,
so we accept either. `match_motif_to_anchors` checks orbit overlap before
falling back to cosine similarity.

Net effect: the symbol that VGSE called `unknown_motif_8a3f...` because a
wire was attached now hashes identically to the wire-free legend exemplar.

## Why the retrieval cascade preserves provenance

`resolve_motif` runs five sources in confidence order:

1. Legend topology match    (0.95)
2. Tenant memory hit        (0.90)
3. OCR proximity → lexicon  (0.80)
4. VLM hook (off by default) (0.50)
5. TRUE_UNKNOWN             (0.00)

Every source that was queried — whether it hit or missed — is appended to
`ResolvedSymbol.retrieval_matches`. The receipt ledger sees:

```python
[
  {"source": "legend",        "hit": False, "confidence": 0.0, "detail": {}},
  {"source": "tenant_memory", "hit": True,  "confidence": 0.90, "detail": {"match": "graph_hash_exact", ...}},
  ...
]
```

This means a downstream auditor can answer "why did this motif get labelled
`gfci_receptacle`?" without re-running anything — the audit trail says:
"legend missed, tenant memory hit on exact graph_hash from suite-A". This
satisfies Law #2 (every state change carries an immutable receipt).

A motif that resolves to `source="unresolved"` STILL writes an audit row —
the unknown is a first-class result, not the absence of one. That keeps the
receipt invariant: 100% coverage.

## Why TRUE_UNKNOWN is allowed to remain unknown

Law #3 — Fail Closed. Three reasons:

1. **No silent inference.** If WL hashing, tenant memory, OCR proximity, and
   the legend cascade ALL miss, the only honest answer is "I don't know."
   Forcing a name (even "unknown_motif_*") gives downstream estimators
   pricing signal where none should exist.
2. **The bucket decides next action, not the name.** The
   `UnknownMotifResolver` returns one of three buckets:
   - `VECTOR_RECOVERABLE` (high-entropy embedding) → escalate to VLM or
     contractor review.
   - `RASTER_RECOVERABLE` (HYBRID / RASTER_SCAN page) → defer to raster
     pipeline.
   - `TRUE_UNKNOWN` (low entropy, vector page) → accept. The drawing is
     topologically indistinct and forcing it would hurt more than helping.
3. **Test enforcement.** `test_unknown_motif_resolver::TestFailClosed`
   asserts that `canonical_type is None` implies `confidence == 0.0` and
   vice versa. The cascade cannot ship a name with conf=0 or a None with
   conf>0.

## Track A integration contract

This wave does NOT import Track A's modules — they live in a different
worktree. We accept duck-typed inputs at the boundary:

- `mine_motifs(graph)` accepts anything that quacks like `networkx.Graph`:
  `nodes(data=True)`, `neighbors(n)`. Tests use a 30-line `DictGraph`.
- `extract_legend_anchors(page, primitives, ..., legend_rows=...)` lets the
  caller bypass the page-parsing fallback by supplying pre-built rows.
- `resolve_motif(motif, *, tenant_memory_lookup=...)` takes the tenant-memory
  callback. The cascade itself holds no tenant state.

When Track A lands, the integration PR will:
- Replace the `legend_rows` injection path with a real PyMuPDF parser that
  uses Track A's `vector_graph_builder` to build a one-off graph per legend
  row and pass it to `mine_motifs(..., min_frequency=1)`.
- Wire the orchestrator's tenant-memory loader as the `tenant_memory_lookup`
  callable.

No changes to the public types in this wave will be needed.

## Optional acceleration

`MotifIndex` picks the fastest available cosine-NN backend at construction:

1. **faiss-cpu** if importable — `IndexFlatIP` over unit-normalised vectors.
2. **sklearn.NearestNeighbors** if importable — cosine metric.
3. **numpy** fallback — `vectors @ q`, then argsort.

All three produce the same `(motif_id, similarity)` tuples. The backend used
is logged at module load (DEBUG) and exposed via `MotifIndex.backend`.

`faiss-cpu` is NOT in `pyproject.toml` — gated by import. No new hard deps.

## Dep reality check

The brief asserted "networkx, sklearn, numpy, scipy already there." Audit:

- numpy → present (transitive via ultralytics / opencv).
- networkx, sklearn, scipy → **not currently installed** in
  `orchestrator/.venv`.

The implementation handles this correctly:
- networkx is `TYPE_CHECKING`-only; runtime accepts any graph-like duck.
- sklearn / faiss are import-guarded with numpy fallback.
- scipy is unused.

When Track A lands networkx (it needs `vector_graph_builder`), the runtime
graph will be a real `networkx.Graph` and nothing changes here.

## Tests

`pytest` was not executed in this session — the orchestrator's `.venv` is
not callable from the agent's tool surface, and the brief's `bash` channel
is not present. The 4 new test files were authored against the public
interfaces of the 4 modules and trace-validated by hand against:

- `subgraph_miner`: deterministic SHA-1 hash chain; multiset-only inputs;
  rotation orbit hashing; min_frequency filter.
- `symbol_retrieval`: cascade order verified by source-string assertions
  + audit-trail coverage.
- `legend_anchor_v2`: lexicon canonicalisation (verified against the
  pre-existing `_ELECTRICAL` table in symbol_lexicon.py).
- `unknown_motif_resolver`: entropy bands tested against flat/peaked
  embeddings (log2(32) ≈ 5 for flat, 0 for peaked).

The runner must execute:
```
cd orchestrator
.venv\Scripts\pytest.exe tests/test_subgraph_miner.py tests/test_symbol_retrieval.py tests/test_legend_anchor_v2.py tests/test_unknown_motif_resolver.py -v
```

If any test fails, the most likely candidates are:
- **`test_five_identical_triangles_collapse_to_one_motif`** — asserts
  `frequency == 15`. The math: 5 triangles × 3 nodes per triangle, all
  sharing the same induced 3-node neighborhood (within-triangle adjacency
  + 1-hop closure) → same WL hash for all 15 centers. If the WL hash
  diverges between centers within a triangle (rare — would imply the
  triangle isn't symmetric under our initial-label function), this would
  fail. Confidence: high. The triangle is a complete K3, fully symmetric.
- **`test_rotated_motif_matches_via_orbit`** — asserts `g0`'s canonical
  hash lives in `g90`'s orbit. The 12-bucket angle code (30° steps) under
  90° rotation shifts by 3 buckets. Both g0 and g90 generate the same
  4-element orbit set; canonical is the min hex → identical. Confidence:
  high.

## Receipts & Logging (Law #9)

Every module logs at INFO with motif_id (12-hex prefix) and confidence
only. No raw OCR text, no PDF coordinates, no embedding values. Audit
trail in `ResolvedSymbol.retrieval_matches` redacts OCR labels to
`{first_two_chars}*({length})` — preserves the canonical resolution but
strips potentially-PII raw text.
