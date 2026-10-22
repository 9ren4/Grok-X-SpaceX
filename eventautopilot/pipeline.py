"""Orchestration: fetch -> recall -> plan -> copy -> remember -> hours saved -> JSON.

Sibling modules are imported lazily; a missing or failing step degrades the
result and adds a log line, it never crashes the run.
"""
from __future__ import annotations

import hashlib
import importlib
import re
import sys
import time
from datetime import datetime
from typing import Any, Callable

from .config import OUT
from .models import (
    Artifacts, Entity, EventSpec, Memory, Relation, RunResult,
)

# (manual minutes, minutes with us) -- docs/PROJECT.md section 9
BASELINES: dict[str, tuple[int, int]] = {
    "run_sheet": (45, 3),
    "budget": (30, 2),
    "rota": (60, 3),
    "page_copy": (30, 2),
    "visual": (30, 1),
    "newsletter": (60, 4),
    "posts": (30, 2),
}

FOOD_WORDS = [
    "pizza", "sandwiches", "sandwich", "burritos", "curry", "sushi", "tacos",
    "bagels", "snacks", "buffet", "catering", "coffee", "drinks", "beer",
    "wine", "vegan", "vegetarian", "lunch", "dinner", "breakfast", "cake",
]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
          "oct", "nov", "dec"]
FORMATS = ["hackathon", "workshop", "social", "talk", "meetup"]


class _Log:
    """Timed log that also echoes to stdout when verbose."""

    def __init__(self, echo: bool = False) -> None:
        self.t0 = time.perf_counter()
        self.lines: list[str] = []
        self.echo = echo

    def add(self, msg: str, since: float | None = None) -> None:
        now = time.perf_counter()
        stamp = f"[{now - self.t0:5.1f}s]"
        took = f" ({now - since:.2f}s)" if since is not None else ""
        line = f"{stamp} {msg}{took}"
        self.lines.append(line)
        if self.echo:
            print(line, flush=True)


def _load(name: str, log: _Log) -> Any | None:
    """Import eventautopilot.<name> lazily; None (and a log line) if unusable."""
    try:
        return importlib.import_module(f"eventautopilot.{name}")
    except ModuleNotFoundError as e:
        if e.name == f"eventautopilot.{name}":
            log.add(f"{name}: not available yet")
        else:
            log.add(f"{name}: import failed (missing dependency {e.name})")
    except Exception as e:  # noqa: BLE001
        log.add(f"{name}: import failed ({type(e).__name__}: {e})")
    return None


def _step(log: _Log, name: str, fn: Callable[[], Any], default: Any) -> Any:
    """Run fn, log timing; on any exception log it and return default."""
    t = time.perf_counter()
    try:
        out = fn()
        return out
    except Exception as e:  # noqa: BLE001
        log.add(f"{name}: FAILED, degraded ({type(e).__name__}: {e})", since=t)
        return default


# ---------------------------------------------------------------- input


def _is_link(source: str) -> bool:
    s = source.strip()
    if not s:
        return False
    if re.match(r"https?://", s, re.I) or re.search(r"(lu\.ma|luma\.com)/", s, re.I):
        return True
    return bool(re.fullmatch(r"[A-Za-z0-9_-]{5,32}", s))  # bare slug


def parse_one_liner(text: str) -> EventSpec:
    """Cheap heuristic parse; anything we cannot find goes to spec.missing."""
    text = text.strip()
    missing: list[str] = []
    low = text.lower()

    title = re.split(r",|;| - ", text, maxsplit=1)[0].strip() or text

    date = None
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(MONTHS) + r")[a-z]*\b", low)
    if not m:
        m2 = re.search(r"\b(" + "|".join(MONTHS) + r")[a-z]*\s+(\d{1,2})(?:st|nd|rd|th)?\b", low)
        if m2:
            mon, day = m2.group(1), int(m2.group(2))
        else:
            mon = day = None
    else:
        day, mon = int(m.group(1)), m.group(2)
    if mon and day:
        now = datetime.now()
        try:
            date = datetime(now.year, MONTHS.index(mon) + 1, day, 18, 30)
            if date.date() < now.date():
                date = date.replace(year=now.year + 1)
        except ValueError:
            date = None
    if date is None:
        missing.append("date")

    n = 0
    m = re.search(r"~?\s*(\d{1,4})\s*(?:\+\s*)?(?:people|ppl|attendees|guests|folks|humans)", low)
    if m:
        n = int(m.group(1))
    else:
        missing.append("expected_attendees")

    food = [w for w in FOOD_WORDS if re.search(rf"\b{w}\b", low)]
    fmt = next((f for f in FORMATS if f in low), "meetup")

    missing += ["venue", "hosts"]
    if not food:
        missing.append("food")

    return EventSpec(
        title=title,
        one_liner=text,
        description=text,
        date=date,
        start="18:30" if date else "",
        expected_attendees=n,
        format=fmt,  # type: ignore[arg-type]
        missing=missing,
        raw={"food": food},
    )


