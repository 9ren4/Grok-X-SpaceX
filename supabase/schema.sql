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
  source_id      text,           -- the pipeline's own event id (slug); id is uuid5(URL, source_id)
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

-- Which events touched which entity: lets times_used count each (event, entity) pair once.
create table if not exists event_entities (
  event_id   uuid not null references events(id) on delete cascade,
  entity_id  uuid not null references entities(id) on delete cascade,
  created_at timestamptz default now(),
  primary key (event_id, entity_id)
);

-- Migrations for databases created from an earlier version of this file (all idempotent).
alter table events   add column if not exists source_id text;
alter table memories add column if not exists text_hash text generated always as (md5(text)) stored;

-- Dedupe any pre-existing duplicates so the unique indexes below can be created.
delete from relations a using relations b
  where a.ctid > b.ctid and a.source_id = b.source_id and a.relation = b.relation
    and a.target_id = b.target_id;
delete from memories a using memories b
  where a.ctid > b.ctid and a.text_hash = b.text_hash;

create unique index if not exists relations_triple_uidx on relations (source_id, relation, target_id);
create unique index if not exists memories_text_hash_uidx on memories (text_hash);
create index if not exists events_source_id_idx on events (source_id);
create index if not exists memories_search_idx on memories using gin (search);
create index if not exists memories_kind_idx on memories (kind);
create index if not exists entities_name_trgm_idx on entities using gin (name gin_trgm_ops);
create index if not exists relations_source_idx on relations (source_id);
create index if not exists relations_target_idx on relations (target_id);

-- Atomic entity upsert (called by memory.py via POST /rpc/upsert_entity with the service key).
-- Inserts, or merges attrs on (kind, name); times_used goes up by one only when this event has
-- not touched the entity before, so re-running the same event never inflates counts.
create or replace function upsert_entity(
  p_kind text, p_name text, p_attrs jsonb default '{}'::jsonb,
  p_event uuid default null, p_used int default 1
) returns uuid language plpgsql as $$
declare
  v_id uuid;
  v_inserted boolean;
  v_linked int;
begin
  insert into entities as e (kind, name, attrs, times_used)
  values (p_kind, p_name, coalesce(p_attrs, '{}'::jsonb), greatest(coalesce(p_used, 1), 0))
  on conflict (kind, name) do update
    set attrs = coalesce(e.attrs, '{}'::jsonb) || excluded.attrs
  returning e.id, (e.xmax = 0) into v_id, v_inserted;

  if p_event is not null then
    insert into event_entities (event_id, entity_id) values (p_event, v_id)
      on conflict do nothing;
    get diagnostics v_linked = row_count;
    if v_linked > 0 and not v_inserted then
      update entities set times_used = coalesce(times_used, 0) + 1 where id = v_id;
    end if;
  end if;
  return v_id;
end $$;
alter function upsert_entity(text, text, jsonb, uuid, int) set search_path = public;
revoke execute on function upsert_entity(text, text, jsonb, uuid, int) from public, anon, authenticated;
grant  execute on function upsert_entity(text, text, jsonb, uuid, int) to service_role;

-- RLS. Public (anon key) can SELECT only entities and relations: the graph page reads those two
-- tables and nothing else. memories, events, edits, event_entities are service-key only.
-- No write policies anywhere: writes need the service key, which bypasses RLS.
alter table events         enable row level security;
alter table edits          enable row level security;
alter table entities       enable row level security;
alter table relations      enable row level security;
alter table memories       enable row level security;
alter table event_entities enable row level security;

drop policy if exists "public read entities"  on entities;
drop policy if exists "public read relations" on relations;
drop policy if exists "public read memories"  on memories;  -- removed on purpose: memories are service-only
create policy "public read entities"  on entities  for select using (true);
create policy "public read relations" on relations for select using (true);

revoke all on events, edits, memories, event_entities from anon;
grant select on entities, relations to anon;
