"""Shared knowledge base: Supabase (PostgREST over httpx) with a local JSON fallback.

Public API: recall, remember, learn_from_edit, graph, backend, volunteers.

Failure policy:
  * reads (recall / graph / volunteers) fall back to the local store, with a logged reason;
  * writes (remember / learn_from_edit) NEVER fall back: a half-written Supabase run replayed
    into local would fork the two stores. They log `memory: supabase error <Exc> <status>`
    and raise MemoryWriteError (the pipeline's _step records it). Writes are idempotent, so
    simply re-running is safe.
"""
from __future__ import annotations

import atexit
import difflib
import hashlib
import json
import logging
import os
import re
import tempfile
import uuid
from typing import Any

import httpx

from . import config
from .models import Entity, EventSpec, Memory, Relation

SEED_PATH = config.ROOT / "seed" / "knowledge_seed.json"
LOCAL_PATH = config.OUT / "memory.json"
GRAPH_PATH = config.OUT / "graph.json"

log = logging.getLogger("eventautopilot.memory")

_STOP = set(
    "a an and the of for to in on at with is are be by or from this that it as we our "
    "event events london meetup".split()
)
_BASE_TERMS = ["cost", "per", "head", "food", "pizza", "budget", "timing", "volunteer"]
_KNOWN_KINDS = {"venue", "supplier", "sponsor", "volunteer", "community", "event", "person",
                "speaker", "entity"}
_PREFERRED_KINDS = ("budget", "venue", "volunteer", "lesson", "voice", "supplier")
# "Venue hire: 19 x 6.00 GBP = 114.00" - a budget line re-learned from a previous plan.
_JUNK_RE = re.compile(r"^\s*[^:\n]{1,80}:\s*[\d.,]+\s*x\s*[\d.,]+\s*[A-Za-z]{0,4}\s*=\s*[\d.,]+")
_warned: set[str] = set()


class MemoryWriteError(RuntimeError):
    """A Supabase write failed; nothing was replayed into the local store."""


def backend() -> str:
    """'supabase' when keys are configured, else 'local'."""
    return "supabase" if config.HAS_SUPABASE else "local"


def _note_once(key: str, msg: str) -> None:
    log.warning(msg)
    if key not in _warned:
        _warned.add(key)
        print(msg)


def _err_desc(e: BaseException) -> str:
    """'<ExcType> <status>' - never includes URLs, headers or keys."""
    status = getattr(getattr(e, "response", None), "status_code", "-")
    return f"{type(e).__name__} {status}"


def _read_fallback(what: str, e: BaseException | None = None) -> None:
    reason = f"memory: supabase error {_err_desc(e)} ({what})" if e else "memory: no supabase keys"
    _note_once("read:" + reason, reason + "; using local fallback")


# ---------------------------------------------------------------- helpers

