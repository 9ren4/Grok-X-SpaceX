"""Intake interview: Grok asks the organiser about each section before anything is planned.

Order: basics → venue → food → sponsorship → volunteers → budget → posts. One question at a
time; "skip" / "don't know" records nothing. The Brief holds only what the organiser said, and
the pipeline applies it on top of the plan, so answers win and nothing is invented.
"""
from __future__ import annotations

import re

import httpx
from pydantic import BaseModel, Field

from . import llm
from .models import EventSpec, LineItem

TOPICS = ["basics", "venue", "food", "sponsorship", "volunteers", "budget", "posts"]
YOU = "you told us"


class Sponsor(BaseModel):
    name: str
    amount: float | None = None
    confirmed: bool = False


class Brief(BaseModel):
    title: str = ""
    when: str = ""                 # free text the organiser gave, e.g. "14 Nov 18:30"
    attendees: int | None = None
    venue_name: str = ""
    venue_city: str = ""
    venue_booked: bool | None = None
    venue_address: str = ""                             # set when the organiser confirms the map match
    venue_confirmed: bool | None = None
    store_consent: bool | None = None                   # may we save this event to the shared knowledge base?
    venue_cost: float | None = None
    food: str = ""
    food_supplier: str = ""
    food_cost: float | None = None
    sponsors: list[Sponsor] = Field(default_factory=list)
    volunteers: list[str] = Field(default_factory=list)
    total_budget: float | None = None
    channels: list[str] = Field(default_factory=list)   # Newsletter, Discord, LinkedIn, WhatsApp group
    luma_url: str = ""
    luma_registered: int | None = None                  # live registration count from Luma
    luma_title: str = ""
    notes: str = ""
    covered: list[str] = Field(default_factory=list)    # topics asked (answered or skipped)


class Turn(BaseModel):
    reply: str
    brief: Brief
    done: bool = False


SYSTEM = f"""You are Event Autopilot's intake assistant. Interview a community organiser about their event
before anything is planned. Topics, in this order: {", ".join(TOPICS)}.
- basics: what the event is, date and start time, the Luma link if they have one, and headcount.
  Headcount: if brief.luma_registered is set, do NOT ask how many people. Say Luma shows that many
  registered and ask whether they're expecting more (walk-ins, late sign-ups) and roughly how many in total.
  Set attendees to the total they expect, or to luma_registered if they don't expect more.
  If there is no Luma link, ask for it once; if they have none, ask roughly how many people.
- venue: is a spot booked? which one, which city, hire cost.
- food: what food/drink, which supplier, what it costs.
- sponsorship: any sponsors? names, amounts, confirmed or still asking.
- volunteers: who is helping (names).
- budget: total budget, if there is one.
- posts: which channels to post on (Newsletter, Discord, LinkedIn, WhatsApp group).
Rules:
- Ask ONE short, friendly question per turn about the next topic not yet in brief.covered. Skip topics the
  organiser already answered in passing (still add them to covered).
- Update brief ONLY with facts the organiser actually said. Never guess, never fill defaults.
- If they say skip / don't know / not sure / none, add the topic to covered, record nothing, move on.
- Amounts are plain numbers in GBP.
- When every topic is in covered, set done=true and reply with a 2-3 line recap of what you'll use and what
  you'll leave for them to fill in. Do not ask anything else then."""

FALLBACK_Q = {
    "basics": "What's the event, when is it, and roughly how many people?",
    "venue": "Do you have a spot booked? Which one, and what does it cost?",
    "food": "What food or drinks, from which supplier, and roughly what cost?",
    "sponsorship": "Any sponsors? Names, amounts, and whether they're confirmed.",
    "volunteers": "Who's volunteering? Just names is fine.",
    "budget": "Is there a total budget for this event?",
    "posts": "Where should we post: newsletter, Discord, LinkedIn, WhatsApp group? And the Luma link if you have one.",
}


def step(history: list[dict], brief: Brief) -> Turn:
    """history: [{"role": "user"|"assistant", "content": str}, ...] ending with the organiser's message."""
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in history[-16:])
    user = f"## Brief so far\n{brief.model_dump_json()}\n\n## Conversation\n{convo}\n\nReturn the next turn."
    parsed, _tier = llm.complete_json(SYSTEM, user, Turn, fast=True)
    if parsed is not None:
        parsed.brief.covered = [t for t in TOPICS if t in set(parsed.brief.covered) | set(brief.covered)]
        for f in ("venue_address", "venue_confirmed", "store_consent",  # set by the UI / server, not Grok
                  "luma_registered", "luma_title"):
            setattr(parsed.brief, f, getattr(brief, f))
        parsed.brief.luma_url = brief.luma_url or parsed.brief.luma_url
        if all(t in parsed.brief.covered for t in TOPICS):
            parsed.done = True
        return parsed
    # No Grok: ask the fixed questions in order and keep the raw answers as notes.
    nxt = next((t for t in TOPICS if t not in brief.covered), None)
    b = brief.model_copy(deep=True)
    if history and history[-1]["role"] == "user":
        last = history[-1]["content"].strip()
        if not b.covered:
            b.title = last[:80]
        elif not re.fullmatch(r"(?i)(skip|none|no|n/?a|don'?t know|not sure)\.?", last):
            b.notes = (b.notes + "\n" if b.notes else "") + last
        if nxt:
            b.covered.append(nxt)
            nxt = next((t for t in TOPICS if t not in b.covered), None)
    if nxt is None:
        return Turn(reply="Thanks, that's everything. Planning it now.", brief=b, done=True)
    return Turn(reply=FALLBACK_Q[nxt], brief=b)


