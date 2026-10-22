-- EventAutopilot schema. Idempotent: safe to run more than once.
-- Memory layer design: docs/KNOWLEDGE-BASE.md
create extension if not exists pg_trgm;
-- pgvector is intentionally omitted tonight (FTS + trigram only, see KNOWLEDGE-BASE.md section 4).
-- To enable later:  create extension if not exists vector;
--                   alter table entities add column if not exists embedding vector(384);

create table if not exists events (
  id             uuid primary key default gen_random_uuid(),
  title          text default '',
  notion_page_id text,
  spec           jsonb default '{}'::jsonb,
  status         text default 'draft',
  created_at     timestamptz default now()
);

create table if not exists entities (
  id          uuid primary key default gen_random_uuid(),
  kind        text not null,   -- venue | supplier | sponsor | volunteer | community | event
  name        text not null,
  attrs       jsonb default '{}'::jsonb,
  times_used  int default 0,
  created_at  timestamptz default now(),
  unique (kind, name)
);

create table if not exists relations (
  id          uuid primary key default gen_random_uuid(),
  source_id   uuid references entities(id) on delete cascade,
  relation    text not null,   -- hosted | organised | catered | sponsored | ran_shift | supplied
  target_id   uuid references entities(id) on delete cascade,
  event_id    uuid references events(id) on delete set null,
  confidence  real default 1.0,
  created_at  timestamptz default now()
);

create table if not exists memories (
  id          uuid primary key default gen_random_uuid(),
  kind        text not null,   -- budget | venue | supplier | voice | volunteer | lesson
  text        text not null,
  tags        text[] default '{}',
  provenance  text default '', -- human-readable name of the event that taught us this
  event_id    uuid references events(id) on delete set null,
  entity_id   uuid references entities(id) on delete set null,
  search      tsvector generated always as (to_tsvector('english', coalesce(text, ''))) stored,
  created_at  timestamptz default now()
);

create table if not exists edits (
  id           uuid primary key default gen_random_uuid(),
  artifact_id  text not null,
  before_text  text default '',
  after_text   text default '',
  notes        text default '',
  created_at   timestamptz default now()
);

create index if not exists memories_search_idx on memories using gin (search);
create index if not exists memories_kind_idx on memories (kind);
create index if not exists entities_name_trgm_idx on entities using gin (name gin_trgm_ops);
create index if not exists relations_source_idx on relations (source_id);
create index if not exists relations_target_idx on relations (target_id);

-- RLS: public read on the knowledge graph (graph.html uses the anon key).
-- No write policies: writes only work with the service key, which bypasses RLS.
alter table events    enable row level security;
alter table edits     enable row level security;
alter table entities  enable row level security;
alter table relations enable row level security;
alter table memories  enable row level security;

drop policy if exists "public read entities"  on entities;
drop policy if exists "public read relations" on relations;
drop policy if exists "public read memories"  on memories;
create policy "public read entities"  on entities  for select using (true);
create policy "public read relations" on relations for select using (true);
create policy "public read memories"  on memories  for select using (true);
