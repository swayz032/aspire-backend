# Wave 11g-v2 — API Contracts

Every public dataclass / function shape used across Phases 1-9. These are the
SHAPES the acceptance tests assert against. If Track A or Track B drift from
these, the acceptance suite fails first.

## Phase 1 — document_classifier

```python
def classify_page(*, page_bytes: bytes, correlation_id: str) -> Literal["VECTOR_NATIVE", "HYBRID", "RASTER_SCAN"]
```

## Phase 2 — vector_extractor

```python
@dataclass(frozen=True)
class VectorPrimitive:
    kind: Literal["line", "arc", "circle", "polygon", "text"]
    coords: tuple[float, ...]
    layer: str | None = None

def extract_primitives(*, page_bytes: bytes, mode: str, correlation_id: str) -> list[VectorPrimitive]
```

## Phase 3 — graph_builder

```python
@dataclass(frozen=True)
class GraphNode:
    node_id: int
    kind: str
    x: float          # unit-bounded (0..1)
    y: float

@dataclass(frozen=True)
class GraphEdge:
    src: int
    dst: int
    kind: Literal["wire", "wall", "connector"]

def build_graph(primitives: list[VectorPrimitive], *, correlation_id: str) -> tuple[list[GraphNode], list[GraphEdge]]
```

## Phase 4 — wl_motif_miner

```python
@dataclass(frozen=True)
class MotifInstance:
    motif_id: str
    graph_hash: str
    page_id: str
    nodes: tuple[int, ...]
    bbox: tuple[float, float, float, float]

@dataclass(frozen=True)
class DiscoveredMotif:
    motif_id: str
    graph_hash: str
    frequency: int
    rotation_orbit: tuple[str, str, str, str]
    instances: tuple[MotifInstance, ...]

def mine_motifs(
    page_subgraphs: list[tuple[str, list[GraphNode], list[GraphEdge]]],
    *,
    min_frequency: int = 2,
    correlation_id: str,
) -> list[DiscoveredMotif]
```

## Phase 6 — legend_anchor extractor

```python
@dataclass(frozen=True)
class LegendAnchor:
    motif_id: str
    canonical_type: str
    canonical_name: str
    anchor_similarity: float

def extract_legend_anchors(*, page_bytes: bytes, motifs: list[DiscoveredMotif], correlation_id: str) -> dict[str, LegendAnchor]
```

## Phase 7 — retrieval resolver

```python
@dataclass(frozen=True)
class RetrievalMatch:
    source: Literal["legend", "tenant_memory", "ocr", "vlm"]
    score: float
    payload: dict[str, Any]

@dataclass(frozen=True)
class ResolvedSymbol:
    motif_id: str
    graph_hash: str
    canonical_type: str | None
    canonical_name: str | None
    source: Literal["legend", "tenant_memory", "ocr", "unresolved"]
    confidence: float
    bucket: Literal["VECTOR_RECOVERABLE", "RASTER_RECOVERABLE", "TRUE_UNKNOWN"] | None
    retrieval_matches: tuple[RetrievalMatch, ...]

def resolve_motif(
    motif: DiscoveredMotif,
    *,
    legend_anchors: dict[str, LegendAnchor],
    tenant_memory: dict[str, dict],
    ocr_hints: dict[str, tuple[str, float]] | None = None,
    correlation_id: str,
) -> ResolvedSymbol
```

## Phase 8 — symbol_memory upsert

```python
async def upsert_observation(
    *,
    suite_id: str,
    graph_hash: str,
    signature_hash: str,            # legacy; pass = graph_hash[:16] for new writes
    canonical_type: str | None,
    canonical_name: str,
    source: str,
    extraction_mode: str,
    primitive_signature: str | None,
    motif_id: str | None,
    wl_rotation_orbit: list[str] | None,
    confidence: float | None,
    confirmation_count: int = 0,
    project_id: str | None,
    correlation_id: str,
) -> dict | None
```

**Law #2 immutability:** if the existing row has `confirmation_count > 0`, this
function INSERTS a new row instead of UPDATE.

## Phase 9 — training_exporter

```python
@dataclass(frozen=True)
class ExportManifest:
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
    rows: Iterable[dict] | None = None,
    rng_seed: int = 1192,
    correlation_id: str | None = None,
) -> ExportManifest
```

## Phase 10 — metrics

```python
class Wave11g2Metrics:
    def inc(self, name: str, *, value: float = 1.0, labels: dict[str, str] | None = None, correlation_id: str | None = None) -> None
    def observe(self, name: str, *, value: float, labels: dict[str, str] | None = None, correlation_id: str | None = None) -> None
    def snapshot(self) -> dict[str, float]
    def reset_for_tests(self) -> None

def get_metrics() -> Wave11g2Metrics
```

## Correlation ID contract

Every public function across Phases 1-9 MUST accept a `correlation_id: str`
kwarg. Callers propagate the trace ID set at API ingress. The orchestrator
emits the same correlation_id on every receipt, log line, and metric label
where label cardinality permits (it does NOT go into Prometheus labels — see
WAVE_11G2_OBSERVABILITY.md).
