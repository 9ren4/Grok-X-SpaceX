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
        ("Talk", 50, "Host"),
        ("Panel and Q&A", 40, "Host"),
        ("Networking", 40, "Floor"),
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
LONG_WINDOW_MIN = 300  # windows longer than this use the drop-in template
MAX_SLOT_MIN = 90


def _split_even(total: int, k: int) -> list[int]:
    """Split `total` minutes into k parts in 5-minute units; remainder goes to the last."""
    units = total // 5
    parts = [(units // k + (1 if i < units % k else 0)) * 5 for i in range(k)]
    parts[-1] += total - sum(parts)
    return parts


def _drop_in_plan(window: int) -> list[tuple[str, int, str]]:
    """Doors, themed work/social blocks (<=~90 min), lunch, open floor, close."""
    doors, close, lunch = 30, 15, 60
    body = window - doors - close - lunch
    pre = (body // 2) // 5 * 5
    post = body - pre
    out: list[tuple[str, int, str]] = [("Doors open", doors, "Check-in")]
    for total, themes in ((pre, ["Quiet work block", "Open session", "Open floor"]),
                          (post, ["Focus work block", "Lounge and community", "Open floor"])):
        k = max(1, math.ceil(total / MAX_SLOT_MIN))
        for i, d in enumerate(_split_even(total, k)):
            name = themes[i] if i < len(themes) else "Open floor"
            out.append((name, d, "Host" if name == "Open session" else "Floor"))
        if themes[0].startswith("Quiet"):
            out.append(("Lunch break", lunch, "Floor"))
    out.append(("Close and thank-yous", close, "Host"))
    return out


def _build_run_sheet(spec: EventSpec) -> tuple[list[Slot], int, int]:
    start = _parse_time(spec.start) if spec.start else _parse_time(DEFAULT_START)
    end = _parse_time(spec.end) if spec.end else start + 180
    if end <= start:
        end += 24 * 60
    window = end - start
    fmt = spec.format if spec.format in _TEMPLATES else "meetup"
    if window > LONG_WINDOW_MIN and fmt != "hackathon":
        setup = 30 if (spec.expected_attendees or DEFAULT_ATTENDEES) <= 25 else 60
        slots = [Slot(time=_fmt(start - setup), duration_min=setup,
                      item="Setup (before doors)", owner="Setup crew")]
        t = start
        for item, d, owner in _drop_in_plan(window):
            slots.append(Slot(time=_fmt(t), duration_min=d, item=item, owner=owner))
            t += d
        return slots, start, t
    tpl = _TEMPLATES[fmt]
    total_w = sum(w for _, w, _ in tpl)
    floor_ = 10 if window >= 90 else 5
    durs = [max(floor_, int(round(w * window / total_w / 5.0)) * 5) for _, w, _ in tpl]
    # Absorb rounding drift in the largest slot so slots tile the window exactly.
    drift = window - sum(durs)
    big = max(range(len(durs)), key=lambda i: durs[i])
    durs[big] = max(5, durs[big] + drift)
    slots = []
    t = start
    for (item, _, owner), d in zip(tpl, durs):
        slots.append(Slot(time=_fmt(t), duration_min=d, item=item, owner=owner))
        t += d
    return slots, start, start + sum(durs)


# ----------------------------------------------------------------------- budget
_CATS: list[tuple[str, tuple[str, ...]]] = [
    ("venue", ("venue hire", "room hire", "space hire", "hire fee", "hire cost")),
    ("drinks", ("drink", "beer", "soft", "wine", "bar tab", "refreshment")),
    ("av", ("projector", "microphone", " mic", "sound", " av ", "av hire", "a/v")),
    ("print", ("print", "signage", "banner", "badge", "poster")),
    ("food", ("pizza", "food", "catering", "sandwich", "snack", "lunch", "dinner")),
]
_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€"}
_CUR_WORDS = {"£": "GBP", "gbp": "GBP", "pounds": "GBP", "$": "USD", "usd": "USD",
              "dollars": "USD", "€": "EUR", "eur": "EUR", "euros": "EUR"}
_NUM = r"\d[\d,]*(?:\.\d+)?"
_MONEY = re.compile(
    rf"(?P<sym>[£$€]|\bgbp\b|\busd\b|\beur\b)\s*(?P<a>{_NUM})"
    rf"|(?P<b>{_NUM})\s*(?P<suf>gbp|pounds|usd|eur|euros|dollars)\b", re.I)
_PEOPLE_WORDS = r"(?:people|persons?|attendees|guests|heads|ppl|pax|members|participants)\b"
# "120 attendees", "120 hackathon attendees", "60 person"; never "per person" or a price.
_PEOPLE = re.compile(
    rf"(?<![£$€\d.,])(\d+)\s+(?:(?!per\b|pizzas?\b)[A-Za-z-]+\s+){{0,2}}?{_PEOPLE_WORDS}", re.I)
_PIZZAS = re.compile(r"(\d+)\s*(?:x\s*)?(?:large\s+)?pizzas?", re.I)
_PER_HEAD = re.compile(r"per\s*(?:head|person)|\bpp\b|a\s+head|/\s*head|each\s+attendee", re.I)
DEFAULT_PEOPLE_PER_PIZZA = 3.5
VENUE_DEFAULT_CAP = 3000.0
LARGE_EVENT = 1000

_FORMAT_KWS: dict[str, tuple[str, ...]] = {
    "meetup": ("meetup", "meet-up"),
    "workshop": ("workshop", "class", "masterclass"),
    "hackathon": ("hackathon", "hack day"),
    "social": ("social", "after dark", "mixer"),
    "talk": ("talk", "panel", "lecture", "keynote"),
}
_GENERIC_KWS = ("generic", "any event", "every event", "all events", "any format")
_NA_KWS = ("ceramic", "clay", "class", "pottery", "painting", "sculpt")
_FOOD_KWS = ("food", "pizza", "drink", "catering", "lunch", "dinner", "snack",
             "refreshment", "beer", "wine", "coffee", "bar ", "buffet")


def _money(seg: str) -> tuple[float, str] | None:
    """First money amount in `seg` as (amount, ISO currency)."""
    m = _MONEY.search(seg)
    if not m:
        return None
    raw = (m.group("a") or m.group("b")).replace(",", "").rstrip(".")
    cur = _CUR_WORDS[(m.group("sym") or m.group("suf")).lower()]
    return float(raw), cur


def _find_people(seg: str) -> int | None:
    m = _PEOPLE.search(seg)
    return int(m.group(1)) if m and int(m.group(1)) > 0 else None


def _segments(text: str) -> list[str]:
    return [s for s in re.split(r"(?<!\d)\.\s+|(?<=\D)\.(?=\s|$)|;|\n", text) if s.strip()]


_SKIP_WORDS = ("all-in", "all in", "typical", "total spend")
_DERIVED_ROW = re.compile(
    r"^\s*(?:venue hire|food|drinks|av(?: and equipment)?|printing(?: and signage)?|pizza|"
    r"contingency)\b[^:\n]*:.*(?:=|\bx\b)", re.I)


def _classify(seg: str) -> str | None:
    low = f" {seg.lower()} "
    if any(w in low for w in _SKIP_WORDS) or _DERIVED_ROW.match(seg):
        return None
    for cat, kws in _CATS:
        if any(k in low for k in kws):
            return cat
    return None


def _mem_tier(mem: Memory, fmt: str) -> int:
    """2 = mentions the event's format; 1 = generic/neutral; 0 = a different format."""
    blob = f"{mem.provenance} {' '.join(mem.tags)} {mem.text}".lower()
    mentioned = {f for f, kws in _FORMAT_KWS.items() if any(k in blob for k in kws)}
    if fmt in mentioned:
        return 2
    if not mentioned or any(g in blob for g in _GENERIC_KWS):
        return 1
    return 0


def _fact_cost(f: dict, n: int) -> float:
    return f["per_head"] * n if "per_head" in f else f["unit"] * f["qty"]


def _from_memories(memories: list[Memory], n: int, fmt: str = "meetup") -> dict[str, dict]:
    """Per-category price facts: {cat: {unit, qty | per_head, note, source, currency}}.

    Candidates are weighted by format relevance; the median-cost candidate of the best
    non-empty tier wins.
    """
    cands: dict[str, list[tuple[int, dict]]] = {}
    for mem in sorted(memories, key=lambda m: -m.score):
        if mem.kind not in ("budget", "venue", "supplier", "lesson"):
            continue
        tier = _mem_tier(mem, fmt)
        for seg in _segments(mem.text):
            got = _money(seg)
            cat = _classify(seg)
            if got is None or cat is None:
                continue
            amount, mcur = got
            sym = _SYMBOLS[mcur]
            people = _find_people(seg)
            pizzas = _PIZZAS.search(seg)
            fact: dict = {"source": f"memory: {mem.provenance or 'past event'}",
                          "currency": mcur}
            if _PER_HEAD.search(seg):
                fact.update(per_head=amount, note=f"{amount:.2f} per head (from memory)")
            elif pizzas and int(pizzas.group(1)) > 0:
                k = int(pizzas.group(1))
                if people:
                    ratio, how = k / people, f"{k} pizzas for {people} people"
                else:
                    ratio = 1 / DEFAULT_PEOPLE_PER_PIZZA
                    how = (f"attendee count not stated in memory; assumed 1 pizza "
                           f"per {DEFAULT_PEOPLE_PER_PIZZA:g} people")
                fact.update(unit=amount / k, qty=max(1, math.ceil(n * ratio)), label="Pizza",
                            note=f"{sym}{amount:g} for {how}; scaled to {n} attendees")
            elif people and cat in ("food", "drinks"):
                fact.update(per_head=amount / people,
                            note=f"{sym}{amount:g} for {people} people scaled to {n}")
            else:
                fact.update(unit=amount, qty=1, note=f"flat {sym}{amount:g} (from memory)")
            cands.setdefault(cat, []).append((tier, fact))
    found: dict[str, dict] = {}
    for cat, lst in cands.items():
        pool = [f for t, f in lst if t == 2] or [f for t, f in lst if t == 1] \
            or [f for _, f in lst]
        pool.sort(key=lambda f: _fact_cost(f, n))
        pick = dict(pool[(len(pool) - 1) // 2])
        if len(pool) > 1:
            pick["note"] += f" (median of {len(pool)} memories)"
        found[cat] = pick
    return found


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _build_budget(spec: EventSpec, memories: list[Memory], n: int,
                  missing: list[str]) -> list[LineItem]:
    facts = _from_memories(memories, n, spec.format)
    ev_cur = (spec.currency or "GBP").upper()
    cur_note = ""
    if ev_cur in _SYMBOLS:
        cur = ev_cur
    else:
        cur, cur_note = "GBP", f" (estimated in GBP; event currency is {spec.currency})"
    sym = _SYMBOLS[cur]
    small = n <= 25
    venue = (60.0 if small else round(_clamp(150.0 if n <= 100 else n * 1.5, 150.0,
                                               VENUE_DEFAULT_CAP)))
    av = 25.0 if n <= 25 else 80.0 if n <= 100 else 250.0 if n <= 500 else 600.0
    printing = 15.0 if small else round(_clamp(n * 0.5, 40.0, 400.0))
    food_pp, drink_pp = 6.0, (1.5 if small else 3.0)
    defaults = {
        "venue": ("Venue hire", "flat", venue, f"Default venue allowance{' (small group)' if small else ''}"),
        "food": ("Food", "head", food_pp, f"Default {sym}{food_pp:g} per head"),
        "drinks": ("Drinks", "head", drink_pp, f"Default {sym}{drink_pp:g} per head"),
        "av": ("AV and equipment", "flat", av, f"Default AV allowance for ~{n} attendees"),
        "print": ("Printing and signage", "flat", printing,
                  f"Default allowance (~{sym}0.50 per head, min/max applied)"),
    }
    blob = f"{spec.title} {spec.one_liner} {spec.description}".lower()
    optional_food = (spec.format in ("workshop", "talk")
                     and any(k in blob for k in _NA_KWS)
                     and not any(k in blob for k in _FOOD_KWS))
    items: list[LineItem] = []
    for cat, (label, mode, price, note) in defaults.items():
        f = facts.get(cat)
        if f:
            if "per_head" in f:
                qty, unit = n, round(f["per_head"], 2)
            else:
                qty, unit = f["qty"], round(f["unit"], 2)
            line_cur, fnote, src = f["currency"], f["note"], f["source"]
            if line_cur != ev_cur:
                fnote += f"; {line_cur} figure from memory; event currency is {spec.currency} — confirm"
            label = f.get("label", label)
        else:
            qty, unit = (n if mode == "head" else 1), price
            line_cur, fnote, src = cur, note + cur_note, DEFAULT_SOURCE
            if cat == "venue":
                missing.append("venue cost")
                fnote = ("Default venue allowance; confirm hire cost" + cur_note)
                if n > LARGE_EVENT:
                    missing.append("venue (large event: confirm hire cost)")
        if optional_food and cat in ("food", "drinks"):
            fnote += " — optional — only if provided"
        items.append(LineItem(label=label, qty=qty, unit_cost=unit, currency=line_cur,
                              note=fnote, source=src))
    base = [i for i in items if i.currency == cur]
    subtotal = round(sum(i.total for i in base), 2)
    cnote = "10% of subtotal"
    if len(base) != len(items):
        cnote += f" (lines in {cur} only; other-currency lines excluded)"
    items.append(LineItem(label="Contingency (10%)", qty=1, unit_cost=round(subtotal * 0.10, 2),
                          currency=cur, note=cnote, source=DEFAULT_SOURCE))
    return items


# ------------------------------------------------------------------------- rota
_STOP = {
    "volunteer", "volunteers", "rota", "check", "check-in", "checkin", "setup", "set", "up",
    "teardown", "photo", "photos", "photographer", "floor", "host", "the", "and", "event",
    "meetup", "london", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "reliable", "helped", "helps", "always", "usually", "also", "this", "last",
    "pizza", "ai", "in", "at", "on", "for", "with", "helper", "helpers", "regular",
    "plan", "ask", "first", "order", "typical", "sponsors", "sponsor", "expect", "book",
    "send", "use", "keep", "door", "doors", "lead", "when", "if", "we", "our", "every",
    "never", "after", "before", "about", "please", "remember", "make", "get", "all", "each",
    "one", "two", "most", "some", "any", "bring", "confirm", "team", "crew", "speaker",
    "speakers", "venue", "food", "drinks", "budget", "cost", "tip", "note", "lesson",
}
_NAME = re.compile(r"\b([A-Z][a-z]+\s+[A-Z][a-z]+)\b")


def _is_name(s: str) -> bool:
    words = s.strip().split()
    return bool(words) and all(w.lower().strip("-") not in _STOP for w in words)


def _volunteer_names(memories: list[Memory]) -> list[str]:
    """Person-like names only: tags, or 'Capitalised First Last' in text."""
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


def _role_keys(text: str) -> set[str]:
    low = (text or "").lower()
    keys: set[str] = set()
    if any(k in low for k in ("door", "check", "regist", "welcome desk")):
        keys.add("checkin")
    if any(k in low for k in ("setup", "set up", "set-up", "logistics")):
        keys.add("setup")
    if "photo" in low:
        keys.add("photo")
    if any(k in low for k in ("teardown", "tear down", "tear-down", "pack")):
        keys.add("teardown")
    if any(k in low for k in ("host", "floor", "mc", "emcee")):
        keys.add("floor")
    return keys


def volunteers_needed(n: int) -> int:
    """Headcount rule of thumb (matches seed memory): attendees // 20 + 2."""
    return n // 20 + 2


def _build_rota(volunteers: list[dict], start: int, end: int, n: int,
                long_day: bool = False) -> list[Shift]:
    """volunteers: [{'name', 'usual_role'}]; empty usual_role means 'any shift'."""
    setup_len = 30 if n <= 25 else 60
    spans: list[tuple[str, str, int, int]] = [  # (label, key, start, end)
        ("Setup", "setup", start - setup_len, start),
        ("Check-in 1", "checkin", start, start + 60),
    ]
    if n > 25:
        spans.append(("Check-in 2", "checkin", start, start + 60))
    if long_day:
        mid = start + ((end - start) // 2) // 5 * 5
        spans += [("Floor / host (first half)", "floor", start, mid),
                  ("Floor / host (second half)", "floor", mid, end)]
    else:
        spans.append(("Floor / host", "floor", start, end))
    if n > 25:
        spans.append(("Photo", "photo", start, end))
    spans.append(("Teardown (15 min after close)", "teardown", end, end + 15))

    pool = [(v["name"], _role_keys(v.get("usual_role", ""))) for v in volunteers]
    load: dict[str, list[tuple[int, int]]] = {name: [] for name, _ in pool}

    def free(name: str, s: int, e: int) -> bool:
        busy = load[name]
        return len(busy) < 2 and all(e <= bs or s >= be for bs, be in busy)

    rota: list[Shift] = []
    for label, key, s, e in spans:
        pick = ""
        for name, keys in pool:  # exact role match first
            if key in keys and free(name, s, e):
                pick = name
                break
        if not pick:  # then people with no stated/recognised role
            for name, keys in pool:
                if not keys and free(name, s, e):
                    pick = name
                    break
        if pick:
            load[pick].append((s, e))
        rota.append(Shift(role=label, volunteer=pick or "TBC", start=_fmt(s), end=_fmt(e)))
    return rota


# ------------------------------------------------------------------------- main
def plan(spec: EventSpec, memories: list[Memory],
         volunteers: list[dict] | None = None) -> EventSpec:
    """Return a copy of `spec` with run_sheet, budget and rota filled in.

    `volunteers` is an optional list of {'name', 'usual_role'} dicts (from the knowledge
    base); when given it replaces names mined from memory text.
    """
    out = spec.model_copy(deep=True)
    memories = memories or []
    out.run_sheet, start, end = _build_run_sheet(out)
    if not spec.start:
        _add_missing(out, "start time (defaulted to 18:30)")
    if not spec.end:
        _add_missing(out, "end time (defaulted to start + 3h)")
    n = out.expected_attendees or 0
    if n <= 0:
        n = DEFAULT_ATTENDEES
        out.expected_attendees = n
        if not any("attendee" in m.lower() for m in out.missing):
            out.missing.append("attendees (defaulted to 50)")
    missing: list[str] = []
    out.budget = _build_budget(out, memories, n, missing)
    for m in missing:
        _add_missing(out, m)
    if volunteers:
        pool = [{"name": v["name"], "usual_role": v.get("usual_role", "")}
                for v in volunteers if v.get("name")]
    else:
        pool = [{"name": nm, "usual_role": ""} for nm in _volunteer_names(memories)]
    long_day = (end - start) > LONG_WINDOW_MIN
    out.rota = _build_rota(pool, start, end, n, long_day)
    if any(s.volunteer == "TBC" for s in out.rota):
        _add_missing(out, f"volunteers (~{volunteers_needed(n)} needed; "
                          "unfilled rota shifts marked TBC)")
    return out


def _add_missing(spec: EventSpec, item: str) -> None:
    if item not in spec.missing:
        spec.missing.append(item)
