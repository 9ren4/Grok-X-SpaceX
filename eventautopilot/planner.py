"""WP3 planner: deterministic run sheet, budget and rota for an EventSpec.

No network or LLM calls. `refine_with_llm` is a reserved hook for a later tweak layer.
"""
from __future__ import annotations

import math
import re

from .models import EventSpec, LineItem, Memory, Shift, Slot

refine_with_llm = None  # future hook: Callable[[EventSpec, list[Memory]], EventSpec]

DEFAULT_START = "18:30"
DEFAULT_ATTENDEES = 50
DEFAULT_SOURCE = "default estimate"

# (item, weight, owner); weights are scaled to fill the event window.
_TEMPLATES: dict[str, list[tuple[str, int, str]]] = {
    "meetup": [
        ("Doors open and check-in", 30, "Check-in"),
        ("Welcome and housekeeping", 10, "Host"),
        ("Talk 1", 25, "Host"),
        ("Break and refills", 15, "Floor"),
        ("Talk 2", 25, "Host"),
        ("Q&A and announcements", 10, "Host"),
        ("Networking", 50, "Floor"),
        ("Close and thank-yous", 10, "Host"),
    ],
    "workshop": [
        ("Doors open and check-in", 20, "Check-in"),
        ("Welcome and goals", 10, "Host"),
        ("Intro and demo", 25, "Host"),
        ("Hands-on session 1", 40, "Host"),
        ("Break", 15, "Floor"),
        ("Hands-on session 2", 40, "Host"),
        ("Show and tell", 20, "Host"),
        ("Wrap-up and next steps", 10, "Host"),
    ],
    "hackathon": [
        ("Doors open and registration", 30, "Check-in"),
        ("Kick-off and rules", 15, "Host"),
        ("Team formation", 20, "Floor"),
        ("Hacking block 1", 90, "Floor"),
        ("Food break", 30, "Floor"),
        ("Hacking block 2", 90, "Floor"),
        ("Demos and judging", 40, "Host"),
        ("Awards and close", 15, "Host"),
    ],
    "social": [
        ("Doors open", 30, "Check-in"),
        ("Welcome drinks", 20, "Floor"),
        ("Icebreaker activity", 20, "Host"),
        ("Open socialising", 45, "Floor"),
        ("Food served", 30, "Floor"),
        ("Games or activity", 30, "Host"),
        ("Open socialising 2", 30, "Floor"),
        ("Close and thank-yous", 10, "Host"),
    ],
    "talk": [
        ("Doors open and seating", 25, "Check-in"),
        ("Welcome and introduction", 10, "Host"),
        ("Keynote talk", 40, "Host"),
        ("Q&A", 20, "Host"),
        ("Short break", 10, "Floor"),
        ("Panel or second talk", 30, "Host"),
        ("Networking", 30, "Floor"),
        ("Close", 10, "Host"),
    ],
}


# ---------------------------------------------------------------- time helpers
def _parse_time(s: str, default: str = DEFAULT_START) -> int:
    """Parse '18:30', '6:30pm', '6pm' to minutes since midnight."""
    m = re.search(r"(\d{1,2})(?:[:.](\d{2}))?\s*([ap]m)?", (s or "").lower())
    if not m:
        return _parse_time(default) if s != default else 18 * 60 + 30
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return (h % 24) * 60 + min(mi, 59)


def _fmt(minutes: int) -> str:
    minutes %= 24 * 60
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# -------------------------------------------------------------------- run sheet
def _build_run_sheet(spec: EventSpec) -> tuple[list[Slot], int, int]:
    start = _parse_time(spec.start) if spec.start else _parse_time(DEFAULT_START)
    end = _parse_time(spec.end) if spec.end else start + 180
    if end <= start:
        end += 24 * 60
    tpl = _TEMPLATES.get(spec.format, _TEMPLATES["meetup"])
    window = end - start
    total_w = sum(w for _, w, _ in tpl)
    durs = [max(5, int(round(w * window / total_w / 5.0)) * 5) for _, w, _ in tpl]
    # Absorb rounding drift in the largest slot so slots tile the window exactly.
    drift = window - sum(durs)
    big = max(range(len(durs)), key=lambda i: durs[i])
    durs[big] = max(5, durs[big] + drift)
    slots: list[Slot] = []
    t = start
    for (item, _, owner), d in zip(tpl, durs):
        slots.append(Slot(time=_fmt(t), duration_min=d, item=item, owner=owner))
        t += d
    return slots, start, start + sum(durs)


