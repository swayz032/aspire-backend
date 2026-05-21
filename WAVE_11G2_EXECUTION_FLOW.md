# Wave 11g-v2 — Execution Flow

## End-to-end pipeline

```mermaid
flowchart TD
    A[PDF upload] --> B[document_classifier]
    B -->|VECTOR_NATIVE| C[vector_extractor]
    B -->|HYBRID| C
    B -->|RASTER_SCAN| R[raster_pipeline]
    C --> D[graph_builder]
    D --> E[wl_motif_miner]
    E --> F[legend_anchor_extractor]
    F --> G[retrieval_resolver]
    G -->|legend hit| H[symbol_memory upsert]
    G -->|tenant_memory hit| H
    G -->|ocr hit| H
    G -->|no match| U[TRUE_UNKNOWN bucket]
    U --> H
    R --> G
    H --> I[blueprint_symbol_memory table]
    I --> J[training_exporter]
    J --> K[YOLO dataset]
```

## Per-page state machine

```mermaid
stateDiagram-v2
    [*] --> Classified
    Classified --> VectorExtracted: VECTOR_NATIVE / HYBRID
    Classified --> RasterScanned: RASTER_SCAN
    VectorExtracted --> GraphBuilt
    GraphBuilt --> MotifsMined
    MotifsMined --> Resolved
    RasterScanned --> Resolved
    Resolved --> Persisted
    Persisted --> [*]
```

## Resolver layered retrieval (Law #3 fail closed)

```mermaid
flowchart LR
    M[Motif] --> L{legend anchor >= 0.85?}
    L -->|yes| R1[source=legend]
    L -->|no| T{tenant_memory hash hit >= 0.85?}
    T -->|yes| R2[source=tenant_memory]
    T -->|no| O{ocr hint >= 0.85?}
    O -->|yes| R3[source=ocr]
    O -->|no| R4[source=unresolved, bucket=TRUE_UNKNOWN]
```

## Persistence write path

```mermaid
sequenceDiagram
    participant R as resolver
    participant SM as symbol_memory service
    participant DB as blueprint_symbol_memory
    R->>SM: upsert(graph_hash, canonical_type, source, confidence)
    SM->>DB: SELECT existing row by (suite_id, signature_hash)
    alt Exists, confirmation_count > 0
        SM->>DB: INSERT new superseding row (Law #2)
    else New or unconfirmed
        SM->>DB: UPSERT (suite_id, signature_hash)
    end
    DB-->>SM: row
    SM-->>R: receipt id
```

## YOLO export pipeline

```mermaid
flowchart TD
    A[export_yolo_dataset call] --> B[load rows from blueprint_symbol_memory]
    B --> C{source in {legend, confirmation, tenant_memory}?}
    C -->|no| X1[exclude: wrong_source]
    C -->|yes| D{confidence >= 0.85?}
    D -->|no| X2[exclude: low_confidence]
    D -->|yes| E{source=legend OR confirmations >= 1?}
    E -->|no| X3[exclude: insufficient_confirmations]
    E -->|yes| F{graph_hash already seen?}
    F -->|yes| X4[exclude: duplicate_graph_hash]
    F -->|no| G[shuffle + split 70/20/10]
    G --> H[write images/, labels/, data.yaml, manifest.json]
```