def _tokens(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9']+", (text or "").lower())
    return [t for t in toks if len(t) > 2 and t not in _STOP]


def _split_terms(spec: EventSpec) -> tuple[list[str], list[str]]:
    """(specific, base): spec-derived terms in order, and the generic budget terms."""
    parts = [
        spec.title, spec.one_liner, spec.description[:400], spec.audience, spec.format,
        spec.venue.name, spec.venue.city, spec.venue.address,
    ]
    base = list(_BASE_TERMS)
    seen: dict[str, None] = {}
    for t in _tokens(" ".join(parts)):
        if t not in base:
            seen.setdefault(t, None)
    return list(seen), base


def _query_terms(spec: EventSpec) -> tuple[list[str], list[str]]:
    """Terms actually searched: first 8 specific ones, plus every base term."""
    specific, base = _split_terms(spec)
    return specific[:8], base


def _spec_terms(spec: EventSpec) -> list[str]:
    specific, base = _split_terms(spec)
    return specific + base


def _is_uuid(v: str) -> bool:
    try:
        uuid.UUID(str(v))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _event_uuid(event_id: str) -> str | None:
    """The event's UUID: itself if it is one, else a deterministic uuid5 of the id string."""
    if not event_id:
        return None
    if _is_uuid(event_id):
        return str(uuid.UUID(event_id))
    return str(uuid.uuid5(uuid.NAMESPACE_URL, event_id))


def _resolve_ref(ref: str, kinds: set[str], by_name: dict[str, list[str]]) -> tuple[str, str]:
    """'kind:name' -> (kind, name); a bare name resolves to a known entity of that name."""
    kind, sep, name = ref.partition(":")
    if sep and name and kind.strip().lower() in kinds:
        return kind.strip().lower(), name
    found = by_name.get(ref)
    return (found[0], ref) if found else ("entity", ref)


def _norm_text(t: str) -> str:
    return " ".join((t or "").lower().split())


def _md5(t: str) -> str:
    return hashlib.md5(t.encode("utf-8")).hexdigest()


def _finalize(cands: list[tuple[float, dict]], spec: EventSpec, kinds: list[str] | None,
              limit: int) -> list[Memory]:
    """Shared post-processing: drop own/junk/duplicate rows, prefer useful kinds, keep a volunteer."""
    own = {x.strip().lower() for x in (spec.title, spec.id, f"event {spec.id}" if spec.id else "")
           if x and x.strip()}
    seen: set[str] = set()
    keep: list[tuple[float, dict]] = []
    for score, row in cands:
        text = row.get("text") or ""
        if (row.get("provenance") or "").strip().lower() in own or _JUNK_RE.match(text):
            continue
        boosted = score + (0.5 if row.get("kind") in _PREFERRED_KINDS else 0.0)
        keep.append((boosted, row))
    keep.sort(key=lambda x: x[0], reverse=True)
    uniq: list[tuple[float, dict]] = []
    for score, row in keep:
        key = _norm_text(row["text"])
        if key not in seen:
            seen.add(key)
            uniq.append((score, row))
    top = uniq[:limit]
    if top and (not kinds or "volunteer" in kinds) and not any(r["kind"] == "volunteer" for _, r in top):
        vol = next((x for x in uniq[limit:] if x[1]["kind"] == "volunteer"), None)
        if vol:
            top[-1] = vol
    return [Memory(kind=r["kind"], text=r["text"], tags=r.get("tags") or [],
                   provenance=r.get("provenance") or "", score=round(s, 3)) for s, r in top]


def _atomic_write(path: Any, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- local store

def _load_local() -> dict[str, Any]:
    data: dict[str, Any] | None = None
    if LOCAL_PATH.exists():
        try:
            data = json.loads(LOCAL_PATH.read_text())
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except (OSError, ValueError):
            bak = LOCAL_PATH.with_name(LOCAL_PATH.name + ".bak")
            try:
                os.replace(LOCAL_PATH, bak)
                _note_once("corrupt", f"memory: corrupt {LOCAL_PATH.name} moved to {bak.name}; reseeding")
            except OSError:
                pass
            data = None
    if data is None:
        data = {"entities": [], "relations": [], "memories": [], "edits": []}
        if SEED_PATH.exists():
            data.update(json.loads(SEED_PATH.read_text()))
        for k in ("entities", "relations", "memories", "edits"):
            data.setdefault(k, [])
        data.setdefault("events", {})
        _save_local(data)
        return data
    for k in ("entities", "relations", "memories", "edits"):
        data.setdefault(k, [])
    data.setdefault("events", {})
    return data


def _save_local(data: dict[str, Any]) -> None:
    _atomic_write(LOCAL_PATH, json.dumps(data, indent=1, ensure_ascii=False))


def _local_recall(spec: EventSpec, kinds: list[str] | None, limit: int) -> list[Memory]:
    data = _load_local()
    specific, base = _query_terms(spec)
    spec_set = set(specific)
    terms = specific + base
    cands: list[tuple[float, dict]] = []
    for m in data["memories"]:
        if kinds and m["kind"] not in kinds:
            continue
        hay = set(_tokens(m["text"] + " " + " ".join(m.get("tags", []))))
        hits = sum(1 for t in terms if t in hay)
        spec_hits = sum(1 for t in spec_set if t in hay)
        score = hits + 1.5 * spec_hits
        ent = m.get("entity") or ""
        if ent and ent.split(":", 1)[-1].lower() in (spec.venue.name or "").lower():
            score += 3
        cands.append((score, m))
    return _finalize(cands, spec, kinds, limit)


def _local_remember(event_id: str, facts: list[Memory], entities: list[Entity],
                    relations: list[Relation]) -> None:
    data = _load_local()
    ev = event_id or ""
    index = {(e["kind"], e["name"]): e for e in data["entities"]}
    by_name: dict[str, list[str]] = {}
    for k, n in index:
        by_name.setdefault(n, []).append(k)
    for e in entities:
        by_name.setdefault(e.name, [])
        if e.kind not in by_name[e.name]:
            by_name[e.name].append(e.kind)
    kinds = _KNOWN_KINDS | {e.kind for e in entities} | {k for k, _ in index}
    title = next((e.name for e in entities if e.kind == "event"), "")

    def touch(kind: str, name: str, attrs: dict, used: int) -> None:
        cur = index.get((kind, name))
        if cur is None:
            cur = {"kind": kind, "name": name, "attrs": dict(attrs), "times_used": used,
                   "events": [ev] if ev else []}
            data["entities"].append(cur)
            index[(kind, name)] = cur
            return
        cur["attrs"] = {**cur.get("attrs", {}), **attrs}
        linked = cur.setdefault("events", [])
        if ev and ev not in linked:  # bump only when this (event, entity) pair is new
            linked.append(ev)
            cur["times_used"] = (cur.get("times_used") or 0) + 1

    done: set[tuple[str, str]] = set()
    for e in entities:
        if (e.kind, e.name) not in done:
            done.add((e.kind, e.name))
            touch(e.kind, e.name, e.attrs, e.times_used)
    have = {(r["source"], r["relation"], r["target"]) for r in data["relations"]}
    for r in relations:
        s = _resolve_ref(r.source, kinds, by_name)
        t = _resolve_ref(r.target, kinds, by_name)
        for k in (s, t):
            if k not in done:
                done.add(k)
                touch(k[0], k[1], {}, 1)
        rec = {"source": f"{s[0]}:{s[1]}", "relation": r.relation, "target": f"{t[0]}:{t[1]}",
               "event_id": r.event_id or ev}
        if (rec["source"], rec["relation"], rec["target"]) in have:
            continue
        have.add((rec["source"], rec["relation"], rec["target"]))
        data["relations"].append(rec)
        if rec["event_id"] and rec["event_id"] != ev:
            data["events"].setdefault(rec["event_id"], {})
    if ev:
        info = data["events"].setdefault(ev, {})
        if title:
            info["title"] = title
    seen_hash = {_md5(m["text"]) for m in data["memories"]}
    for f in facts:
        h = _md5(f.text)
        if h in seen_hash:
            continue
        seen_hash.add(h)
        data["memories"].append({"kind": f.kind, "text": f.text, "tags": f.tags,
                                 "provenance": f.provenance or f"event {event_id}",
                                 "entity": None})
    _save_local(data)


def _local_learn(artifact_id: str, before: str, after: str, notes: str, text: str) -> None:
    data = _load_local()
    data["edits"].append({"artifact_id": artifact_id, "before": before, "after": after, "notes": notes})
    if _md5(text) not in {_md5(m["text"]) for m in data["memories"]}:
        data["memories"].append({"kind": "voice", "text": text, "tags": ["voice", "edit"],
                                 "provenance": f"organiser edit of artifact {artifact_id}",
                                 "entity": None})
    _save_local(data)


def _local_volunteers() -> list[dict]:
    return _vol_rows(_load_local()["entities"])


def _vol_rows(ents: list[dict]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for e in ents:
        if e.get("kind") != "volunteer" or e["name"] in seen:
            continue
        seen.add(e["name"])
        a = e.get("attrs") or {}
        out.append({"name": e["name"], "usual_role": a.get("usual_role") or a.get("role") or ""})
    return out


def _local_graph(limit: int, highlight: str | None) -> dict[str, Any]:
    data = _load_local()
    ents = sorted(data["entities"], key=lambda e: e.get("times_used") or 0, reverse=True)[:limit]
    kinds = _KNOWN_KINDS | {e["kind"] for e in data["entities"]}
    by_name: dict[str, list[str]] = {}
    for e in data["entities"]:
        by_name.setdefault(e["name"], []).append(e["kind"])
    titles = {k: (v.get("title") or k) for k, v in data["events"].items()}
    hl_ids = _local_highlight_ids(data, highlight)
    nodes = []
    for e in ents:
        evs = e.get("events") or []
        node = {"id": f"{e['kind']}:{e['name']}", "name": e["name"], "label": e["name"],
                "kind": e["kind"], "times_used": e.get("times_used") or 0,
                "attrs": e.get("attrs") or {}, "events": list(dict.fromkeys(titles.get(x, x) for x in evs))}
        if highlight is not None:
            node["new"] = bool(hl_ids & set(evs))
        nodes.append(node)
    valid = {n["id"] for n in nodes}
    edges = []
    seen: set[tuple[str, str, str]] = set()
    for r in data["relations"]:
        s = "%s:%s" % _resolve_ref(r["source"], kinds, by_name)
        t = "%s:%s" % _resolve_ref(r["target"], kinds, by_name)
        if s not in valid or t not in valid or (s, r["relation"], t) in seen:
            continue
        seen.add((s, r["relation"], t))
        edge = {"source": s, "target": t, "relation": r["relation"]}
        if highlight is not None:
            edge["new"] = (r.get("event_id") or "") in hl_ids
        edges.append(edge)
    return {"nodes": nodes, "edges": edges}


def _local_highlight_ids(data: dict[str, Any], highlight: str | None) -> set[str]:
    if highlight is None:
        return set()
    ids = {highlight}
    ids |= {k for k, v in data["events"].items() if v.get("title") == highlight}
    return ids


# ---------------------------------------------------------------- supabase

_client: httpx.Client | None = None
_client_key: tuple[str, str] | None = None


def _http() -> httpx.Client:
    """Single lazily-created client, rebuilt only if the configured URL/key changes."""
    global _client, _client_key
    key = (config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY)
    if _client is None or key != _client_key:
        _close_client()
        _client = httpx.Client(
            base_url=f"{config.SUPABASE_URL}/rest/v1",
            headers={"apikey": config.SUPABASE_SERVICE_KEY,
                     "Authorization": f"Bearer {config.SUPABASE_SERVICE_KEY}",
                     "Content-Type": "application/json"},
            timeout=10.0,
        )
        _client_key = key
    return _client


def _close_client() -> None:
    global _client, _client_key
    if _client is not None:
        try:
            _client.close()
        except Exception:  # noqa: BLE001
            pass
    _client, _client_key = None, None


atexit.register(_close_client)


class _SB:
    """Tiny PostgREST wrapper over the shared client."""

    def __init__(self) -> None:
        self.c = _http()

    def get(self, table: str, params: dict[str, str]) -> list[dict]:
        r = self.c.get(f"/{table}", params=params)
        r.raise_for_status()
        return r.json()

    def post(self, table: str, rows: list[dict] | dict, prefer: str = "return=representation",
             params: dict[str, str] | None = None) -> list[dict]:
        r = self.c.post(f"/{table}", json=rows, params=params, headers={"Prefer": prefer})
        r.raise_for_status()
        return r.json() if r.content else []

    def rpc(self, fn: str, args: dict) -> Any:
        r = self.c.post(f"/rpc/{fn}", json=args)
        r.raise_for_status()
        return r.json() if r.content else None


def _eq(v: str) -> str:
    return "eq." + v


def _in(vals: list[str]) -> str:
    """PostgREST in.(...) list with each value double-quoted (commas/parens are safe)."""
    return "in.(" + ",".join('"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"' for v in vals) + ")"


def _sb_recall(spec: EventSpec, kinds: list[str] | None, limit: int) -> list[Memory]:
    sb = _SB()
    specific, base = _query_terms(spec)
    fts_terms = [t.replace("'", "") for t in specific + base]
    cols = "id,kind,text,tags,provenance,entity_id"
    flt: dict[str, str] = {"select": cols, "limit": "60"}
    if kinds:
        flt["kind"] = _in(kinds)
    scored: dict[str, tuple[float, dict]] = {}

    for i, row in enumerate(sb.get("memories", {**flt, "search": "wfts(english)." + " or ".join(fts_terms)})):
        scored[row["id"]] = (2.0 / (1 + i * 0.15), row)

    # Entity-name match (trigram-backed ilike): venue and other names mentioned in the spec.
    names = [n for n in [spec.venue.name, spec.title] if n] + specific[:6]
    ent_ids: list[str] = []
    for n in names[:8]:
        rows = sb.get("entities", {"select": "id", "name": f"ilike.*{n.replace('*', '')}*", "limit": "5"})
        ent_ids += [r["id"] for r in rows]
    if ent_ids:
        for row in sb.get("memories", {**flt, "entity_id": _in(sorted(set(ent_ids)))}):
            old = scored.get(row["id"], (0.0, row))[0]
            scored[row["id"]] = (old + 1.0, row)

    if not kinds or "volunteer" in kinds:  # a rota needs people: always surface volunteer facts
        if not any(r["kind"] == "volunteer" for _, r in scored.values()):
            for row in sb.get("memories", {"select": cols, "kind": _eq("volunteer"), "limit": "5"}):
                scored.setdefault(row["id"], (0.6, row))

    if len(scored) < limit:  # top up with generally useful facts
        general = {"select": cols, "limit": str(limit * 2), "order": "created_at.desc",
                   "kind": _in(kinds or list(_PREFERRED_KINDS))}
        for row in sb.get("memories", general):
            scored.setdefault(row["id"], (0.1, row))

    return _finalize(list(scored.values()), spec, kinds, limit)


def _sb_remember(event_id: str, facts: list[Memory], entities: list[Entity],
                 relations: list[Relation]) -> None:
    sb = _SB()
    ev_uuid = _event_uuid(event_id)
    title = next((e.name for e in entities if e.kind == "event"), "")

    # events rows: the run's own event (with title) and any per-relation event ids.
    ev_rows: dict[str, dict] = {}
    if ev_uuid:
        ev_rows[ev_uuid] = {"id": ev_uuid, "source_id": event_id}
        if title:
            ev_rows[ev_uuid]["title"] = title
    for r in relations:
        u = _event_uuid(r.event_id)
        if u and u not in ev_rows:
            ev_rows[u] = {"id": u, "source_id": r.event_id}
    titled = [v for v in ev_rows.values() if "title" in v]
    plain = [v for v in ev_rows.values() if "title" not in v]
    if titled:
        sb.post("events", titled, prefer="resolution=merge-duplicates,return=minimal",
                params={"on_conflict": "id"})
    if plain:
        sb.post("events", plain, prefer="resolution=ignore-duplicates,return=minimal",
                params={"on_conflict": "id"})

    ids: dict[tuple[str, str], str] = {}

    def upsert(kind: str, name: str, attrs: dict, used: int) -> None:
        # Atomic server-side upsert; bumps times_used only for a new (event, entity) pair.
        res = sb.rpc("upsert_entity", {
            "p_kind": kind, "p_name": name, "p_attrs": attrs, "p_event": ev_uuid, "p_used": used})
        if isinstance(res, list):  # tolerate table-shaped replies
            res = res[0] if res else None
        if isinstance(res, dict):
            res = res.get("upsert_entity") or res.get("id")
        if not res:
            raise ValueError("upsert_entity returned no id")
        ids[(kind, name)] = str(res)

    by_name: dict[str, list[str]] = {}
    for e in entities:
        by_name.setdefault(e.name, [])
        if e.kind not in by_name[e.name]:
            by_name[e.name].append(e.kind)
    kinds = _KNOWN_KINDS | {e.kind for e in entities}
    for e in entities:
        if (e.kind, e.name) not in ids:
            upsert(e.kind, e.name, e.attrs, e.times_used)

    def ref_id(ref: str) -> tuple[str, str]:
        key = _resolve_ref(ref, kinds, by_name)
        if key[0] == "entity" and ":" not in ref and key not in ids:
            rows = sb.get("entities", {"select": "id,kind", "name": _eq(ref), "limit": "1"})
            if rows:
                key = (rows[0]["kind"], ref)
                ids[key] = rows[0]["id"]
        if key not in ids:
            upsert(key[0], key[1], {}, 1)
        return key

    rel_rows: dict[tuple[str, str, str], dict] = {}
    for r in relations:
        s, t = ids[ref_id(r.source)], ids[ref_id(r.target)]
        rel_rows.setdefault((s, r.relation, t), {
            "source_id": s, "relation": r.relation, "target_id": t,
            "event_id": _event_uuid(r.event_id) or ev_uuid})
    if rel_rows:
        sb.post("relations", list(rel_rows.values()),
                prefer="resolution=ignore-duplicates,return=minimal",
                params={"on_conflict": "source_id,relation,target_id"})

    mem_rows: dict[str, dict] = {}
    for f in facts:
        mem_rows.setdefault(_md5(f.text), {
            "kind": f.kind, "text": f.text, "tags": f.tags,
            "provenance": f.provenance or f"event {event_id}", "event_id": ev_uuid})
    if mem_rows:  # text_hash is a generated column with a unique index
        sb.post("memories", list(mem_rows.values()),
                prefer="resolution=ignore-duplicates,return=minimal",
                params={"on_conflict": "text_hash"})


def _sb_learn(artifact_id: str, before: str, after: str, notes: str, text: str) -> None:
    sb = _SB()
    sb.post("edits", {"artifact_id": artifact_id, "before_text": before, "after_text": after,
                      "notes": notes}, prefer="return=minimal")
    sb.post("memories", {"kind": "voice", "text": text, "tags": ["voice", "edit"],
                         "provenance": f"organiser edit of artifact {artifact_id}"},
            prefer="resolution=ignore-duplicates,return=minimal",
            params={"on_conflict": "text_hash"})


def _sb_volunteers() -> list[dict]:
    rows = _SB().get("entities", {"select": "kind,name,attrs", "kind": _eq("volunteer"),
                                  "order": "times_used.desc.nullslast,name.asc", "limit": "200"})
    return _vol_rows(rows)


def _sb_graph(limit: int, highlight: str | None) -> dict[str, Any]:
    sb = _SB()
    ents = sb.get("entities", {"select": "id,kind,name,times_used,attrs",
                               "order": "times_used.desc.nullslast,name.asc", "limit": str(limit)})
    node_id = {e["id"]: f"{e['kind']}:{e['name']}" for e in ents}
    ev_titles: dict[str, str] = {}
    ent_events: dict[str, list[str]] = {}
    hl: set[str] = set()
    if ents:
        try:  # event hints are optional; the graph still renders without them
            links = sb.get("event_entities", {"select": "event_id,entity_id",
                                              "entity_id": _in(list(node_id)), "limit": "5000"})
            for k in links:
                ent_events.setdefault(k["entity_id"], []).append(k["event_id"])
            evs = sb.get("events", {"select": "id,title,source_id",
                                    "id": _in(sorted({k["event_id"] for k in links})), "limit": "1000"}) \
                if links else []
            for ev in evs:
                ev_titles[ev["id"]] = ev.get("title") or ev.get("source_id") or ev["id"]
                if highlight is not None and highlight in (ev.get("title"), ev.get("source_id")):
                    hl.add(ev["id"])
        except Exception as e:  # noqa: BLE001
            log.warning("memory: graph event hints skipped (%s)", _err_desc(e))
    if highlight is not None:
        u = _event_uuid(highlight)
        if u:
            hl.add(u)
    nodes = []
    for e in ents:
        evs_for = ent_events.get(e["id"], [])
        node = {"id": node_id[e["id"]], "name": e["name"], "label": e["name"], "kind": e["kind"],
                "times_used": e.get("times_used") or 0, "attrs": e.get("attrs") or {},
                "events": list(dict.fromkeys(ev_titles.get(x, x) for x in evs_for))}
        if highlight is not None:
            node["new"] = bool(hl & set(evs_for))
        nodes.append(node)
    edges: list[dict] = []
    if node_id:
        rels = sb.get("relations", {"select": "source_id,target_id,relation,event_id",
                                    "source_id": _in(list(node_id)), "limit": "2000"})
        for r in rels:
            if r["target_id"] in node_id:
                edge = {"source": node_id[r["source_id"]], "target": node_id[r["target_id"]],
                        "relation": r["relation"]}
                if highlight is not None:
                    edge["new"] = r.get("event_id") in hl
                edges.append(edge)
    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------- public API

def recall(spec: EventSpec, kinds: list[str] | None = None, limit: int = 12) -> list[Memory]:
    """Facts relevant to this event, ranked by relevance. Each carries .provenance."""
    if config.HAS_SUPABASE:
        try:
            return _sb_recall(spec, kinds, limit)
        except Exception as e:  # noqa: BLE001 - reads degrade to local, with a logged reason
            _read_fallback("recall", e)
    else:
        _read_fallback("recall")
    return _local_recall(spec, kinds, limit)


def _write_failed(e: Exception) -> MemoryWriteError:
    msg = f"memory: supabase error {_err_desc(e)}"
    _note_once("write:" + msg, msg + " (write NOT replayed locally)")
    return MemoryWriteError(msg)


def remember(event_id: str, facts: list[Memory], entities: list[Entity],
             relations: list[Relation]) -> None:
    """Write back what a run learned; entities upsert on (kind, name).

    times_used is bumped only the first time an event touches an entity, so re-running the
    same event_id is a no-op for counts. Supabase failures raise MemoryWriteError (no local replay).
    """
    if config.HAS_SUPABASE:
        try:
            _sb_remember(event_id, facts, entities, relations)
        except Exception as e:  # noqa: BLE001
            raise _write_failed(e) from None
        return
    _local_remember(event_id, facts, entities, relations)


def _edit_text(before: str, after: str, notes: str) -> str:
    a, b = before.split(), after.split()
    removed: list[str] = []
    added: list[str] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if op in ("replace", "delete"):
            removed.append(" ".join(a[i1:i2]))
        if op in ("replace", "insert"):
            added.append(" ".join(b[j1:j2]))
    text = "Organiser edited our copy."
    if removed:
        text += " Removed: " + "; ".join(f'"{x}"' for x in removed[:5]) + "."
    if added:
        text += " Added: " + "; ".join(f'"{x}"' for x in added[:5]) + "."
    if notes:
        text += f" Notes: {notes}"
    return text[:900]


def learn_from_edit(artifact_id: str, before: str, after: str, notes: str = "") -> None:
    """Store an organiser's edit diff as a 'voice' memory (and in the edits table)."""
    text = _edit_text(before, after, notes)
    if config.HAS_SUPABASE:
        try:
            _sb_learn(artifact_id, before, after, notes, text)
        except Exception as e:  # noqa: BLE001
            raise _write_failed(e) from None
        return
    _local_learn(artifact_id, before, after, notes, text)


def volunteers() -> list[dict]:
    """Known volunteers as [{'name', 'usual_role'}] (usual_role '' when unknown)."""
    if config.HAS_SUPABASE:
        try:
            return _sb_volunteers()
        except Exception as e:  # noqa: BLE001
            _read_fallback("volunteers", e)
    return _local_volunteers()


def graph(limit: int = 150, highlight_event: str | None = None) -> dict:
    """Nodes + edges for the graph view; also written to out/graph.json.

    Node id is 'kind:name' in both modes. With highlight_event (event id, uuid or title) every
    node/edge gets a boolean `new`, true when that event touched it.
    """
    result: dict[str, Any] | None = None
    if config.HAS_SUPABASE:
        try:
            result = _sb_graph(limit, highlight_event)
        except Exception as e:  # noqa: BLE001
            _read_fallback("graph", e)
    if result is None:
        result = _local_graph(limit, highlight_event)
    _atomic_write(GRAPH_PATH, json.dumps(result, indent=1, ensure_ascii=False))
    return result