def _ensure_id(spec: EventSpec) -> str:
    if not spec.id:
        base = re.sub(r"[^a-z0-9]+", "-", (spec.title or "event").lower()).strip("-")[:30]
        h = hashlib.sha1((spec.luma_url + spec.title + str(time.time())).encode()).hexdigest()[:6]
        spec.id = f"{base or 'event'}-{h}"
    return spec.id


def _safe_id(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", s)


# ---------------------------------------------------------------- learning


def build_learnings(spec: EventSpec) -> tuple[list[Memory], list[Entity], list[Relation]]:
    """Facts, entities and relations this run teaches the knowledge base."""
    title = spec.title or spec.id
    eid = spec.id
    facts: list[Memory] = []
    entities: list[Entity] = [Entity(kind="event", name=title, attrs={
        "date": spec.date.isoformat() if spec.date else "",
        "attendees": spec.expected_attendees,
        "format": spec.format,
        "budget_total": spec.budget_total,
        "currency": spec.currency,
    })]
    relations: list[Relation] = []

    if spec.venue.name:
        v = spec.venue
        entities.append(Entity(kind="venue", name=v.name, attrs={
            k: val for k, val in {"address": v.address, "city": v.city,
                                  "capacity": v.capacity, "contact": v.contact}.items() if val}))
        relations.append(Relation(source=f"venue:{v.name}", relation="hosted",
                                  target=f"event:{title}", event_id=eid))
        facts.append(Memory(
            kind="venue",
            text=f"{v.name}{', ' + v.city if v.city else ''} hosted {title} "
                 f"with ~{spec.expected_attendees or '?'} people.",
            tags=["venue", v.name.lower()], provenance=title))

    seen: set[str] = set()
    for item in spec.budget:
        facts.append(Memory(
            kind="budget",
            text=f"{item.label}: {item.qty:g} x {item.unit_cost:.2f} {item.currency} "
                 f"= {item.total:.2f} {item.currency}"
                 f"{' (' + item.note + ')' if item.note else ''}",
            tags=["budget", item.label.lower()], provenance=title))
        low = item.label.lower()
        if any(re.search(rf"\b{w}\b", low) for w in FOOD_WORDS) and low not in seen:
            seen.add(low)
            entities.append(Entity(kind="supplier", name=item.label,
                                   attrs={"unit_cost": item.unit_cost, "currency": item.currency}))
            relations.append(Relation(source=f"event:{title}", relation="used_supplier",
                                      target=f"supplier:{item.label}", event_id=eid))
    return facts, entities, relations


# ---------------------------------------------------------------- hours


def _visual_exists(spec: EventSpec) -> bool:
    pats = [f"*{_safe_id(spec.id)}*.png", f"*{_safe_id(spec.id)}*.jpg"]
    for d in (OUT, OUT / "visuals"):
        if d.is_dir() and any(next(d.glob(p), None) for p in pats):
            return True
    return False


def compute_hours(spec: EventSpec, art: Artifacts, has_visual: bool) -> tuple[float, list[str]]:
    generated: list[str] = []
    if spec.run_sheet:
        generated.append("run_sheet")
    if spec.budget:
        generated.append("budget")
    if spec.rota:
        generated.append("rota")
    if art.page_copy:
        generated.append("page_copy")
    if has_visual:
        generated.append("visual")
    if art.newsletter_html or art.newsletter_subject:
        generated.append("newsletter")
    if art.linkedin or art.whatsapp:
        generated.append("posts")
    minutes = sum(BASELINES[k][0] - BASELINES[k][1] for k in generated)
    return round(minutes / 60, 2), generated


# ---------------------------------------------------------------- run


def run(source: str, verbose: bool = False) -> RunResult:
    """Run the whole flow for a Luma link/slug or a one-line description."""
    log = _Log(echo=verbose)
    source = (source or "").strip()
    log.add(f"start: {source[:80]!r}")

    # 1. fetch / parse
    t = time.perf_counter()
    spec: EventSpec | None = None
    if _is_link(source):
        luma = _load("luma", log)
        if luma:
            spec = _step(log, "luma", lambda: luma.fetch_event(source), None)
        if spec is not None:
            log.add(f"luma: fetched {spec.title!r}", since=t)
        else:
            spec = EventSpec(title=source.rstrip("/").split("/")[-1], luma_url=source,
                             missing=["everything (luma fetch failed)"])
            log.add("luma: degraded to bare spec", since=t)
    else:
        spec = parse_one_liner(source)
        log.add(f"one-liner: parsed {spec.title!r}, missing={spec.missing}", since=t)
    _ensure_id(spec)

    memory = _load("memory", log)
    backend = "none"
    if memory:
        backend = _step(log, "memory.backend", lambda: str(memory.backend()), "unknown")
    log.add(f"memory backend: {backend}")

    # 2. recall
    t = time.perf_counter()
    memories: list[Memory] = []
    if memory:
        memories = _step(log, "memory.recall", lambda: list(memory.recall(spec)), [])
    log.add(f"recall: {len(memories)} memories "
            f"({', '.join(sorted({m.provenance for m in memories if m.provenance})) or 'none'})",
            since=t)

    # 3. plan
    t = time.perf_counter()
    planner = _load("planner", log)
    if planner:
        planned = _step(log, "planner.plan", lambda: planner.plan(spec, memories), None)
        if planned is not None:
            spec = planned
            _ensure_id(spec)
            log.add(f"plan: {len(spec.run_sheet)} slots, {len(spec.budget)} budget lines "
                    f"({spec.budget_total:.2f} {spec.currency}), {len(spec.rota)} shifts", since=t)
        else:
            log.add("plan: skipped (degraded)", since=t)

    # 4. copy
    t = time.perf_counter()
    artifacts = Artifacts()
    tiers: dict[str, str] = {}
    copy_mod = _load("copy", log)
    if copy_mod:
        res = _step(log, "copy.generate", lambda: copy_mod.generate(spec, memories), None)
        if res is not None:
            artifacts, tiers = res[0], dict(res[1] or {})
            log.add(f"copy: tiers {tiers}", since=t)
        else:
            log.add("copy: skipped (degraded)", since=t)

    # 5. remember
    t = time.perf_counter()
    learned = 0
    facts, entities, relations = build_learnings(spec)
    if memory:
        ok = _step(log, "memory.remember",
                   lambda: (memory.remember(spec.id, facts, entities, relations), True)[1], False)
        if ok:
            learned = len(facts)
            log.add(f"remember: {len(facts)} facts, {len(entities)} entities, "
                    f"{len(relations)} relations [{backend}]", since=t)
    else:
        log.add("remember: skipped (no memory module)", since=t)

    # 6. hours saved
    has_visual = _visual_exists(spec)
    hours, items = compute_hours(spec, artifacts, has_visual)
    log.add(f"hours saved: {hours} h from {items}")

    result = RunResult(spec=spec, artifacts=artifacts, memories_used=memories,
                       learned=learned, hours_saved=hours)

    # 7. write JSON, then optional Notion push
    path = OUT / f"run_{_safe_id(spec.id)}.json"

    def write() -> None:
        result.log = list(log.lines)
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")

    log.add(f"wrote {path}")
    write()

    try:
        t = time.perf_counter()
        notion = importlib.import_module("eventautopilot.notion_sync")
        url = notion.push(result)
        log.add(f"notion: {url}", since=t)
    except ModuleNotFoundError as e:
        if e.name == "eventautopilot.notion_sync":
            log.add("notion_sync: not available yet")
        else:
            log.add(f"notion_sync: missing dependency {e.name}")
    except Exception as e:  # noqa: BLE001
        log.add(f"notion_sync: FAILED ({type(e).__name__}: {e})")
    write()
    return result


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: python -m eventautopilot.pipeline <luma-url | one-liner>")
        return 2
    source = " ".join(args)
    result = run(source, verbose=True)
    path = OUT / f"run_{_safe_id(result.spec.id)}.json"
    print(f"\n{result.spec.title}: {result.hours_saved} hours saved, "
          f"{result.learned} facts learned\n{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