# ----------------------------------------------------------------------- budget
_CATS: list[tuple[str, tuple[str, ...]]] = [
    ("venue", ("venue", "room hire", "hire fee", "space hire")),
    ("drinks", ("drink", "beer", "soft", "wine", "bar tab", "refreshment")),
    ("av", ("projector", "microphone", " mic", "sound", " av ", "av hire", "a/v")),
    ("print", ("print", "signage", "banner", "badge", "poster")),
    ("food", ("pizza", "food", "catering", "sandwich", "snack", "lunch", "dinner")),
]
_MONEY = re.compile(r"(?:[£$€]|\bgbp\b)\s*(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s*(?:gbp|pounds)\b", re.I)
_PEOPLE = re.compile(r"(\d+)\s*(?:people|attendees|guests|heads|ppl|pax|members|participants)", re.I)
_PIZZAS = re.compile(r"(\d+)\s*(?:x\s*)?(?:large\s+)?pizzas?", re.I)
_PER_HEAD = re.compile(r"per\s*(?:head|person)|\bpp\b|a\s+head|/\s*head|each\s+attendee", re.I)


def _money(seg: str) -> float | None:
    m = _MONEY.search(seg)
    if not m:
        return None
    return float((m.group(1) or m.group(2)).replace(",", ""))


def _segments(text: str) -> list[str]:
    return [s for s in re.split(r"(?<!\d)\.\s+|(?<=\D)\.(?=\s|$)|;|\n", text) if s.strip()]


def _classify(seg: str) -> str | None:
    low = f" {seg.lower()} "
    for cat, kws in _CATS:
        if any(k in low for k in kws):
            return cat
    return None


def _from_memories(memories: list[Memory], n: int) -> dict[str, dict]:
    """Extract per-category price facts. Returns {cat: {unit, qty, per_head, note, source}}."""
    found: dict[str, dict] = {}
    ordered = sorted(memories, key=lambda m: -m.score)
    for mem in ordered:
        if mem.kind not in ("budget", "venue", "supplier", "lesson"):
            continue
        for seg in _segments(mem.text):
            amount = _money(seg)
            cat = _classify(seg)
            if amount is None or cat is None or cat in found:
                continue
            src = f"memory: {mem.provenance or 'past event'}"
            people = _PEOPLE.search(seg)
            pizzas = _PIZZAS.search(seg)
            fact: dict = {"source": src}
            if _PER_HEAD.search(seg):
                fact.update(per_head=amount, note=f"{amount:.2f} per head (from memory)")
            elif people and int(people.group(1)) > 0 and cat in ("food", "drinks"):
                k = int(people.group(1))
                fact.update(per_head=amount / k,
                            note=f"£{amount:g} for {k} people scaled to {n}")
            elif pizzas and int(pizzas.group(1)) > 0:
                k = int(pizzas.group(1))
                unit = amount / k
                per_head_pizzas = k / (int(people.group(1)) if people else 40)
                fact.update(unit=unit, qty=max(1, math.ceil(n * per_head_pizzas)),
                            label="Pizza",
                            note=f"£{amount:g} for {k} pizzas; scaled to {n} attendees")
            else:
                fact.update(unit=amount, qty=1, note=f"flat £{amount:g} (from memory)")
            found[cat] = fact
    return found


