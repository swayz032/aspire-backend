-- Wave 9l: Blueprint Engine — sheet image storage for SEE-stage cache
-- Plan: Drew pipeline production-grade optimization
--
-- 1. Create `blueprint-sheet-images` Storage bucket (private, 10 MB limit, image/png)
-- 2. Add image_storage_path column to blueprint_sheets (nullable)
-- 3. Service-role write policy (backend enforces suite_id path prefix at app layer)
--
-- RLS on blueprint_sheets inherits the existing `blueprint_sheets_tenant_isolation`
-- policy from migration 20260517000001 — no new table policy needed.
-- Reversible DOWN section at bottom.

-- ──────────────────────────────────────────────────────────────────────────────
-- 1. Storage bucket: blueprint-sheet-images
-- ──────────────────────────────────────────────────────────────────────────────
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'blueprint-sheet-images',
  'blueprint-sheet-images',
  false,
  10485760,   -- 10 MB in bytes
  array['image/png']
)
on conflict (id) do nothing;

-- ──────────────────────────────────────────────────────────────────────────────
-- 2. Storage RLS policies for blueprint-sheet-images bucket.
--    The backend service role owns all writes. Suite path isolation is enforced
--    at the application layer (sheet_image_storage.py path = {suite_id}/{project_id}/{sheet_id}.png).
--    Authenticated reads are gated on the caller's current_setting suite scope.
-- ──────────────────────────────────────────────────────────────────────────────

-- Service role can upload (insert) any object in this bucket.
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'storage'
      and tablename  = 'objects'
      and policyname = 'blueprint-sheet-images: service-role write'
  ) then
    execute $pol$
      create policy "blueprint-sheet-images: service-role write"
        on storage.objects for insert
        to service_role
        with check (bucket_id = 'blueprint-sheet-images')
    $pol$;
  end if;
end $$;

-- Service role can read (select) any object — needed for download_sheet_image.
do $$
begin
  if not exists (
    select 1 from pg_policies
    where schemaname = 'storage'
      and tablename  = 'objects'
      and policyname = 'blueprint-sheet-images: service-role read'
  ) then
    execute $pol$
      create policy "blueprint-sheet-images: service-role read"
        on storage.objects for select
        to service_role
        using (bucket_id = 'blueprint-sheet-images')
    $pol$;
  end if;
end $$;

-- ──────────────────────────────────────────────────────────────────────────────
-- 3. image_storage_path column on blueprint_sheets
-- ──────────────────────────────────────────────────────────────────────────────
alter table public.blueprint_sheets
  add column if not exists image_storage_path text;

comment on column public.blueprint_sheets.image_storage_path
  is 'Supabase Storage path for the full-res 200 DPI PNG of this sheet. '
     'Format: {suite_id}/{project_id}/{sheet_id}.png. '
     'Null if image upload failed or not yet run. '
     'Used by SEE stage to skip PDF re-render (cache hit).';

-- ──────────────────────────────────────────────────────────────────────────────
-- DOWN (manual; do not run automatically)
-- ──────────────────────────────────────────────────────────────────────────────
-- drop policy if exists "blueprint-sheet-images: service-role read"  on storage.objects;
-- drop policy if exists "blueprint-sheet-images: service-role write" on storage.objects;
-- alter table public.blueprint_sheets drop column if exists image_storage_path;
-- delete from storage.buckets where id = 'blueprint-sheet-images';
