get # graph/: Community Knowledge Graph (WP4)

A single static page (`index.html`, Cytoscape.js 3.30.2 from cdnjs) that draws the shared memory.

## Open it

- **Double-click** `index.html` (file://): renders the inline seeded snapshot. The **SNAPSHOT** badge is shown.
- **Served** (recommended, so `../out/graph.json` resolves): from the repo root run
  `python3 -m http.server 8765` and open <http://localhost:8765/graph/index.html>
- **Live Supabase**: `index.html?supabase=https://<ref>.supabase.co&key=<anon key>`, or fill
  `CONFIG` at the top of `index.html`. Needs public-read RLS policies on `entities`, `relations`
  (and, optionally, `memories`).

The header shows the active source: `LIVE` (Supabase), `FILE` (graph.json) or `SNAPSHOT`.

## Source order

1. Supabase REST: `GET /rest/v1/entities?select=*`, `/rest/v1/relations?select=*`, `/rest/v1/memories?select=…` (optional)
2. `../out/graph.json` (written by `memory.graph()`), then `./graph.json`
3. `./snapshot.json`, then the inline copy embedded in `index.html` (used on file://)

## Expected `graph.json` format (what `memory.graph()` should write)

```jsonc
{
  "nodes": [
    {
      "id": "venue:Rise London",      // any unique string: a uuid or "kind:name"
      "kind": "venue",                // venue | supplier | sponsor | volunteer | community | event (others render grey)
      "name": "Rise London",          // or "label"
      "attrs": { "capacity": 80 },    // shown as key/value; attrs.seeded=true → "seeded example" tag
      "times_used": 3,                // drives node size
      "seeded": false,                // optional
      "created_at": "2026-10-22T09:00:00Z"   // optional; on events it decides "latest event"
    }
  ],
  "edges": [
    {
      "source": "venue:Rise London",  // a node id OR "kind:name" (case-insensitive)
      "target": "event:AI x Climate Meetup",
      "relation": "hosted",           // hosted | catered | sponsored | ran_shift | supplied | organised
      "event_id": "event:AI x Climate Meetup"   // optional
    }
  ],
  "memories": [                       // optional: shown under "What we remember"
    { "kind": "venue", "text": "Lift closes at 22:00", "entity_id": "venue:Rise London", "event_id": "event:…" }
  ]
}
```

Raw Supabase rows also work: `entities` (`id, kind, name, attrs, times_used, created_at`) and
`relations` (`source_id, relation, target_id, event_id`). Direction convention: the thing points
at the event (`venue → hosted → event`, `volunteer → ran_shift → event`).

"Highlight latest event" picks the event with the newest `created_at` (falling back to
`attrs.date`, then to the last event in the list).

## Demo

1. Open the page, then click **Highlight latest event**. The new event lights up and latches onto Rise London, Pizza Pilgrims and Sam.
2. Run a second event and click **Refresh** (or press `r`). Nodes that were not there before get a double
   border, a pulse, and the camera zooms to them. Shortcuts: `l` latest, `r` refresh, `Esc` clear.

## Keeping the inline snapshot in sync

After editing `snapshot.json`, re-embed it in `index.html`:

```bash
python3 - <<'EOF'
import json, re, pathlib
g = pathlib.Path("graph")
data = json.dumps(json.load(open(g/"snapshot.json")), ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
h = (g/"index.html").read_text()
h = re.sub(r'(<script id="snapshot-inline" type="application/json">\n).*?(\n</script>)', lambda m: m.group(1)+data+m.group(2), h, flags=re.S)
(g/"index.html").write_text(h)
EOF
```
