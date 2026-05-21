# Wave 11g-v2 — WL Motif Pipeline (README)

## What this is

Wave 11g-v2 replaces the legacy `signature_hash = SHA1(sequence_of_primitives)`
identity for blueprint symbols with a **Weisfeiler-Lehman (WL) graph hash** over
the symbol's local topology. The WL hash is stable under:

- Rotation (0/90/180/270) — `wl_rotation_orbit` carries the 4 orbit members.
- Scale — vector coords are normalised before extraction.
- Translation — node coords don't enter the WL hash.
- **Context noise** — wires/walls outside the motif boundary don't perturb it.

That last property is the breakthrough: two outlets surrounded by completely
different wire/wall geometry now cluster into one motif. The legacy SHA1-of-
sequence hash could not do that.

## Three tracks

| Track | Scope | This worktree |
| --- | --- | --- |
| A | Vector graph builder (classify → extract → build graph) | consumes |
| B | WL motif miner + resolver | consumes |
| C | **Persistence, observability, training pipeline, docs** | **THIS** |

## 12-phase plan summary

| Phase | Description | Owner | Status (Track C) |
| --- | --- | --- | --- |
| 1 | document_classifier (VECTOR_NATIVE / HYBRID / RASTER_SCAN) | Track A | consumed |
| 2 | vector_extractor (VectorPrimitive list) | Track A | consumed |
| 3 | graph_builder (nodes + edges, unit-bounded coords) | Track A | consumed |
| 4 | wl_motif_miner (DiscoveredMotif list) | Track B | consumed |
| **5** | **Schema migration (additive)** | **Track C** | **DONE** |
| 6 | legend_anchor extractor | Track B | consumed |
| 7 | retrieval resolver (legend → memory → ocr → unresolved) | Track B | consumed |
| 8 | symbol memory upsert (with new columns) | Track B | consumed |
| **9** | **YOLO training exporter** | **Track C** | **DONE** |
| **10** | **Observability metrics + correlation IDs** | **Track C** | **DONE** |
| **11** | **Acceptance contract tests** | **Track C** | **DONE** |
| **12** | **Documentation + rollout/rollback** | **Track C** | **DONE** |

## Decision matrix

| Input | Pipeline mode | Confidence floor | Persisted? |
| --- | --- | --- | --- |
| Vector PDF, legend anchor matches | VECTOR_NATIVE → legend | 0.85 | yes, source=`legend` |
| Vector PDF, prior tenant confirmation | VECTOR_NATIVE → memory | 0.85 | yes, source=`tenant_memory` |
| Vector PDF, novel topology | VECTOR_NATIVE → unresolved | 0.0 | yes, source=`unresolved`, bucket=TRUE_UNKNOWN |
| Scan PDF, OCR match | RASTER_SCAN → ocr | 0.85 | yes, source=`ocr` |
| Scan PDF, no match | RASTER_SCAN → unresolved | 0.0 | yes, source=`unresolved`, bucket=RASTER_RECOVERABLE |
| Hybrid page | HYBRID | per-stream | per-stream |

**Law #3 (fail closed):** below 0.85 confidence, the resolver returns
`source='unresolved'`. There is no fallback that hallucinates a class.

## File map (Track C deliverables)

```
orchestrator/
├── migrations/
│   └── 20260521000002_blueprint_symbol_memory_wave_11g2.sql   # Phase 5
├── src/aspire_orchestrator/services/blueprint/
│   ├── training_exporter.py                                   # Phase 9
│   └── wave_11g2_metrics.py                                   # Phase 10
└── tests/
    └── test_wave_11g2_acceptance.py                           # Phase 11

WAVE_11G2_README.md            # this file
WAVE_11G2_EXECUTION_FLOW.md
WAVE_11G2_API_CONTRACTS.md
WAVE_11G2_OBSERVABILITY.md
WAVE_11G2_ROLLOUT.md
WAVE_11G2_ROLLBACK.md
```

## How to run the acceptance suite

```pwsh
cd backend/.claude/worktrees/wave-11g2-persistence/orchestrator
pytest tests/test_wave_11g2_acceptance.py -v
```

The acceptance file re-declares every Track A/B interface inline — it runs
even on a branch where Tracks A/B have not landed.
