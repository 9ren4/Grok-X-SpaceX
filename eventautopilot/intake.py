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
- At most ONE follow-up per topic. Once the organiser has named a venue (or said there isn't one), venue is
  covered; same for every topic. Never ask whether something is "confirmed" again.
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


def step(history: list[dict], brief: Brief, lookup: str = "") -> Turn:
    """history: [{"role": "user"|"assistant", "content": str}, ...] ending with the organiser's message.
    lookup: what the knowledge base said about the thing the organiser wasn't sure about."""
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in history[-16:])
    extra = (f"\n\n## Knowledge base lookup (the organiser wasn't sure)\n{lookup}\n"
             "Start your reply with one short sentence saying what you found in the community's records, "
             "or that you found nothing, and that options are below (search online, message the venue or "
             "supplier, or add it later). Do NOT put any of these numbers into the brief yourself; the "
             "organiser decides. Then ask the next question.") if lookup else ""
    user = f"## Brief so far\n{brief.model_dump_json()}\n\n## Conversation\n{convo}{extra}\n\nReturn the next turn."
    parsed, _tier = llm.complete_json(SYSTEM, user, Turn, fast=True)
    if parsed is not None:
        parsed.brief.covered = [t for t in TOPICS if t in set(parsed.brief.covered) | set(brief.covered)]
        for f in ("venue_address", "venue_confirmed", "store_consent",  # set by the UI / server, not Grok
                  "luma_registered", "luma_title"):
            setattr(parsed.brief, f, getattr(brief, f))
        parsed.brief.luma_url = brief.luma_url or parsed.brief.luma_url
        for f in Brief.model_fields:  # Grok can add or change answers, never silently erase one
            if getattr(parsed.brief, f) in ("", None, []) and getattr(brief, f) not in ("", None, []):
                setattr(parsed.brief, f, getattr(brief, f))
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


UNSURE = re.compile(r"(?i)\b(not sure|unsure|don'?t know|dunno|no idea|idk|tbc|not yet|no clue|haven'?t (?:checked|asked))\b")


HELP = re.compile(r"(?i)\b(look (?:for|it up|up)|find|search|suggest|recommend|options|ideas|help me|you (?:pick|choose|decide)|any idea)\b")
FOOD_CATS = re.compile(r"(?i)pizza|cater|coffee|drink|food|vegan|snack|bakery|pastr")


