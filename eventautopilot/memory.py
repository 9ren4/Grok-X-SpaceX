"""Shared knowledge base: Supabase (PostgREST over httpx) with a local JSON fallback.

Public API: recall, remember, learn_from_edit, graph, backend.
Any Supabase error degrades to the local store (out/memory.json) instead of crashing.
"""
from __future__ import annotations

import difflib
import json
import re
import uuid
from typing import Any

import httpx

from . import config
from .models import Entity, EventSpec, Memory, Relation

SEED_PATH = config.ROOT / "seed" / "knowledge_seed.json"
LOCAL_PATH = config.OUT / "memory.json"
GRAPH_PATH = config.OUT / "graph.json"

_STOP = set(
    "a an and the of for to in on at with is are be by or from this that it as we our "
    "event events london meetup".split()
)
_BASE_TERMS = ["cost", "per", "head", "food", "pizza", "budget", "timing", "volunteer"]
_warned = False


def backend() -> str:
    """'supabase' when keys are configured, else 'local'."""
    return "supabase" if config.HAS_SUPABASE else "local"


def _fallback_note() -> None:
    global _warned
    if not _warned:
        print("memory: local fallback")
        _warned = True


# ---------------------------------------------------------------- helpers

def _tokens(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9']+", (text or "").lower())
    return [t for t in toks if len(t) > 2 and t not in _STOP]


def _spec_terms(spec: EventSpec) -> list[str]:
    parts = [
        spec.title, spec.one_liner, spec.description[:400], spec.audience, spec.format,
        spec.venue.name, spec.venue.city, spec.venue.address,
    ]
    terms = _tokens(" ".join(parts))
    if spec.expected_attendees:
        terms.append("per")
    seen: dict[str, None] = {}
    for t in terms + _BASE_TERMS:
        seen.setdefault(t, None)
    return list(seen)


def _is_uuid(v: str) -> bool:
    try:
        uuid.UUID(str(v))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _split_ref(ref: str) -> tuple[str, str]:
    kind, _, name = ref.partition(":")
    return (kind, name) if name else ("entity", ref)


# ---------------------------------------------------------------- local store

def _load_local() -> dict[str, Any]:
    if LOCAL_PATH.exists():
        try:
            return json.loads(LOCAL_PATH.read_text())
        except (OSError, ValueError):
            pass
    data: dict[str, Any] = {"entities": [], "relations": [], "memories": [], "edits": []}
    if SEED_PATH.exists():
        data.update(json.loads(SEED_PATH.read_text()))
        data.setdefault("edits", [])
    _save_local(data)
    return data


def _save_local(data: dict[str, Any]) -> None:
    LOCAL_PATH.write_text(json.dumps(data, indent=1, ensure_ascii=False))


def _local_recall(spec: EventSpec, kinds: list[str] | None, limit: int) -> list[Memory]:
    data = _load_local()
    terms = _spec_terms(spec)
    specific = set(terms[: len(terms) - len(_BASE_TERMS)])
    out: list[Memory] = []
    for m in data["memories"]:
        if kinds and m["kind"] not in kinds:
            continue
        hay = set(_tokens(m["text"] + " " + " ".join(m.get("tags", []))))
        hits = sum(1 for t in terms if t in hay)
        spec_hits = sum(1 for t in specific if t in hay)
        score = hits + 1.5 * spec_hits
        ent = m.get("entity") or ""
        if ent and ent.split(":", 1)[-1].lower() in (spec.venue.name or "").lower():
            score += 3
        out.append(Memory(kind=m["kind"], text=m["text"], tags=m.get("tags", []),
                          provenance=m.get("provenance", ""), score=round(score, 3)))
    out.sort(key=lambda x: x.score, reverse=True)
    return out[:limit]


def _local_remember(event_id: str, facts: list[Memory], entities: list[Entity],
                    relations: list[Relation]) -> None:
    data = _load_local()
    index = {(e["kind"], e["name"]): e for e in data["entities"]}

    def upsert(kind: str, name: str, attrs: dict, used: int, new: bool = True) -> None:
        cur = index.get((kind, name))
        if cur:
            cur["times_used"] = cur.get("times_used", 0) + 1
            cur["attrs"] = {**cur.get("attrs", {}), **attrs}
        else:
            cur = {"kind": kind, "name": name, "attrs": attrs, "times_used": used}
            data["entities"].append(cur)
            index[(kind, name)] = cur

    for e in entities:
        upsert(e.kind, e.name, e.attrs, e.times_used)
    for r in relations:
        for ref in (r.source, r.target):
            kind, name = _split_ref(ref)
            if (kind, name) not in index:
                upsert(kind, name, {}, 1)
        rec = {"source": r.source, "relation": r.relation, "target": r.target,
               "event_id": r.event_id or event_id}
        if not any(x["source"] == rec["source"] and x["relation"] == rec["relation"]
                   and x["target"] == rec["target"] and x.get("event_id") == rec["event_id"]
                   for x in data["relations"]):
            data["relations"].append(rec)
    have = {m["text"] for m in data["memories"]}
    for f in facts:
        if f.text in have:
            continue
        data["memories"].append({"kind": f.kind, "text": f.text, "tags": f.tags,
                                 "provenance": f.provenance or f"event {event_id}",
                                 "entity": None})
    _save_local(data)


def _local_learn(artifact_id: str, before: str, after: str, notes: str, text: str) -> None:
    data = _load_local()
    data["edits"].append({"artifact_id": artifact_id, "before": before, "after": after, "notes": notes})
    data["memories"].append({"kind": "voice", "text": text, "tags": ["voice", "edit"],
                             "provenance": f"organiser edit of artifact {artifact_id}", "entity": None})
    _save_local(data)


def _local_graph(limit: int) -> dict[str, Any]:
    data = _load_local()
    ents = sorted(data["entities"], key=lambda e: e.get("times_used", 0), reverse=True)[:limit]
    ids = {(e["kind"], e["name"]): f"{e['kind']}:{e['name']}" for e in ents}
    nodes = [{"id": ids[(e["kind"], e["name"])], "label": e["name"], "kind": e["kind"],
              "times_used": e.get("times_used", 0), "attrs": e.get("attrs", {})} for e in ents]
    valid = set(ids.values())
    edges = [{"source": r["source"], "target": r["target"], "relation": r["relation"]}
             for r in data["relations"] if r["source"] in valid and r["target"] in valid]
    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------- supabase

class _SB:
    """Tiny PostgREST client."""

    def __init__(self) -> None:
        self.c = httpx.Client(
            base_url=f"{config.SUPABASE_URL}/rest/v1",
            headers={"apikey": config.SUPABASE_SERVICE_KEY,
                     "Authorization": f"Bearer {config.SUPABASE_SERVICE_KEY}",
                     "Content-Type": "application/json"},
            timeout=10.0,
        )

    def get(self, table: str, params: dict[str, str]) -> list[dict]:
        r = self.c.get(f"/{table}", params=params)
        r.raise_for_status()
        return r.json()

    def post(self, table: str, rows: list[dict] | dict, prefer: str = "return=representation",
             params: dict[str, str] | None = None) -> list[dict]:
        r = self.c.post(f"/{table}", json=rows, params=params, headers={"Prefer": prefer})
        r.raise_for_status()
        return r.json() if r.content else []

    def patch(self, table: str, params: dict[str, str], row: dict) -> None:
        r = self.c.patch(f"/{table}", params=params, json=row)
        r.raise_for_status()


def _eq(v: str) -> str:
    """PostgREST eq value, quoted so commas and parentheses are safe."""
    return "eq." + v


def _sb_recall(spec: EventSpec, kinds: list[str] | None, limit: int) -> list[Memory]:
    sb = _SB()
    terms = _spec_terms(spec)[:14]
    cols = "id,kind,text,tags,provenance,entity_id"
    base: dict[str, str] = {"select": cols, "limit": "60"}
    if kinds:
        base["kind"] = "in.(" + ",".join(kinds) + ")"
    scored: dict[str, tuple[float, dict]] = {}

    fts = sb.get("memories", {**base, "search": "wfts(english)." + " or ".join(terms)})
    for i, row in enumerate(fts):
        scored[row["id"]] = (2.0 / (1 + i * 0.15), row)

    # Entity-name match (trigram-backed ilike): venue and other names mentioned in the spec.
    names = [n for n in [spec.venue.name, spec.title] if n]
    for t in terms[:6]:
        names.append(t)
    ent_ids: list[str] = []
    for n in names[:8]:
        rows = sb.get("entities", {"select": "id", "name": f"ilike.*{n.replace('*', '')}*", "limit": "5"})
        ent_ids += [r["id"] for r in rows]
    if ent_ids:
        rows = sb.get("memories", {**base, "entity_id": "in.(" + ",".join(set(ent_ids)) + ")"})
        for row in rows:
            old = scored.get(row["id"], (0.0, row))[0]
            scored[row["id"]] = (old + 1.0, row)

    if len(scored) < limit:  # top up with generally useful budget / lesson facts
        general = {**base, "limit": str(limit), "order": "created_at.desc"}
        general["kind"] = "in.(" + ",".join(kinds or ["budget", "lesson", "venue"]) + ")"
        for row in sb.get("memories", general):
            scored.setdefault(row["id"], (0.1, row))

    ranked = sorted(scored.values(), key=lambda x: x[0], reverse=True)[:limit]
    return [Memory(kind=r["kind"], text=r["text"], tags=r.get("tags") or [],
                   provenance=r.get("provenance") or "", score=round(s, 3)) for s, r in ranked]


def _sb_entity_ids(sb: _SB, refs: set[tuple[str, str]]) -> dict[tuple[str, str], str]:
    ids: dict[tuple[str, str], str] = {}
    for kind, name in refs:
        rows = sb.get("entities", {"select": "id", "kind": _eq(kind), "name": _eq(name), "limit": "1"})
        if rows:
            ids[(kind, name)] = rows[0]["id"]
    return ids


def _sb_remember(event_id: str, facts: list[Memory], entities: list[Entity],
                 relations: list[Relation]) -> None:
    sb = _SB()
    ev_uuid: str | None = None
    if _is_uuid(event_id):
        ev_uuid = str(uuid.UUID(event_id))
        sb.post("events", {"id": ev_uuid}, prefer="resolution=ignore-duplicates,return=minimal",
                params={"on_conflict": "id"})

    ids: dict[tuple[str, str], str] = {}

    def upsert(kind: str, name: str, attrs: dict, used: int) -> None:
        rows = sb.get("entities", {"select": "id,attrs,times_used", "kind": _eq(kind),
                                   "name": _eq(name), "limit": "1"})
        if rows:
            cur = rows[0]
            sb.patch("entities", {"id": _eq(cur["id"])},
                     {"times_used": (cur.get("times_used") or 0) + 1,
                      "attrs": {**(cur.get("attrs") or {}), **attrs}})
            ids[(kind, name)] = cur["id"]
        else:
            new = sb.post("entities", {"kind": kind, "name": name, "attrs": attrs, "times_used": used})
            ids[(kind, name)] = new[0]["id"]

    for e in entities:
        upsert(e.kind, e.name, e.attrs, e.times_used)
    for r in relations:
        for ref in (r.source, r.target):
            kind, name = _split_ref(ref)
            if (kind, name) not in ids:
                found = _sb_entity_ids(sb, {(kind, name)})
                if found:
                    ids.update(found)
                else:
                    upsert(kind, name, {}, 1)
        s, t = ids[_split_ref(r.source)], ids[_split_ref(r.target)]
        q = {"select": "id", "source_id": _eq(s), "target_id": _eq(t),
             "relation": _eq(r.relation), "limit": "1"}
        if not sb.get("relations", q):
            sb.post("relations", {"source_id": s, "target_id": t, "relation": r.relation,
                                  "event_id": ev_uuid}, prefer="return=minimal")
    for f in facts:
        if sb.get("memories", {"select": "id", "text": _eq(f.text), "limit": "1"}):
            continue
        sb.post("memories", {"kind": f.kind, "text": f.text, "tags": f.tags,
                             "provenance": f.provenance or f"event {event_id}",
                             "event_id": ev_uuid}, prefer="return=minimal")


def _sb_learn(artifact_id: str, before: str, after: str, notes: str, text: str) -> None:
    sb = _SB()
    sb.post("edits", {"artifact_id": artifact_id, "before_text": before, "after_text": after,
                      "notes": notes}, prefer="return=minimal")
    sb.post("memories", {"kind": "voice", "text": text, "tags": ["voice", "edit"],
                         "provenance": f"organiser edit of artifact {artifact_id}"},
            prefer="return=minimal")


def _sb_graph(limit: int) -> dict[str, Any]:
    sb = _SB()
    ents = sb.get("entities", {"select": "id,kind,name,times_used,attrs",
                               "order": "times_used.desc", "limit": str(limit)})
    nodes = [{"id": e["id"], "label": e["name"], "kind": e["kind"],
              "times_used": e.get("times_used") or 0, "attrs": e.get("attrs") or {}} for e in ents]
    valid = {n["id"] for n in nodes}
    rels = sb.get("relations", {"select": "source_id,target_id,relation", "limit": "1000"})
    edges = [{"source": r["source_id"], "target": r["target_id"], "relation": r["relation"]}
             for r in rels if r["source_id"] in valid and r["target_id"] in valid]
    return {"nodes": nodes, "edges": edges}


# ---------------------------------------------------------------- public API

def recall(spec: EventSpec, kinds: list[str] | None = None, limit: int = 12) -> list[Memory]:
    """Facts relevant to this event, ranked by relevance. Each carries .provenance."""
    if config.HAS_SUPABASE:
        try:
            return _sb_recall(spec, kinds, limit)
        except Exception:  # noqa: BLE001 - any failure degrades to local
            pass
    _fallback_note()
    return _local_recall(spec, kinds, limit)


def remember(event_id: str, facts: list[Memory], entities: list[Entity],
             relations: list[Relation]) -> None:
    """Write back what a run learned; upserts entities by (kind, name), bumping times_used."""
    if config.HAS_SUPABASE:
        try:
            _sb_remember(event_id, facts, entities, relations)
            return
        except Exception:  # noqa: BLE001
            pass
    _fallback_note()
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
            return
        except Exception:  # noqa: BLE001
            pass
    _fallback_note()
    _local_learn(artifact_id, before, after, notes, text)


def graph(limit: int = 150) -> dict:
    """Nodes + edges for the graph view; also written to out/graph.json."""
    result: dict[str, Any] | None = None
    if config.HAS_SUPABASE:
        try:
            result = _sb_graph(limit)
        except Exception:  # noqa: BLE001
            result = None
    if result is None:
        _fallback_note()
        result = _local_graph(limit)
    GRAPH_PATH.write_text(json.dumps(result, indent=1, ensure_ascii=False))
    return result
