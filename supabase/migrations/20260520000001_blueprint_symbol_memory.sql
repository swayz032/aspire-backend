-- Wave 11i — Tenant Symbol Memory (the data flywheel moat)
--
-- Stores deterministic geometric-signature → name mappings PER TENANT.
-- When the same architect submits a second project, every previously-named
-- symbol auto-resolves because signatures are deterministic SHA1 hashes
-- of the CAD geometry.
--
-- Layered retrieval order in production SEE:
--   1. Lookup (suite_id, signature_hash) in this table.        → instant name
--   2. If miss: lookup signature in the current blueprint's
--      legend region (Wave 11h).                              → architect's name
--   3. If miss: name as "unknown_motif_<sig8>" with provenance
--      vgse_unanchored. Surfaced for contractor confirmation.
--
-- Every contractor confirmation upserts a row here, building tenant taxonomy.

create extension if not exists "vector";

create table public.blueprint_symbol_memory (
  id                       uuid primary key default gen_random_uuid(),
  suite_id                 uuid not null,
  office_id                uuid,
  signature_hash           text not null,        -- VGSE deterministic hash (sha1[:16])
  canonical_name           text not null,        -- human-readable name (architect's preferred)
  category                 text,                 -- electrical | plumbing | architectural | structural | mechanical | annotation | unknown
  aliases                  jsonb not null default '[]'::jsonb,
  source                   text not null,        -- 'legend' | 'confirmation' | 'vlm' | 'manual'
  first_seen_project_id    uuid,
  last_seen_project_id     uuid,
  confirmation_count       integer not null default 0,
  total_instances_observed integer not null default 0,
  embedding                vector(384),          -- optional DINOv2-small features for fuzzy retrieval
  model_version            text,                 -- which model produced the embedding
  confidence               numeric,              -- 0-1 — confidence in canonical_name
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now(),
  created_by               uuid,

  -- Composite uniqueness — one canonical row per (suite, signature)
  constraint blueprint_symbol_memory_suite_sig_unique
    unique (suite_id, signature_hash)
);

-- Indexes
create index idx_blueprint_symbol_memory_suite on public.blueprint_symbol_memory (suite_id);
create index idx_blueprint_symbol_memory_sig on public.blueprint_symbol_memory (signature_hash);
create index idx_blueprint_symbol_memory_suite_sig on public.blueprint_symbol_memory (suite_id, signature_hash);
create index idx_blueprint_symbol_memory_category on public.blueprint_symbol_memory (category);

-- pgvector HNSW index for fuzzy retrieval (when exact hash misses but a
-- visually-similar symbol exists in memory — e.g., same outlet drawn at a
-- slightly different scale produces a different hash but similar embedding).
-- Cosine distance because DINOv2 embeddings are unit-normalized.
create index idx_blueprint_symbol_memory_embedding
  on public.blueprint_symbol_memory
  using hnsw (embedding vector_cosine_ops)
  where embedding is not null;

-- RLS — strict tenant isolation (Law #6)
alter table public.blueprint_symbol_memory enable row level security;

create policy blueprint_symbol_memory_tenant_isolation
  on public.blueprint_symbol_memory
  for all
  using (suite_id = current_setting('app.current_suite_id')::uuid);

-- moddatetime trigger to keep updated_at fresh
create extension if not exists moddatetime;
create trigger blueprint_symbol_memory_set_updated_at
  before update on public.blueprint_symbol_memory
  for each row execute procedure moddatetime(updated_at);

comment on table public.blueprint_symbol_memory is
  'Wave 11i — per-tenant deterministic symbol name memory. Every (suite_id, '
  'signature_hash) pair maps to the architect''s canonical name. When the same '
  'CAD signature appears in any subsequent blueprint upload, it auto-resolves. '
  'This is the data flywheel that competitors cannot replicate without rebuilding '
  'the entire Aspire trust spine + tenant model.';
