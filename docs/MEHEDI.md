<div align="center">

# 🧠 Mehedi — The Brain

### Data in. Plan and copy out.

You build everything between **"a Luma link"** and **"a finished `RunResult`"**.
Aryan turns that `RunResult` into what the judges see.

`Code freeze 21:50` &nbsp;·&nbsp; `Branch: mehedi/*` &nbsp;·&nbsp; back to [PROJECT.md](./PROJECT.md)

</div>

---

## 🎯 Your one-line mission

> Given `https://lu.ma/<slug>`, return a `RunResult`: a planned event, grounded in what past events taught us, with copy written in the community's voice. Write what you learned back to the knowledge base.

```mermaid
flowchart LR
    A[Luma link] --> B[WP1 luma.py]
    B --> C[WP2 memory.recall]
    C --> D[WP3 planner]
    D --> E[WP4 copy]
    E --> F[memory.remember]
    F --> G{{RunResult}}
    G -.->|handed to| H[Aryan's outputs]
```

---

## 📦 Files you own

```
eventautopilot/
├── models.py          ← already drafted; you are its maintainer
├── config.py          ← already drafted
├── luma.py            WP1
├── memory.py          WP2  (Supabase knowledge base)
├── planner.py         WP3  (run sheet · budget · rota)
├── llm.py             WP4  (Grok switch + fallbacks)
├── copy.py            WP4  (page · newsletter · LinkedIn · WhatsApp)
└── pipeline.py        WP5  (orchestration + CLI)
seed/
├── sample_run.json    ← THE CONTRACT FIXTURE (do this first)
└── knowledge.sql      WP2  (seeded past events)
```

**Don't touch:** `notion_sync.py`, `visuals/`, `ghost.py`, `graph/`. Those are Aryan's.

---

## 🚦 Step 0 — unblock Aryan (do this before anything else, ~10 min)

Aryan can't build against nothing. Push these first:

- [ ] Confirm `eventautopilot/models.py` and `config.py` are on `main` (they were committed in `2dc7b11`)
- [ ] Write **`seed/sample_run.json`**: one complete, realistic `RunResult` (title, date, venue, a ~10-slot run sheet, ~6 budget lines with `source` fields, ~6 rota shifts, all five copy artifacts, 3 `memories_used`)
- [ ] Push to `main` and tell Aryan

> This is **Checkpoint C0**. Everything Aryan does depends on it.

---

## 🧩 Work packages

Each package is sized for **one coding agent**. Hand it the package text, the files it owns, and the definition of done.

---

### WP1 · `luma.py` — fetch and normalise &nbsp; ⏱️ ~20 min

**Goal:** turn any public Luma link into an `EventSpec`.

**Verified endpoints (no key):**

| Call | Use |
|---|---|
| `GET api.lu.ma/url?url=<slug>` | resolve `lu.ma/abc123` → the event (`data.api_id`) |
| `GET api.lu.ma/event/get?event_api_id=evt-…` | full event |
| `GET api.lu.ma/discover/get-paginated-events?pagination_limit=N` | real public events, for testing and seeds |

**Field mapping**

| Luma | → `EventSpec` |
|---|---|
| `event.name` | `title` |
| `event.start_at` / `end_at` (UTC) | `date`, `start`, `end` — **convert using `event.timezone`** |
| `event.geo_address_info` | `venue.city`; address is often `mode: obfuscated`, so leave blank rather than invent |
| `hosts[]` | `hosts` |
| `ticket_types[].cents` | `ticket_price` — **cents, divide by 100** |
| `guest_count`, `ticket_types[].max_capacity` | `expected_attendees` (fall back to a sensible default and add `"attendees"` to `missing`) |
| `description_mirror` | `description` — **rich-text JSON, not a string. Flatten it** (walk `content[].content[].text`) |

**Edge cases**
- Link may be `lu.ma/x`, `luma.com/x` or a full URL with a query string
- 404 `"could not find what you were looking for"` → raise a clear error
- Anything you can't determine goes in `spec.missing`, never invented

**Definition of done**
- [ ] `fetch_event("https://lu.ma/pxzsiu78")` returns a valid `EventSpec`
- [ ] Works on 3 different live events from the discover feed
- [ ] Response cached to `out/luma_<id>.json` so reruns and demos don't hit the network

