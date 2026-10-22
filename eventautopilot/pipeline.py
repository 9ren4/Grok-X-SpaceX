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
import inspect
from datetime import datetime, timedelta
from typing import Any, Callable

from .config import OUT
from .models import (
    Artifacts, Entity, EventSpec, Memory, Relation, RunResult, Venue,
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


GENERIC_LABELS = [
    "food", "drinks", "venue hire", "av and equipment", "printing and signage",
    "contingency", "catering", "equipment", "signage", "snacks", "coffee",
]


def _is_supplier_name(label: str) -> bool:
    """True only for real supplier names (e.g. 'Dishoom'), not category words."""
    low = label.lower()
    words = re.findall(r"[a-z]+", low)
    return bool(words) and not all(w in FOOD_WORDS or w in {"and", "for", "the", "hire"} for w in words)


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


MON_RE = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
          r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
WD_RE = r"(?:monday|tuesday|tues|wednesday|thursday|thurs|friday|saturday|sunday)"
ATT_ALIASES = (r"people|pepole|peple|peeps|ppl|persons?|guests?|attendees?|pax|folks|"
               r"members|humans|heads")
DEFAULT_ATTENDEES = 50
MAX_ATTENDEES = 5000
ATTENDEE_FAIL = f"attendees (could not parse; defaulted to {DEFAULT_ATTENDEES})"
CURRENCIES = {"£": "GBP", "$": "USD", "€": "EUR"}


class PipelineError(Exception):
    """Bad input / unusable source; main() prints it and exits with exit_code."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _norm_input(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _hm(h: int, m: int, ap: str | None) -> str | None:
    if ap:
        if not 1 <= h <= 12:
            return None
        h = h % 12 + (12 if ap == "pm" else 0)
    elif h > 23:
        return None
    return f"{h:02d}:{m:02d}"


def _parse_times(low: str) -> tuple[str, str, int | None]:
    """(start, end, span_start) from '7pm to 10pm', '19:00-22:00', 'at 7pm'."""
    rng = re.search(
        r"(?<![\w:.£$€])(\d{1,2})(?::([0-5]\d))?\s*(am|pm)?\s*(?:to|until|till|-|–|—)\s*"
        r"(\d{1,2})(?::([0-5]\d))?\s*(am|pm)?(?![\w:])", low)
    if rng:
        h1, m1, a1, h2, m2, a2 = rng.groups()
        end_marked = m2 is not None or a2 is not None
        start_marked = m1 is not None or a1 is not None or a2 is not None
        if end_marked and start_marked:
            e = _hm(int(h2), int(m2 or 0), a2)
            ap1 = a1 or a2
            s = _hm(int(h1), int(m1 or 0), ap1)
            if s and e and a1 is None and a2 == "pm" and int(h1) <= 12 and s > e:
                s = _hm(int(h1), int(m1 or 0), "am")  # e.g. '11 to 2pm'
            if s and e:
                return s, e, rng.start()
    one = re.search(
        r"(?<![\w:.£$€])(?:(\d{1,2})(?::([0-5]\d))?\s*(am|pm)|([01]?\d|2[0-3]):([0-5]\d))(?![\w:])",
        low)
    if one:
        if one.group(1):
            s = _hm(int(one.group(1)), int(one.group(2) or 0), one.group(3))
        else:
            s = _hm(int(one.group(4)), int(one.group(5)), None)
        if s:
            return s, "", one.start()
    return "", "", None


def _parse_attendees(low: str) -> tuple[int | None, int | None]:
    """(raw count or None if no mention, span_start). Count may be out of range."""
    pats = [
        rf"(?<![\w.£$€])(\d{{1,3}}(?:,\d{{3}})+|\d+)\s*(?:\+\s*|-\s*)?(?:{ATT_ALIASES})\b",
        r"(?<![\w.£$€])(\d+)\s*人",
        rf"\bfor\s+~?\s*(\d+)\b(?!\s*(?:hours?|hrs?|h\b|mins?|minutes?|days?|%|pounds?|quid|"
        rf"{MON_RE}\b))",
    ]
    for p in pats:
        m = re.search(p, low)
        if m:
            return int(m.group(1).replace(",", "")), m.start()
    return None, None


def _parse_date(low: str, now: datetime, start: str
                ) -> tuple[datetime | None, str, bool, int | None]:
    """(date, note, mentioned, span_start). mentioned=True but date None means unusable."""
    hh, mm = (int(x) for x in (start or "18:30").split(":"))
    today = now.date()
    mon = None
    m = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({MON_RE})\b(?:,?\s+(\d{{4}})\b)?", low)
    if m:
        day, mon, year = int(m.group(1)), m.group(2), m.group(3)
    else:
        m = re.search(
            rf"\b({MON_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?!\s*(?::|am\b|pm\b|\d|{ATT_ALIASES}))"
            r"(?:,?\s+(\d{4})\b)?", low)
        if m:
            mon, day, year = m.group(1), int(m.group(2)), m.group(3)
    if m and mon:
        idx = MONTHS.index(mon[:3]) + 1
        try:
            if year:
                d = datetime(int(year), idx, day, hh, mm)
                if d.date() < today:
                    return None, f"date {d.date()} is in the past; not used", True, m.start()
                return d, "", True, m.start()
            d = datetime(today.year, idx, day, hh, mm)
            note = ""
            if d.date() < today:
                d = d.replace(year=today.year + 1)
                note = f"date already passed this year; rolled to next occurrence {d.date()}"
            return d, note, True, m.start()
        except ValueError:
            return None, f"invalid date {day} {mon}", True, m.start()
    m = re.search(r"\b(tomorrow|tonight|today)\b", low)
    if m:
        d = datetime.combine(today, datetime.min.time()).replace(hour=hh, minute=mm)
        if m.group(1) == "tomorrow":
            d += timedelta(days=1)
        return d, "", True, m.start()
    m = re.search(rf"\b(?:(next|this|on)\s+)?({WD_RE})\b", low)
    if m:
        wd = next(i for i, w in enumerate(WEEKDAYS) if w.startswith(m.group(2)[:3]))
        delta = (wd - today.weekday()) % 7
        if delta == 0 and m.group(1) == "next":
            delta = 7
        d = datetime.combine(today, datetime.min.time()).replace(hour=hh, minute=mm)
        return d + timedelta(days=delta), "", True, m.start()
    return None, "", False, None


def _parse_venue(text: str) -> tuple[str, int | None]:
    for m in re.finditer(r"(?:\bat\s+|@\s*)(?![\s\d]|noon\b|midnight\b)([^,;.\n]+)", text, re.I):
        cand = m.group(1)
        cut = re.search(r"\s+(?:(?:on|for|with|from|next|this|tomorrow|tonight|today|around|"
                        r"about|starting|until|till|to)\b|\d|[£$€~])", cand, re.I)
        if cut:
            cand = cand[:cut.start()]
        cand = " ".join(cand.split()[:6]).strip()
        if cand:
            return cand, m.start()
    return "", None


def _parse_price(low: str) -> tuple[float | None, str, int | None]:
    m = re.search(r"([£$€])\s*(\d+(?:\.\d{1,2})?)", low)
    if m:
        return float(m.group(2)), CURRENCIES[m.group(1)], m.start()
    m = re.search(r"\bfree\b(?!\s+(?:pizza|food|drinks?|beer|wine|snacks|coffee|lunch|dinner|swag))", low)
    if m:
        return 0.0, "", m.start()
    return None, "", None


def parse_one_liner(text: str) -> EventSpec:
    """Heuristic parse; anything we cannot find (or could not parse) goes to spec.missing."""
    text = " ".join(text.split())
    low = text.lower()
    now = datetime.now()
    missing: list[str] = []
    spans: list[int] = []

    start, end, sp = _parse_times(low)
    if sp is not None:
        spans.append(sp)
    date, date_note, mentioned, sp = _parse_date(low, now, start)
    if sp is not None:
        pre = re.search(rf"(?:\b(?:next|this|on)\s+)?\b{WD_RE}\s+(?:the\s+)?$", low[:sp])
        spans.append(pre.start() if pre else sp)
    if date is None:
        missing.append("date" if not date_note else f"date ({date_note})")

    raw_n, sp = _parse_attendees(low)
    if sp is not None:
        spans.append(sp)
    n = 0
    if raw_n is None:
        missing.append("expected_attendees")
    elif 1 <= raw_n <= MAX_ATTENDEES:
        n = raw_n
    else:
        n = DEFAULT_ATTENDEES
        missing.append(ATTENDEE_FAIL)

    price, cur, sp = _parse_price(low)
    if sp is not None:
        spans.append(sp)
    venue, sp = _parse_venue(text)
    if sp is not None:
        spans.append(sp)

    first_sep = re.search(r",|;| - | – ", text)
    cut = min(spans + ([first_sep.start()] if first_sep else []) + [len(text)])
    if cut == 0 or len(text[:cut].strip(" ,;-–:")) < 3:  # token at the very start
        cut = first_sep.start() if first_sep else len(text)
    title = text[:cut].strip(" ,;-–:")
    while True:
        t2 = re.sub(r"\s+(?:on|at|for|with|in|the|a|from|and|to|@)$", "", title, flags=re.I)
        if t2 == title:
            break
        title = t2
    title = title or text
    desc = text[len(title):].strip(" ,;-–:") if text.startswith(title) else ""

    food = [w for w in FOOD_WORDS if re.search(rf"\b{w}\b", low)]
    fmt = next((f for f in FORMATS if f in low), "meetup")

    if not venue:
        missing.append("venue")
    missing.append("hosts")
    if not food:
        missing.append("food")

    spec = EventSpec(
        title=title,
        one_liner=text,
        description=desc,
        date=date,
        start=start or ("18:30" if date else ""),
        end=end,
        expected_attendees=n,
        format=fmt,  # type: ignore[arg-type]
        missing=missing,
        raw={"food": food, "date_note": date_note},
    )
    if venue:
        spec.venue = Venue(name=venue)
    if price is not None:
        spec.ticket_price = price
        if cur:
            spec.currency = cur
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:30] or "event"
    spec.id = f"{base}-{hashlib.sha1(_norm_input(text).encode()).hexdigest()[:8]}"
    return spec


def _ensure_id(spec: EventSpec) -> str:
    if not spec.id:
        base = re.sub(r"[^a-z0-9]+", "-", (spec.title or "event").lower()).strip("-")[:30]
        day = spec.date.date().isoformat() if spec.date else ""
        h = hashlib.sha1(f"{spec.title.strip().lower()}|{day}".encode()).hexdigest()[:6]
        spec.id = f"{base or 'event'}-{h}"
    return spec.id


def _safe_id(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", s)


# ---------------------------------------------------------------- learning


LEARNED_TAGS = ["learned", "run"]


def build_learnings(spec: EventSpec, degraded: bool = False, memories: list[Memory] = ()
                    ) -> tuple[list[Memory], list[Entity], list[Relation]]:
    """Facts, entities and relations this run teaches the knowledge base.

    Returns nothing when the run degraded; default estimates are never written.
    """
    if degraded:
        return [], [], []
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
            tags=["venue", v.name.lower(), *LEARNED_TAGS], provenance=title))

    seen: set[str] = set()
    for item in spec.budget:
        if not item.source.startswith("memory:"):
            continue  # defaults/estimates are not knowledge; never launder them
        facts.append(Memory(
            kind="budget",
            text=f"{item.label}: {item.qty:g} x {item.unit_cost:.2f} {item.currency} "
                 f"= {item.total:.2f} {item.currency}"
                 f"{' (' + item.note + ')' if item.note else ''}",
            tags=["budget", item.label.lower(), *LEARNED_TAGS], provenance=title))
        low = item.label.lower()
        if (low not in seen and not any(g in low for g in GENERIC_LABELS)
                and _is_supplier_name(item.label)):
            seen.add(low)
            entities.append(Entity(kind="supplier", name=item.label,
                                   attrs={"unit_cost": item.unit_cost, "currency": item.currency}))
            relations.append(Relation(source=f"event:{title}", relation="used_supplier",
                                      target=f"supplier:{item.label}", event_id=eid))

    # Edges to what this event actually reused, so the graph shows it latching on.
    for name in dict.fromkeys(s.volunteer for s in spec.rota if s.volunteer):
        relations.append(Relation(source=f"volunteer:{name}", relation="ran_shift",
                                  target=f"event:{title}", event_id=eid))
    for prov in dict.fromkeys(m.provenance for m in memories if m.provenance):
        if prov != title:
            relations.append(Relation(source=f"event:{title}", relation="learned_from",
                                      target=f"event:{prov}", event_id=eid))
    return facts, entities, relations


# ---------------------------------------------------------------- hours


def _visual_exists(spec: EventSpec) -> bool:
    pats = [f"*{_safe_id(spec.id)}*.png", f"*{_safe_id(spec.id)}*.jpg"]
    for d in (OUT, OUT / "visuals"):
        if d.is_dir() and any(next(d.glob(p), None) for p in pats):
            return True
    return False


def compute_hours(spec: EventSpec, art: Artifacts, has_visual: bool
                  ) -> tuple[float, dict[str, float]]:
    """Hours saved vs manual baseline; planning baselines scale with event size."""
    factor = max(0.6, min(2.0, 0.6 + (spec.expected_attendees or 0) / 100))
    scaled = {"run_sheet", "budget", "rota"}
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
    if art.newsletter_html.strip():
        generated.append("newsletter")
    if art.linkedin or art.whatsapp:
        generated.append("posts")
    breakdown: dict[str, float] = {}
    for k in generated:
        manual, ours = BASELINES[k]
        mins = (manual * (factor if k in scaled else 1.0)) - ours
        breakdown[k] = round(max(mins, 0) / 60, 2)
    return round(sum(breakdown.values()), 2), breakdown


def compute_memory_stats(spec: EventSpec, memories: list[Memory]) -> dict[str, Any]:
    """How much of the plan came from past events."""
    return {
        "budget_from_memory": sum(1 for i in spec.budget if i.source.startswith("memory:")),
        "budget_total": len(spec.budget),
        "rota_filled": sum(1 for sh in spec.rota
                           if sh.volunteer.strip() and sh.volunteer.strip().upper() != "TBC"),
        "rota_total": len(spec.rota),
        "memories_recalled": len(memories),
    }


# ---------------------------------------------------------------- run


def run(source: str, verbose: bool = False) -> RunResult:
    """Run the whole flow for a Luma link/slug or a one-line description."""
    log = _Log(echo=verbose)
    source = (source or "").strip()
    if not source:
        raise PipelineError("usage: python -m eventautopilot.pipeline <luma-url | one-liner> "
                            "(input was empty)", exit_code=2)
    degraded = False
    log.add(f"start: {source[:80]!r}")

    # 1. fetch / parse
    t = time.perf_counter()
    spec: EventSpec | None = None
    if _is_link(source):
        luma = _load("luma", log)
        if luma:
            not_found = getattr(luma, "LumaNotFound", None)
            try:
                spec = luma.fetch_event(source)
            except Exception as e:  # noqa: BLE001
                if not_found and isinstance(e, not_found):
                    raise PipelineError(f"Luma event not found: {source}", exit_code=1) from e
                log.add(f"luma: FAILED, degraded ({type(e).__name__}: {e})", since=t)
        if spec is not None:
            log.add(f"luma: fetched {spec.title!r}", since=t)
        else:
            spec = EventSpec(title=source.rstrip("/").split("/")[-1], luma_url=source,
                             missing=["everything (luma fetch failed)"])
            degraded = True
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
    vols: list[dict] | None = None
    try:
        from eventautopilot import memory as _mem
        vols = _mem.volunteers()
    except (AttributeError, ImportError):
        vols = None
    except Exception as e:  # noqa: BLE001
        log.add(f"memory.volunteers: FAILED ({type(e).__name__}: {e})")
    log.add(f"recall: {len(memories)} memories "
            f"({', '.join(sorted({m.provenance for m in memories if m.provenance})) or 'none'})",
            since=t)

    # 3. plan
    t = time.perf_counter()
    planner = _load("planner", log)
    if planner:
        kw: dict[str, Any] = {}
        try:
            if "volunteers" in inspect.signature(planner.plan).parameters:
                kw["volunteers"] = vols
        except (TypeError, ValueError):
            pass
        planned = _step(log, "planner.plan", lambda: planner.plan(spec, memories, **kw), None)
        if planned is not None:
            spec = planned
            _ensure_id(spec)
            log.add(f"plan: {len(spec.run_sheet)} slots, {len(spec.budget)} budget lines "
                    f"({spec.budget_total:.2f} {spec.currency}), {len(spec.rota)} shifts", since=t)
        else:
            degraded = True
            log.add("plan: skipped (degraded)", since=t)
    else:
        degraded = True

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
    facts, entities, relations = build_learnings(spec, degraded=degraded, memories=memories)
    if degraded:
        log.add("degraded: nothing learned", since=t)
    elif memory:
        ok = _step(log, "memory.remember",
                   lambda: (memory.remember(spec.id, facts, entities, relations), True)[1], False)
        if ok:
            learned = len(facts)
            log.add(f"remember: {len(facts)} facts, {len(entities)} entities, "
                    f"{len(relations)} relations [{backend}]", since=t)
    else:
        log.add("remember: skipped (no memory module)", since=t)

    # 6. visual (rendered before hours saved so it counts)
    visual_path = None
    visuals = _load("visuals", log)
    if visuals:
        t = time.perf_counter()
        visual_path = _step(log, "visuals.render",
                            lambda: visuals.render(RunResult(spec=spec, artifacts=artifacts)), None)
        if visual_path:
            log.add(f"visual: {visual_path}", since=t)

    # 7. hours saved
    has_visual = _visual_exists(spec)
    hours, breakdown = compute_hours(spec, artifacts, has_visual)
    log.add(f"hours saved: {hours} h from {breakdown}")
    stats = compute_memory_stats(spec, memories)
    if spec.raw.get("date_note"):
        log.add(f"note: {spec.raw['date_note']}")

    result = RunResult(spec=spec, artifacts=artifacts, memories_used=memories,
                       learned=learned, hours_saved=hours,
                       hours_breakdown=breakdown, memory_stats=stats)

    # 8. write JSON, then publish (visual upload, Ghost draft, Notion page) and the graph
    path = OUT / f"run_{_safe_id(spec.id)}.json"

    def write() -> None:
        result.log = list(log.lines)
        path.write_text(result.model_dump_json(indent=2), encoding="utf-8")

    log.add(f"wrote {path}")
    write()

    face = _load("face", log)
    if face:
        t = time.perf_counter()
        pub = _step(log, "face.publish",
                    lambda: face.publish(result, visual_path=visual_path), None) or {}
        for key in ("visual_url", "ghost_url", "notion_url"):
            if pub.get(key):
                log.add(f"{key.removesuffix('_url')}: {pub[key]}")
        log.add("publish: done", since=t)

    if memory:
        t = time.perf_counter()
        g = _step(log, "memory.graph", lambda: memory.graph(highlight_event=spec.id), None)
        if g is not None:
            log.add(f"graph: {len(g.get('nodes', []))} nodes, {len(g.get('edges', []))} edges",
                    since=t)
    write()
    return result


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: python -m eventautopilot.pipeline <luma-url | one-liner>", file=sys.stderr)
        return 2
    source = " ".join(args)
    try:
        result = run(source, verbose=True)
    except PipelineError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.exit_code
    path = OUT / f"run_{_safe_id(result.spec.id)}.json"
    spec = result.spec
    print(f"\n{spec.title}: {result.hours_saved} h est. hours saved vs manual baseline, "
          f"{result.learned} facts learned")
    if result.hours_breakdown:
        print("  est. hours saved vs manual baseline: " + ", ".join(
            f"{k} {v}" for k, v in result.hours_breakdown.items()))
    st = result.memory_stats
    if st:
        print(f"  {st['budget_from_memory']}/{st['budget_total']} budget lines and "
              f"{st['rota_filled']}/{st['rota_total']} shifts came from past events "
              f"({st['memories_recalled']} memories recalled)")
    if spec.budget:
        w = max(len(i.label) for i in spec.budget)
        print(f"  {'budget line'.ljust(w)}  {'total':>10}  source")
        for i in spec.budget:
            print(f"  {i.label.ljust(w)}  {i.total:>10.2f}  {i.source or 'default estimate'}")
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