def lookup_venue(name: str, city: str = "") -> dict | None:
    """Find the venue on OpenStreetMap (free, no key) so the organiser can confirm it's the right place."""
    q = ", ".join(x for x in (name, city or "London") if x)
    try:
        r = httpx.get("https://nominatim.openstreetmap.org/search",
                      params={"q": q, "format": "jsonv2", "limit": 1, "addressdetails": 1},
                      headers={"User-Agent": "EventAutopilot/0.1 (community events hackathon)"}, timeout=8)
        hits = r.json() if r.status_code == 200 else []
    except Exception:  # noqa: BLE001 — lookup is a convenience, never block the interview
        return None
    if not hits:
        return None
    h = hits[0]
    return {"query": q, "name": h.get("name") or name, "address": h.get("display_name", ""),
            "lat": float(h["lat"]), "lon": float(h["lon"])}


LUMA_LINK = re.compile(r"(?:https?://)?(?:www\.)?(?:lu\.ma|luma\.com)/[\w-]+", re.I)


def luma_registrations(url: str) -> dict | None:
    """Live registered-guest count plus title, start and venue for a Luma event (not cached)."""
    from . import luma
    try:
        ref = luma.parse_ref(url)
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            evt = ref
            if not ref.startswith("evt-"):
                data = luma._get(client, "/url", url=ref).get("data") or {}
                evt = data.get("api_id") or ""
            if not evt:
                return None
            raw = luma._get(client, "/event/get", event_api_id=evt)
        ev = raw.get("event") or {}
        geo = ev.get("geo_address_info") or {}
        return {"registered": int(raw.get("guest_count") or 0), "title": ev.get("name", ""),
                "start": (ev.get("start_at") or "")[:16].replace("T", " "),
                "venue": geo.get("address") or "", "city": geo.get("city") or ""}
    except Exception:  # noqa: BLE001 — a convenience, never block the interview
        return None


def to_source(b: Brief) -> str:
    """Input string for pipeline.run: the Luma link if given, else a one-liner built from the answers."""
    if b.luma_url:
        return b.luma_url
    parts = [b.title or "Community event", b.when]
    if b.attendees:
        parts.append(f"~{b.attendees} people")
    if b.food:
        parts.append(b.food)
    return ", ".join(p for p in parts if p)


# ---------------------------------------------------------------- applied by the pipeline

FOOD = re.compile(r"pizza|food|drink|cater|coffee|tea\b|snack|pastr|breakfast|lunch|dinner|beverage|beer|wine", re.I)
VENUE = re.compile(r"venue|hire|room|space|hall", re.I)


def apply_basics(spec: EventSpec, b: Brief) -> None:
    if b.title and not spec.title:
        spec.title = b.title
    if b.attendees or b.luma_registered:
        spec.expected_attendees = b.attendees or b.luma_registered
    if b.luma_registered is not None:
        spec.missing = [m for m in spec.missing if "attend" not in m and "people" not in m]
    if b.venue_name:
        spec.venue.name = b.venue_name
    if b.venue_city:
        spec.venue.city = b.venue_city
    if b.venue_address and b.venue_confirmed:
        spec.venue.address = b.venue_address
    if b.luma_url and not spec.luma_url:
        spec.luma_url = b.luma_url
    if b.notes:
        spec.description = (spec.description + "\n\n" if spec.description else "") + b.notes
    spec.missing = [m for m in spec.missing if not (b.venue_name and m.startswith("venue"))]
    spec.raw["brief"] = b


def apply_plan(spec: EventSpec, b: Brief) -> None:
    """The organiser's numbers replace the planner's; their volunteers fill the rota."""
    cur = spec.currency
    if b.venue_cost is not None:
        spec.budget = [i for i in spec.budget if not VENUE.search(i.label)]
        spec.budget.insert(0, LineItem(label=f"Venue hire{': ' + b.venue_name if b.venue_name else ''}",
                                       qty=1, unit_cost=b.venue_cost, currency=cur, source=YOU))
        spec.missing = [m for m in spec.missing if m != "venue cost"]
    if b.food_cost is not None:
        spec.budget = [i for i in spec.budget if not FOOD.search(i.label)]
        what = b.food or "Food and drink"
        spec.budget.insert(1, LineItem(label=f"{what}{' from ' + b.food_supplier if b.food_supplier else ''}",
                                       qty=1, unit_cost=b.food_cost, currency=cur, source=YOU))
    elif b.food_supplier:
        for i in spec.budget:
            if FOOD.search(i.label):
                i.note = (i.note + "; " if i.note else "") + f"supplier: {b.food_supplier} ({YOU})"
    if b.volunteers:
        names = iter(b.volunteers)
        for sh in spec.rota:  # only the organiser's people; extra shifts stay unfilled, not invented
            sh.volunteer = next(names, "")
    # Numbers with no basis (neither said nor learned) are not invented: ask instead.
    guessed = [i for i in spec.budget if i.source.startswith("default") and "contingency" not in i.label.lower()]
    if guessed:
        spec.budget = [i for i in spec.budget if i not in guessed]
        spec.missing.append("costs for " + ", ".join(i.label.lower() for i in guessed))
    for i in spec.budget:
        if "contingency" in i.label.lower():
            rest = sum(x.total for x in spec.budget if x is not i)
            i.qty, i.unit_cost = 1, round(rest * 0.10, 2)
    if b.total_budget is not None:
        spec.raw["total_budget"] = b.total_budget
        over = spec.budget_total - b.total_budget
        if over > 0:
            spec.missing.append(f"plan is £{over:,.2f} over your £{b.total_budget:,.0f} budget")
