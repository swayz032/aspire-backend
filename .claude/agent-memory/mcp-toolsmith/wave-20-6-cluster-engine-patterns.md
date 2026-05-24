---
name: wave-20-6-cluster-engine-patterns
description: Wave 20.6 Stage 5 spatial repetition clustering — DB proxy pattern, test vector pitfalls, propagation writes
metadata:
  type: project
---

## DB Proxy Pattern for Patchable Deferred Imports

`supabase_select` etc. are deferred imports inside async functions (mirrors wave_11g2_see.py). This means `patch("module.supabase_select")` fails with AttributeError because the name only exists during function execution.

**Solution**: Add module-level proxy functions `_db_select`, `_db_update`, `_db_insert_batch` that call the deferred imports. Tests patch these proxies via `patch.object(sce, "_db_select", ...)`.

```python
async def _db_select(table, filters, **kwargs):
    from aspire_orchestrator.services.supabase_client import supabase_select
    return await supabase_select(table, filters, **kwargs)
```

**Why:** `patch.object` on the module works because the proxy name IS a module attribute. Direct deferred import name is NOT.

## Test Vector Pitfall

`_make_vec(i * pi/5)` for i=0..4 does NOT produce orthogonal vectors. `cos(0)=1.0`, `cos(pi/5)≈0.809` — overlapping in the same half-plane, similarity > 0.92 for nearby angles.

**Solution**: Use standard basis vectors `e_0..e_{n-1}` (identity matrix rows). Cosine similarity between any two = 0.0. Helper: `_orthonormal_basis(n, dim)`.

## blueprint_symbols.embedding Bytea Encoding

Supabase returns bytea columns as hex string `\\x<hex>` in REST responses. Must strip `\\x` prefix before `bytes.fromhex()`. `_embedding_from_bytes` handles both raw bytes and string forms.

## propagate_legend_bindings_to_clusters Writes ALL Clusters

The function writes `blueprint_resolved_symbols` rows for ALL clusters (auto-bound + flagged + unknown), not just bound ones. The `resolution_source` column distinguishes: `cluster_propagation` | `pending_vlm_verify` | `unknown`. This gives Stage 6 a complete work queue.

## cluster_id Stability

SHA1 of sorted member IDs → `cls_<16-hex>`. Sorting ensures the same cluster gets the same ID regardless of insertion order. Prefix `cls_` prevents confusion with UUID format.

## Single-Linkage vs HDBSCAN

Used single-linkage union-find (no external deps). scipy hierarchical used as optional refinement when rows > 500. The `_scipy_linkage_inplace` function is side-effect on the parent array, not return-value, because union-find state must be shared.