def _build_budget(spec: EventSpec, memories: list[Memory], n: int,
                  missing: list[str]) -> list[LineItem]:
    facts = _from_memories(memories, n)
    cur = spec.currency or "GBP"
    defaults = {
        "venue": ("Venue hire", "flat", 150.0, "Default venue allowance"),
        "food": ("Food", "head", 6.0, "Default £6 per head"),
        "drinks": ("Drinks", "head", 3.0, "Default £3 per head"),
        "av": ("AV and equipment", "flat", 80.0, "Default AV allowance"),
        "print": ("Printing and signage", "flat", 40.0, "Default allowance"),
    }
    items: list[LineItem] = []
    for cat, (label, mode, price, note) in defaults.items():
        f = facts.get(cat)
        if f:
            if "per_head" in f:
                qty, unit = n, round(f["per_head"], 2)
            else:
                qty, unit = f["qty"], round(f["unit"], 2)
            items.append(LineItem(label=f.get("label", label), qty=qty, unit_cost=unit,
                                  currency=cur, note=f["note"], source=f["source"]))
        else:
            if cat == "venue":
                missing.append("venue cost")
                note = "Default venue allowance; confirm hire cost"
            items.append(LineItem(label=label, qty=n if mode == "head" else 1,
                                  unit_cost=price, currency=cur, note=note,
                                  source=DEFAULT_SOURCE))
    subtotal = round(sum(i.total for i in items), 2)
    items.append(LineItem(label="Contingency (10%)", qty=1, unit_cost=round(subtotal * 0.10, 2),
                          currency=cur, note="10% of subtotal", source=DEFAULT_SOURCE))
    return items


# ------------------------------------------------------------------------- rota
_STOP = {
    "volunteer", "volunteers", "rota", "check", "check-in", "checkin", "setup", "set", "up",
    "teardown", "photo", "photos", "photographer", "floor", "host", "the", "and", "event",
    "meetup", "london", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "reliable", "helped", "helps", "always", "usually", "also", "this", "last",
    "pizza", "ai", "in", "at", "on", "for", "with", "helper", "helpers", "regular",
}
_NAME = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\b")


def _is_name(s: str) -> bool:
    words = s.strip().split()
    return bool(words) and all(w.lower().strip("-") not in _STOP for w in words)


def _volunteer_names(memories: list[Memory]) -> list[str]:
    names: list[str] = []

    def add(n: str) -> None:
        n = n.strip()
        if n and _is_name(n) and n.lower() not in {x.lower() for x in names}:
            names.append(n)

    for mem in sorted(memories, key=lambda m: -m.score):
        if mem.kind != "volunteer":
            continue
        for tag in mem.tags:
            t = tag.strip().replace("_", " ")
            if t and t[0].isalpha() and (t[0].isupper() or " " in t) and len(t) > 1:
                add(t.title() if t.islower() else t)
        for m in _NAME.finditer(mem.text):
            add(m.group(1))
    return names


def _build_rota(volunteers: list[str], start: int, end: int) -> list[Shift]:
    spans = [
        ("Setup", start - 60, start),
        ("Check-in 1", start, start + 60),
        ("Check-in 2", start, start + 60),
        ("Floor / host", start, end),
        ("Photo", start, end),
        ("Teardown", end - 15, end + 30),
    ]
    load: dict[str, list[tuple[int, int]]] = {v: [] for v in volunteers}
    rota: list[Shift] = []
    for role, s, e in spans:
        pick = ""
        for v in volunteers:
            busy = load[v]
            if len(busy) < 2 and all(e <= bs or s >= be for bs, be in busy):
                pick = v
                break
        if pick:
            load[pick].append((s, e))
        rota.append(Shift(role=role, volunteer=pick or "TBC", start=_fmt(s), end=_fmt(e)))
    return rota


# ------------------------------------------------------------------------- main
def plan(spec: EventSpec, memories: list[Memory]) -> EventSpec:
    """Return a copy of `spec` with run_sheet, budget and rota filled in."""
    out = spec.model_copy(deep=True)
    memories = memories or []
    out.run_sheet, start, end = _build_run_sheet(out)
    if not spec.start:
        _add_missing(out, "start time (defaulted to 18:30)")
    if not spec.end:
        _add_missing(out, "end time (defaulted to start + 3h)")
    n = out.expected_attendees
    if n <= 0:
        n = DEFAULT_ATTENDEES
        out.expected_attendees = n
        _add_missing(out, "attendees (defaulted to 50)")
    missing: list[str] = []
    out.budget = _build_budget(out, memories, n, missing)
    for m in missing:
        _add_missing(out, m)
    names = _volunteer_names(memories)
    out.rota = _build_rota(names, start, end)
    if any(s.volunteer == "TBC" for s in out.rota):
        _add_missing(out, "volunteers (unfilled rota shifts marked TBC)")
    return out


def _add_missing(spec: EventSpec, item: str) -> None:
    if item not in spec.missing:
        spec.missing.append(item)