---

### WP2 · `memory.py` + Supabase schema — the knowledge base &nbsp; ⏱️ ~35 min

**Goal:** the shared memory that makes the product different. Design: [KNOWLEDGE-BASE.md](./KNOWLEDGE-BASE.md).

**Part A — schema (via the Supabase MCP, no keys needed)**
- [ ] Create the Supabase project (or confirm Aryan hasn't already — Q3)
- [ ] Apply the migration: `events`, `entities`, `relations`, `memories`, `edits`
- [ ] Enable `pg_trgm`; add GIN indexes on `memories.search` and `entities.name`
- [ ] Add a public-read policy on `entities`/`relations`/`memories` so `graph.html` can read with the anon key (writes stay service-key only)

**Part B — seed data** (`seed/knowledge.sql`)
Plausible past events, so recall has something to find. Roughly:
- 4–5 past events across 2–3 communities
- 6–8 venues, 5–6 suppliers, 3–4 sponsors, 8–10 volunteers
- ~30 `memories`: costs per head, what ran over time, venue quirks, voice notes
- ~40 `relations`

> ⚠️ Seeded data is fine and normal for a demo. **Mark it:** put `seed: true` in `attrs`. We never claim seeded rows are real events.

**Part C — the four functions** (signatures are the contract)

```python
def recall(spec, kinds=None, limit=12) -> list[Memory]
def remember(event_id, facts, entities, relations) -> None   # upsert entities by (kind,name), bump times_used
def learn_from_edit(artifact_id, before, after, notes="") -> None
def graph(limit=150) -> dict          # {"nodes":[…], "edges":[…]} → also writes out/graph.json
```

**Recall ranking:** `websearch_to_tsquery` over `memories.search`, boosted by trigram similarity on entity names, filtered by `kind` and city. Return `Memory.provenance` = the event that taught us the fact.

**Fallback:** with no Supabase keys, read/write `out/memory.json`. Same functions, same shapes. The log line must say `memory: local fallback`.

**Definition of done**
- [ ] `recall()` for a London meetup returns ≥ 5 relevant seeded facts, each with provenance
- [ ] `remember()` twice with the same venue → one entity, `times_used = 2`
- [ ] `graph()` returns nodes with `kind` and `times_used`, edges with `relation`
- [ ] Local fallback passes the same three checks

---

### WP3 · `planner.py` — run sheet, budget, rota &nbsp; ⏱️ ~30 min

**Goal:** turn an `EventSpec` + recalled `Memory` into a run sheet, a budget and a rota.

**Deterministic first, Grok second.** Build a template per `format` so the planner works with zero keys; let Grok adjust it when available.

| Output | Rule |
|---|---|
| **Run sheet** | Template per format (meetup: doors → welcome → talk → break → talk → networking → close). Anchor to `spec.start`, honour `spec.end`, ≥ 8 slots |
| **Budget** | Line items × `expected_attendees`. **Prices come from recalled memories first**; if none, a labelled default. Fill `LineItem.source` |
| **Rota** | Roles (setup, check-in ×2, floor, photo, teardown) matched to volunteer entities from memory; **max 2 shifts per person** |

**The trust rule:** every budget line either has `source="memory: <event>"` or `source="default estimate"`. Never blank. This is what the judges can click into.

**Definition of done**
- [ ] A planned event has run sheet + budget + rota, all non-empty, no overlapping slots
- [ ] ≥ 1 budget line cites a memory when memory exists
- [ ] With empty memory it still returns a plan, with every line marked `default estimate`
- [ ] `budget_total` and `per_head` are correct

---

### WP4 · `llm.py` + `copy.py` — Grok and the copy &nbsp; ⏱️ ~30 min

**Goal:** generate the five copy artifacts in the community's voice.

**`llm.py` — one switch, three tiers**

```
1. Grok Bot, if Q1 gives us a callable interface
2. xAI Grok API (OpenAI-compatible, base_url https://api.x.ai/v1, GROK_API_KEY)
3. Deterministic templates (no key → still returns usable copy)
```

> ❓ **Q1 is open:** research showed Grok Bot is an agent product; we found no public API for it. Ask the partner on the night. Build tier 2 and tier 3 now; add tier 1 behind the same function if it turns out to exist.

**`copy.py` outputs**

| Artifact | Spec |
|---|---|
| `page_copy` | 120–180 words, Markdown, lead with the hook, include date/venue/price |
| `newsletter_subject` + `newsletter_html` | Subject < 60 chars. HTML body that renders in Ghost (simple `<p>`, `<h2>`, `<ul>`, one CTA link to `luma_url`) |
| `linkedin` | ≤ 1,300 chars, no hashtag spam (max 3) |
| `whatsapp` | ≤ 600 chars, casual, emoji ok, ends with the link |

**Prompt shape**

```
## Event            (the EventSpec)
## Community memory (recalled facts + voice examples)
## Task             (artifact spec above)
Return JSON matching the schema. Do not invent facts not in Event or Memory.
```

**Guardrails:** Pydantic-validate, one retry with the error message, then template. Facts not in the spec or memory must not appear (no invented speakers, no made-up prices).

**Definition of done**
- [ ] All five artifacts generated with no key (template tier)
- [ ] With a key, the copy mentions the real title, date and venue
- [ ] The log says which tier produced each artifact
- [ ] Links in copy point at `spec.luma_url`

---

### WP5 · `pipeline.py` — orchestration and CLI &nbsp; ⏱️ ~20 min

**Goal:** one command runs the whole thing and returns a `RunResult`.

```bash
python -m eventautopilot.pipeline https://lu.ma/pxzsiu78
python -m eventautopilot.pipeline "AI x Climate meetup, 14 Nov, ~60 people, pizza"   # one-liner mode
```

Flow: `luma.fetch` → `memory.recall` → `planner.plan` → `copy.generate` → `memory.remember` → compute `hours_saved` → write `out/run_<id>.json` → (Aryan's `notion_sync.push`, once it exists).

- [ ] Every step appends to `RunResult.log` with timing, so the demo narrates itself
- [ ] `hours_saved` uses the baseline table in [PROJECT.md §9](./PROJECT.md#9--how-we-are-judged-and-how-we-score)
- [ ] A step failing yields a degraded result and a log line, never a crash
- [ ] One-liner mode: extract title/date/size/food with Grok or simple parsing; unknowns → `missing`

**Definition of done**
- [ ] Link → `out/run_<id>.json` in under 90 seconds, with **no keys set**
- [ ] Running a second event shows `memories_used` that reference the first
- [ ] The hand-off call to Aryan's `notion_sync.push(result)` is wired behind `try/except ImportError`

---

## ⏱️ Suggested order

| Block | Do | Gate |
|---|---|---|
| 0:00–0:10 | **Step 0**: push contract + fixture | C0 ✅ |
| 0:10–0:30 | **WP1** luma · start **WP2A** schema in parallel | |
| 0:30–1:05 | **WP2B/C** seed + memory · **WP3** planner in parallel | |
| 1:05–1:30 | **WP4** llm + copy · **WP5** pipeline | **C1** real `RunResult` |
| then | Integrate with Aryan, run end to end ×3 | **C2** |

WP1 + WP2A, WP2 + WP3, and WP4 + WP5 are independent pairs, so they can run as parallel agents.

---

## 🔑 What you need

| Item | Needed for | Status |
|---|---|---|
| Supabase project (MCP, no key) | WP2 | ⬜ create |
| `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` in `.env` | WP2 live mode | ⬜ from MCP |
| xAI `GROK_API_KEY` | WP4 tier 2 | ⬜ console.x.ai |
| Grok Bot interface | WP4 tier 1 | ❓ Q1 |

Nothing here blocks the build: every item has a no-key fallback.

---

## ✅ Your acceptance checklist

- [ ] `seed/sample_run.json` pushed and Aryan told
- [ ] `fetch_event` works on live links
- [ ] Knowledge base live in Supabase, seeded, flagged `seed: true`
- [ ] `recall` → `plan` → `copy` → `remember` run end to end
- [ ] Every budget line has a `source`
- [ ] Runs with zero keys and says what it fell back to
- [ ] Second event visibly reuses the first's venue/caterer
- [ ] Committed, pushed, merged before **21:30**
