"""Load seed/demo_extra.json into the knowledge base (Supabase if configured, else out/memory.json).

    .venv/bin/python seed/load_demo.py

Goes through memory.remember, so it is idempotent: re-running does not duplicate rows or
inflate times_used. Everything written is fictional and flagged attrs.seed = true.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eventautopilot import memory  # noqa: E402
from eventautopilot.models import Entity, Memory, Relation  # noqa: E402

SEED = {"seed": True}


def load(path: Path) -> None:
    data = json.loads(path.read_text())
    for ev in data["events"]:
        title, eid = ev["title"], ev["id"]
        event = f"event:{title}"
        ents = [Entity(kind="event", name=title, attrs={**ev["attrs"], **SEED})]
        rels: list[Relation] = []

        def link(kind: str, name: str, rel: str, attrs: dict | None = None, outward: bool = True) -> None:
            ents.append(Entity(kind=kind, name=name, attrs={**(attrs or {}), **SEED}))
            src, dst = (f"{kind}:{name}", event) if outward else (event, f"{kind}:{name}")
            rels.append(Relation(source=src, relation=rel, target=dst, event_id=eid))

        venue, how = ev["venue"]
        link("venue", venue, how)
        link("community", ev["community"], "organised")
        for name, rel in ev["suppliers"]:
            link("supplier", name, rel)
        for name, amount in ev["sponsors"]:
            link("sponsor", name, "sponsored", {"last_paid_gbp": amount})
        for name in ev["volunteers"]:
            link("volunteer", name, "ran_shift")

        facts = [Memory(kind=k, text=t, tags=[k, "seed"], provenance=title) for k, t in ev["facts"]]
        memory.remember(eid, facts, ents, rels)
        print(f"seeded {title}: {len(ents)} entities, {len(rels)} relations, {len(facts)} facts")
    print(f"backend: {memory.backend()}")


if __name__ == "__main__":
    load(Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).with_name("demo_extra.json"))
