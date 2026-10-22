# Memory Layer: Supabase instead of Cognee

> **Status:** decided, 22 Oct ~19:40. Supersedes §4 ("Cognee: what goes into memory") of
> [ARCHITECTURE.md](./ARCHITECTURE.md). Everything else in that doc still stands.
> **If you are about to write code against Cognee — stop and read this first.**

---

## 1. What changed, and what didn't

**Dropped:** Cognee as the memory layer.

**Why:** two hard risks with a 21:50 freeze. The install is heavy, and `cognify()` is slow
enough that it sits directly in the demo path — if it stalls on stage, the differentiator
is what breaks. Neither risk buys us anything a database doesn't already give us.

**Kept — unchanged:** the product idea. Every event run through the platform writes what it
learned into a **shared knowledge base**, so the next organiser inherits it. The more events
go through, the less anyone starts from zero. That is a claim about **data shape and reuse**,
not about which vendor stores it.

**Replaced with:** Supabase Postgres, which we were already running for events and artifacts.
The knowledge base is now three tables that form a real graph — nodes, edges, and a text
corpus for recall.

### The pitch does not change

"Shared knowledge graph that gets smarter with every event" stays true and is now *more*
defensible, because we can show the rows and the SQL behind any claim we make on stage.

---

## 2. Schema

Three tables carry the knowledge base. (`events`, `artifacts` and `edits` are operational
and listed in §6 of ARCHITECTURE.md.)

```sql
-- NODES: venues, suppliers, caterers, sponsors, volunteers, communities
create table entities (
  id          uuid primary key default gen_random_uuid(),
  kind        text not null,        -- venue | supplier | sponsor | volunteer | community
  name        text not null,
  attrs       jsonb default '{}',   -- capacity, price_per_head, contact, quirks, …
  times_used  int  default 0,       -- drives node size in the graph view
  embedding   vector(384),          -- NULLABLE, unused tonight (see §4)
  created_at  timestamptz default now(),
  unique (kind, name)
);

-- EDGES: the graph
create table relations (
  id          uuid primary key default gen_random_uuid(),
  source_id   uuid references entities(id) on delete cascade,
  relation    text not null,        -- hosted | catered | sponsored | ran_shift | supplied
  target_id   uuid references entities(id) on delete cascade,
  event_id    uuid references events(id) on delete set null,
  confidence  real default 1.0,
  created_at  timestamptz default now()
);

-- RECALL CORPUS: free-text facts, with provenance back to the event they came from
create table memories (
  id          uuid primary key default gen_random_uuid(),
  kind        text not null,        -- budget | venue | supplier | voice | volunteer | lesson
  text        text not null,        -- "60 people ate 15 pizzas, £142 from Pizza Pilgrims"
  tags        text[] default '{}',
  event_id    uuid references events(id) on delete set null,
  entity_id   uuid references entities(id) on delete set null,
  search      tsvector generated always as (to_tsvector('english', text)) stored,
  created_at  timestamptz default now()
);

create index on memories using gin (search);
create index on entities using gin (name gin_trgm_ops);
```

An event node is an `entities` row with `kind = 'event'`, so a single `relations` table
covers both "this venue hosted that event" and "this sponsor backed that venue".

---

## 3. The interface you code against

Only `memory.py` touches these tables. Everything else — extractor, planner, promo writer —
goes through two methods. **Do not query the knowledge tables directly from a generator**;
if you need a new access pattern, add it here so we both get it.

```python
# eventautopilot/memory.py

def recall(spec: EventSpec, kinds: list[str] | None = None, limit: int = 12) -> list[Memory]:
    """Facts relevant to this event, ranked. Goes into every Grok prompt
    under '## Community memory'. Each Memory carries .text and .provenance."""

def remember(event_id: str, facts: list[Memory], entities: list[Entity],
             relations: list[Relation]) -> None:
    """Write back everything this run learned. Called once at the end of a pipeline run.
    Upserts entities by (kind, name) and bumps times_used."""

def learn_from_edit(artifact_id: str, before: str, after: str, notes: str = "") -> None:
    """Organiser edited our copy — store the diff as a 'voice' memory."""

def graph(limit: int = 150) -> dict:
    """Nodes + edges as JSON for the graph view (§5)."""
```

Prompt injection stays exactly as planned in ARCHITECTURE.md §4 — retrieved context goes
into every Grok prompt under a `## Community memory` heading. Only the retrieval
implementation behind it changed.

---

## 4. Retrieval: full-text + trigram, not vectors

Recall ranks `memories` with Postgres full-text search (`search @@ websearch_to_tsquery`)
blended with trigram similarity on entity names, filtered by `kind` and city.

**Why no pgvector tonight:** embeddings need a provider, and xAI has no public embeddings
endpoint — pgvector would mean onboarding a second LLM vendor at hour 20 for a corpus of
roughly 20–50 seeded facts, which FTS retrieves just as well at that size.

The `entities.embedding` column already exists and is nullable, so switching on pgvector is
a backfill plus one query change, not a rewrite. **This is the honest answer if a judge asks
"why not vector search?" — the upgrade path is deliberate, not an oversight.**

---

## 5. Graph view

`memory.graph()` writes `out/graph.json`; a single static HTML page renders it with
Cytoscape from a CDN. No Puppeteer, no canvas library — nothing from the project's
do-not-use list.

- **Colour** by `entities.kind`, with a legend
- **Node size** by `times_used`, so reused venues visibly dominate — the thesis, made visual
- **Edge labels** from `relations.relation`

**Demo moment:** render the graph, run a second event, re-render. The new event's nodes latch
onto the venue and caterer the *first* organiser discovered, highlighted. That before/after
is the clearest statement of "no organiser starts from zero" we have.

**Ground rule:** the page renders real rows only. Seeding the knowledge base with plausible
past events is normal and we are doing it — drawing nodes with no row behind them is not,
because it collapses the moment a judge clicks into it.

---

## 6. Build order (~2h to freeze)

| # | Step | Notes |
|---|------|-------|
| 1 | Supabase project + schema + seeded knowledge base | via MCP, needs no keys from us |
| 2 | Luma fetch → `recall()` → Grok copy → run sheet / budget / rota | the MVP spine |
| 3 | Notion push | run sheet + budget + rota |
| 4 | Graph page | 20 min once data exists |
| 5 | Ghost newsletter + LinkedIn / WhatsApp copy | first to be cut if we are behind |

If we are behind at 21:00, Ghost and the channel copy get cut — **not** the graph, since it
carries the differentiator.

## 7. Luma API — verified working, no key

Checked against a live public event, both return JSON unauthenticated:

- `GET api.lu.ma/url?url=<slug>` — resolves a `lu.ma/abc123` link to its event
- `GET api.lu.ma/event/get?event_api_id=evt-…` — full event
- `GET api.lu.ma/discover/get-paginated-events` — real public events, useful for seed data

Fields we map into `EventSpec`: `name`, `start_at`, `end_at`, `timezone`,
`geo_address_info` (city / country; often `mode: obfuscated`, so no street address),
`hosts`, `ticket_types` (prices in **cents**), `guest_count`, `capacity`.

**Watch out:** the description is `description_mirror`, a rich-text JSON doc — not a plain
string. It needs flattening before it goes near a prompt.

## 8. Open questions

- **Cognee sponsor prize** — if Cognee is on the sponsor list, dropping it may cost a track.
  A shim behind `recall()` / `remember()` is ~15 lines if we decide it is worth it. Currently
  a clean break.
- **Supabase project** — creating a fresh one via MCP. Say so now if you already made one.