def suggest_options(topic: str, b: Brief) -> list[dict]:
    """What the community has used before for this topic, priced for this event's headcount."""
    try:
        from . import memory
        nodes = memory.graph(limit=500).get("nodes", [])
    except Exception:  # noqa: BLE001
        return []
    heads = b.attendees or b.luma_registered or 0
    out = []
    for n in nodes:
        a = n.get("attrs") or {}
        if topic == "food" and n.get("kind") == "supplier" and FOOD_CATS.search(str(a.get("category", ""))):
            cost, how = None, ""
            if a.get("price_per_head_gbp") and heads:
                cost = round(float(a["price_per_head_gbp"]) * heads, 2)
                how = f"£{a['price_per_head_gbp']}/head × {heads}"
            elif a.get("price_per_pizza_gbp") and heads:
                pizzas = -(-heads // 4)
                cost = round(float(a["price_per_pizza_gbp"]) * pizzas, 2)
                how = f"{pizzas} pizzas × £{a['price_per_pizza_gbp']}"
            what = str(a.get("category", "food")).capitalize()
            out.append({"label": f"{what} from {n['name']}", "food": what, "supplier": n["name"], "cost": cost,
                        "detail": "; ".join(x for x in (how, f"used at {n.get('times_used', 1)} events",
                                                        a.get("lead_time_days") and f"{a['lead_time_days']} days notice") if x),
                        "seeded": bool(a.get("seed"))})
        elif topic == "venue" and n.get("kind") == "venue":
            cap = a.get("capacity")
            if heads and cap and int(cap) < heads:
                continue  # too small for this event
            cost = a.get("hire_cost_gbp")
            out.append({"label": n["name"], "venue": n["name"], "city": a.get("area") or a.get("city", ""),
                        "cost": float(cost) if cost is not None else None,
                        "detail": "; ".join(str(x) for x in (cap and f"holds {cap}", a.get("quirks")) if x),
                        "seeded": bool(a.get("seed"))})
    out.sort(key=lambda o: (-(o["cost"] is not None), o["cost"] or 0))
    return out[:5]


def current_topic(brief: Brief, last_question: str = "") -> str | None:
    """The topic of the question just asked (from its wording), else the next uncovered one."""
    q = last_question.lower()
    if re.search(r"food|drink|cater|supplier|pizza", q):
        return "food"
    if re.search(r"venue|spot|location|hire", q):
        return "venue"
    return next((t for t in TOPICS if t not in brief.covered), None)


def _kb_node(kind: str, name: str) -> dict | None:
    if not name:
        return None
    try:
        from . import memory
        nodes = memory.graph(limit=500).get("nodes", [])
    except Exception:  # noqa: BLE001
        return None
    key = name.lower()
    return next((n for n in nodes if n.get("kind") == kind and
                 (key in n["name"].lower() or n["name"].lower() in key)), None)


def help_unsure(topic: str, b: Brief) -> dict | None:
    """The organiser doesn't know an answer: check the knowledge base and offer next steps."""
    if topic == "venue":
        subject, kind = b.venue_name, "venue"
    elif topic == "food":
        subject, kind = b.food_supplier or b.food, "supplier"
    else:
        return None
    node = _kb_node(kind, subject)
    a = (node or {}).get("attrs") or {}
    facts, suggest = [], None
    if node:
        if a.get("hire_cost_gbp") is not None:
            suggest = float(a["hire_cost_gbp"])
            facts.append(f"hire cost £{suggest:,.0f} last time")
        heads = b.attendees or b.luma_registered or 0
        if a.get("price_per_head_gbp") and heads:
            suggest = round(float(a["price_per_head_gbp"]) * heads, 2)
            facts.append(f"£{a['price_per_head_gbp']}/head, about £{suggest:,.0f} for {heads} people")
        elif a.get("price_per_pizza_gbp") and heads:
            pizzas = -(-heads // 4)
            suggest = round(float(a["price_per_pizza_gbp"]) * pizzas, 2)
            facts.append(f"£{a['price_per_pizza_gbp']}/pizza, about {pizzas} pizzas ≈ £{suggest:,.0f} for {heads} people")
        for k in ("capacity", "quirks", "contact"):
            if a.get(k):
                facts.append(f"{k}: {a[k]}")
        if a.get("capacity") and heads and int(a["capacity"]) < heads:
            facts.append(f"⚠️ holds {a['capacity']} but you're expecting {heads}")
    found = f"Found {node['name']} in your community's records: " + "; ".join(facts) if node and facts else \
        f"Nothing about {subject or 'this'} in your community's records yet."
    q = f"{subject} {'hire cost' if topic == 'venue' else 'catering price'} {b.venue_city or 'London'}".strip()
    return {"topic": topic, "subject": subject or ("the venue" if topic == "venue" else "the caterer"),
            "found": bool(node and facts), "summary": found, "suggest": suggest,
            "seeded": bool(a.get("seed")), "contact": a.get("contact", ""),
            "search_url": "https://www.google.com/search?q=" + httpx.QueryParams({"q": q})["q"].replace(" ", "+")}


class Draft(BaseModel):
    subject: str
    message: str


def draft_message(topic: str, b: Brief, contact: str = "") -> Draft:
    """A short message the organiser can send to the venue or caterer to get the missing answer."""
    who = b.venue_name if topic == "venue" else (b.food_supplier or "the caterer")
    ask = ("hire cost, availability and what AV is included" if topic == "venue"
           else "a quote, lead time and dietary options")
    facts = f"Event: {b.title or 'community event'}; when: {b.when or 'TBC'}; people: {b.attendees or b.luma_registered or 'TBC'}"
    parsed, _ = llm.complete_json(
        "Write a short, friendly message from a volunteer community organiser. Plain text, under 90 words, "
        "no placeholders in square brackets, sign off as 'the organising team'.",
        f"To: {who}. Ask for {ask}. {facts}.", Draft, fast=True)
    if parsed:
        return parsed
    return Draft(subject=f"Enquiry: {b.title or 'community event'}",
                 message=f"Hi {who},\n\nWe're planning {b.title or 'a community event'} ({b.when or 'date TBC'}, "
                         f"about {b.attendees or b.luma_registered or 'TBC'} people). Could you let us know your "
                         f"{ask}?\n\nThanks,\nthe organising team")


def lookup_supplier(name: str, b: Brief) -> dict:
    """A supplier we have no records for: find its nearest branches to the venue (OpenStreetMap)."""
    import math
    centre = lookup_venue(b.venue_name, b.venue_city) if b.venue_name else None
    lat, lon = (centre["lat"], centre["lon"]) if centre else (51.5074, -0.1278)  # central London
    near = centre["name"] if centre else "central London (couldn't place the venue on the map)"
    branches = []
    # People type brands loosely ("mcdonalds"); OpenStreetMap names them properly ("McDonald's").
    variants = list(dict.fromkeys([name, re.sub(r"(?i)s$", "'s", name.strip()), name.title()]))
    try:
        hits = []
        for q_ in variants:
            r = httpx.get("https://nominatim.openstreetmap.org/search",
                          params={"q": q_, "format": "jsonv2", "limit": 10, "bounded": 1,
                                  "viewbox": f"{lon - 0.04},{lat + 0.025},{lon + 0.04},{lat - 0.025}"},
                          headers={"User-Agent": "EventAutopilot/0.1 (community events hackathon)"}, timeout=8)
            hits = r.json() if r.status_code == 200 else []
            if hits:
                break
        for h in hits:
            hl, ho = float(h["lat"]), float(h["lon"])
            km = 6371 * 2 * math.asin(math.sqrt(math.sin(math.radians(hl - lat) / 2) ** 2 + math.cos(math.radians(lat))
                                                * math.cos(math.radians(hl)) * math.sin(math.radians(ho - lon) / 2) ** 2))
            branches.append({"address": h.get("display_name", ""), "lat": hl, "lon": ho, "km": round(km, 2)})
    except Exception:  # noqa: BLE001
        pass
    branches.sort(key=lambda x: x["km"])
    q = lambda s: "https://www.google.com/search?q=" + s.replace(" ", "+")  # noqa: E731
    return {"name": name, "near": near, "branches": branches[:3],
            "prices_url": q(f"{name} catering order price {b.venue_city or 'London'}"),
            "maps_url": "https://www.google.com/maps/search/?api=1&query=" + f"{name} near {near}".replace(" ", "+")}


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
