"""WP4 copy: page, newsletter, LinkedIn, WhatsApp. Grok when available, templates otherwise."""
from __future__ import annotations

import html
import re

from pydantic import BaseModel

from . import llm, luma
from .models import Artifacts, EventSpec, Memory

LIMITS = {"subject": 59, "linkedin": 1300, "whatsapp": 600}
PAGE_WORDS = (120, 180)


class CopyOut(BaseModel):
    page_copy: str
    newsletter_subject: str
    newsletter_html: str
    linkedin: str
    whatsapp: str


# ---------- fact helpers ----------

def _title(s: EventSpec) -> str:
    return s.title.strip() or "Our next community event"


def _date(s: EventSpec) -> str:
    if not s.date:
        return ""
    return f"{s.date.strftime('%A')} {s.date.day} {s.date.strftime('%B %Y')}"


def _short_date(s: EventSpec) -> str:
    return f"{s.date.day} {s.date.strftime('%b')}" if s.date else ""


def _when(s: EventSpec) -> str:
    d = _date(s)
    if d and s.start:
        return f"{d}, {s.start}" + (f"-{s.end}" if s.end else "")
    return d or s.start


def _venue(s: EventSpec) -> str:
    """Venue name + address when known, else name + city, else city, else ''."""
    v = s.venue
    if v.address:
        head = ", ".join(p for p in (v.name, v.address) if p)
        if v.city and v.city.lower() not in head.lower():
            head += f", {v.city}"
        return head
    return ", ".join(p for p in (v.name, v.city) if p)


def _price(s: EventSpec) -> str:
    if s.ticket_price is None:
        return ""
    if s.ticket_price == 0:
        return "Free"
    sym = {"GBP": "£", "USD": "$", "EUR": "€"}.get(s.currency, s.currency + " ")
    amt = f"{s.ticket_price:g}" if s.ticket_price == int(s.ticket_price) else f"{s.ticket_price:.2f}"
    return f"{sym}{amt}"


def _hosts(s: EventSpec) -> str:
    names = [luma.strip_emoji(h.name) for h in s.hosts if h.name]
    names = [n for n in names if n]
    if len(names) > 4:
        return ", ".join(names[:3]) + f" and {len(names) - 3} more"
    return ", ".join(names)


def _speakers(s: EventSpec) -> str:
    sp = s.raw.get("speakers") if isinstance(s.raw, dict) else None
    return ", ".join(sp) if isinstance(sp, list) else ""


def _words(t: str) -> int:
    return len(t.split())


def _tok(t: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", t.lower()))


def _similar(a: str, b: str) -> bool:
    ta, tb = _tok(a), _tok(b)
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) > 0.6


def _lead(s: EventSpec) -> str:
    """The real one_liner, unless it was hard-truncated."""
    ol = s.one_liner.strip()
    return "" if (not ol or ol.endswith("…") or ol.endswith("...")) else ol


def _body_sentences(s: EventSpec, n: int = 2, skip: tuple[str, ...] = ()) -> list[str]:
    """Up to n full description sentences that do not repeat the title or one_liner."""
    out: list[str] = []
    seen = [_title(s), s.one_liner, *skip]
    for sent in luma.sentences(s.description):
        t = luma._BOILER.sub("", sent).strip(" -–—:•*")
        t = re.sub(r"^[A-Z ]{3,12}:\s+", "", t)
        if not t or t[-1] not in ".!?" or not 5 <= _words(t) <= 45:
            continue
        if re.search(r"https?://|version below|\bbelow\)|^(and|but|so)\b", t, re.I):
            continue
        if any(_similar(t, x) for x in seen if x):
            continue
        t = t[0].upper() + t[1:]
        out.append(t)
        seen.append(t)
        if len(out) == n:
            break
    return out


def _food_ok(s: EventSpec) -> bool:
    if any(re.search(r"food|cater|pizza|snack|refreshment|drinks|lunch|dinner", i.label, re.I) for i in s.budget):
        return True
    d = s.description
    return bool(re.search(r"(food|pizza|lunch|dinner|snacks?|refreshments|catering|drinks)[^.\n]{0,40}"
                          r"(provided|included|served|on us|free)|(free|complimentary)\s+(food|pizza|lunch|dinner|snacks?|drinks)",
                          d, re.I))


def _voice(mems: list[Memory], phrase: str) -> bool:
    return any(m.kind == "voice" and phrase in m.text.lower() for m in mems)


def _aud(s: EventSpec) -> str:
    return s.audience.strip()


# ---------- template tier ----------

FORMAT_BLURB = {
    "hackathon": "Come ready to build: bring an idea or find a team on the night, and aim to leave with something working.",
    "workshop": "Expect a practical, hands-on session where you learn by doing and leave with skills you can use straight away.",
    "talk": "Expect a focused programme followed by time for questions and conversation with the people around you.",
    "social": "Expect a relaxed room with no agenda beyond good company, good conversation and meeting new people.",
    "meetup": "Expect short, useful sessions and plenty of time afterwards to talk with others who care about the same things.",
}
FORMAT_PAD = {
    "hackathon": ["Beginners are welcome, and teams can form on the spot if you arrive on your own.",
                  "Small, scrappy projects are just as good as ambitious ones, so start with something you can finish."],
    "workshop": ["No prior experience is assumed, so come as you are and ask as many questions as you like.",
                 "Places are limited so everyone gets proper attention, which is why booking ahead matters."],
    "talk": ["Come with a question or two, because the best moments tend to happen in the discussion.",
             "Arrive a few minutes early to find a good seat before things begin."],
    "social": ["Come on your own or bring friends, because people here are friendly and easy to talk to.",
               "Drop in, say hello and stay as long as you like."],
    "meetup": ["Come on your own or with a colleague, because the room is friendly and easy to join.",
               "Arrive a little early to settle in and say hello before we begin."],
}
GENERIC_PAD = ["Everyone is welcome, and no preparation is needed beyond bringing your curiosity.",
               "Bring a friend or colleague if you like, because the more voices in the room, the better the conversation.",
               "Stay afterwards to keep the conversation going with the community.",
               "If you have questions before the day, reach out to the organisers and we will happily help.",
               "Please register in advance so we can plan the room properly."]
CLOSER = {
    "hackathon": "Spots go quickly, so claim yours now.",
    "workshop": "Seats are limited, so book yours now.",
    "talk": "Reserve your seat and we will see you there.",
    "social": "Grab your ticket and come and say hello.",
    "meetup": "Reserve your place and we will see you there.",
}


def _format_para(s: EventSpec) -> str:
    fmt = s.format
    bits = [FORMAT_BLURB[fmt]]
    sp, aud = _speakers(s), _aud(s)
    if sp:
        bits.insert(0, f"Speakers include {sp}.")
    if aud:
        bits.append(f"It is aimed at {aud}, though everyone curious is welcome.")
    return " ".join(bits)


def _t_page(s: EventSpec) -> str:
    title, when, venue, price, hosts = _title(s), _when(s), _venue(s), _price(s), _hosts(s)
    fmt = s.format
    lead = _lead(s)
    body = _body_sentences(s, 2 if lead else 3)
    if not lead:
        lead, body = (body[0], body[1:]) if body else (f"A {fmt} for the community.", [])
    facts = []
    if when:
        facts.append(f"**When:** {when}")
    if venue:
        facts.append(f"**Where:** {venue}")
    if price:
        facts.append(f"**Price:** {price}")
    if hosts:
        facts.append(f"**Hosted by:** {hosts}")
    attend = ""
    going = s.raw.get("guest_count") if isinstance(s.raw, dict) else 0
    if isinstance(going, int) and going >= 20:
        attend = f"Around {going} people are already going, so it is a good room for meeting new faces."
    cta = f"[Register on Luma]({s.luma_url})" if s.luma_url else "Register via our community channels."
    pads = FORMAT_PAD[fmt] + GENERIC_PAD

    def build(sentences: list[str], npad: int) -> str:
        paras = [f"# {title}", f"**{lead}**"]
        if facts:
            paras.append("  \n".join(facts))
        if sentences:
            paras.append(" ".join(sentences))
        paras.append(_format_para(s))
        if attend:
            paras.append(attend)
        if npad:
            paras.append(" ".join(pads[:npad]))
        paras += [CLOSER[fmt], cta]
        return "\n\n".join(paras)

    # Drop whole description sentences if too long; add neutral pad sentences if too short.
    sents = list(body)
    text = build(sents, 0)
    while _words(text) > PAGE_WORDS[1] and sents:
        sents.pop()
        text = build(sents, 0)
    npad = 0
    while _words(text) < PAGE_WORDS[0] + 6 and npad < len(pads):
        npad += 1
        text = build(sents, npad)
    return text


def _t_subject(s: EventSpec) -> str:
    cap = LIMITS["subject"]
    title = _title(s)
    d = _short_date(s)
    if len(title) > cap:
        head = re.split(r"\s[-–—]\s|:|\|", title)[0].strip()
        title = head or title
    cand = f"{title} · {d}" if d else title
    if len(cand) <= cap:
        return cand
    if len(title) <= cap:
        return title
    cut = title[:cap].rsplit(" ", 1)[0].rstrip(" ,;:-–—|·")  # word boundary, no ellipsis
    return cut or title[:cap]


def _t_html(s: EventSpec) -> str:
    e = html.escape
    title = _title(s)
    items = []
    if _when(s):
        items.append(f"<li><strong>When:</strong> {e(_when(s))}</li>")
    if _venue(s):
        items.append(f"<li><strong>Where:</strong> {e(_venue(s))}</li>")
    if _price(s):
        items.append(f"<li><strong>Price:</strong> {e(_price(s))}</li>")
    if _hosts(s):
        items.append(f"<li><strong>Hosted by:</strong> {e(_hosts(s))}</li>")
    lead = _lead(s)
    body = _body_sentences(s, 2 if lead else 3)
    if not lead:
        lead, body = (body[0], body[1:]) if body else ("We would love to see you at our next event.", [])
    parts = [f"<h2>{e(title)}</h2>", f"<p>{e(lead)}</p>"]
    if body:
        parts.append(f"<p>{e(' '.join(body))}</p>")
    if items:
        parts.append("<ul>" + "".join(items) + "</ul>")
    parts.append(f"<p>{e(CLOSER[s.format])}</p>")
    if s.luma_url:
        parts.append(f'<p><a href="{e(s.luma_url, quote=True)}">Reserve your spot</a></p>')
    return "\n".join(parts)


LI_HEAD = {
    "hackathon": "Calling all builders: {t}.",
    "workshop": "Learn by doing: {t}.",
    "talk": "Join the conversation: {t}.",
    "social": "Come and hang out: {t}.",
    "meetup": "We're getting the community together: {t}.",
}
LI_CLOSE = {
    "hackathon": "Bring an idea, find a team and build something.",
    "workshop": "Places are limited, so book early.",
    "talk": "Come with questions and stay for the conversation.",
    "social": "Come along, meet new people and enjoy the evening.",
    "meetup": "Come and meet the community and swap ideas.",
}
_TAGS = [(r"\bAI\b|artificial intelligence|\bLLMs?\b", "#AI", 0), (r"\bstartups?\b|\bfounders?\b", "#startups", re.I),
         (r"\bfintech\b|\bfinance\b", "#fintech", re.I), (r"\bdesign(?:ers?)?\b", "#design", re.I),
         (r"\bpython\b", "#python", re.I), (r"\bmusic\b|\bDJ\b", "#music", 0), (r"\bfilm\b", "#film", re.I),
         (r"\bmongodb\b", "#MongoDB", re.I)]
_FORMAT_TAG = {"hackathon": "#hackathon", "workshop": "#workshop"}


def _hashtags(s: EventSpec) -> list[str]:
    """At most 2 relevant tags: a title hit, or 3+ mentions in the description."""
    tags: list[str] = []
    if s.format in _FORMAT_TAG:
        tags.append(_FORMAT_TAG[s.format])
    for pat, tag, fl in _TAGS:
        if len(tags) >= 2:
            break
        if re.search(pat, s.title, fl) or len(re.findall(pat, s.description, fl)) >= 3:
            tags.append(tag)
    return tags[:2]


def _short_title(s: EventSpec) -> str:
    """Title trimmed at its first ':' / '|' / ' - ' when long, for headlines."""
    t = _title(s)
    if len(t) > 45:
        t = re.split(r"\s[-–—]\s|:|\|", t)[0].strip() or t
    return t


def _t_linkedin(s: EventSpec, mems: list[Memory]) -> str:
    title, when, venue, price, hosts = _title(s), _when(s), _venue(s), _price(s), _hosts(s)
    no_emoji = _voice(mems, "no emoji")
    head = LI_HEAD[s.format].format(t=_short_title(s)) if s.title else "We're hosting our next community event."
    lead = _lead(s)
    body = _body_sentences(s, 1 if lead else 2)
    if not lead and body:
        lead, body = body[0], body[1:]
    lines = [head, ""]
    if lead:
        lines.append(lead)
    if body:
        lines.append(body[0])
    if hosts:
        lines.append(f"Hosted by {hosts}.")
    if lead or body or hosts:
        lines.append("")
    fact = (lambda icon, label, v: f"{label}: {v}" if no_emoji else f"{icon} {v}")
    if when:
        lines.append(fact("📅", "When", when))
    if venue:
        lines.append(fact("📍", "Where", venue))
    if price:
        lines.append(fact("🎟", "Price", price))
    if _voice(mems, "mention food") and _food_ok(s):
        lines.append("Food provided.")
    if when or venue or price:
        lines.append("")
    lines.append(LI_CLOSE[s.format])
    if s.luma_url:
        lines += ["", f"Register: {s.luma_url}"]
    tags = _hashtags(s)
    if tags:
        lines += ["", " ".join(tags)]
    text = "\n".join(lines)
    if no_emoji:
        text = "\n".join(luma.strip_emoji(ln) for ln in text.splitlines())
    if len(text) > LIMITS["linkedin"]:  # drop the optional body before ever cutting
        text = text.replace(f"{body[0]}\n", "", 1) if body else text
    return _clip_tail(text, LIMITS["linkedin"])


def _clip_tail(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[:n].rsplit("\n", 1)[0].rstrip()


WA_HEAD = {
    "hackathon": "Hey! 👋 Builders wanted:",
    "workshop": "Hey! 👋 Hands-on session coming up:",
    "talk": "Hey! 👋 One for the diary:",
    "social": "Hey! 👋 Plans coming up:",
    "meetup": "Hey! 👋 Next get-together:",
}
WA_CLOSE = {
    "hackathon": "Bring a friend and build something!",
    "workshop": "Places are limited, so grab one early!",
    "talk": "Would be great to see you there!",
    "social": "Bring friends, the more the merrier!",
    "meetup": "Would be great to see you there!",
}


def _t_whatsapp(s: EventSpec) -> str:
    cap = LIMITS["whatsapp"]
    title, when, venue, price, hosts = _title(s), _when(s), _venue(s), _price(s), _hosts(s)
    head = f"{WA_HEAD[s.format]} {_short_title(s)}" if s.title else "Hey! 👋 Our next event is coming up."
    link = s.luma_url
    tail = f"Grab your spot here: {link}" if link else "Reply here if you're in!"
    must = [head]
    for icon, v in (("📅", when), ("📍", venue), ("🎟", price)):
        if v:
            must.append(f"{icon} {v}")
    extras = []  # dropped last-first when over budget
    lead = _lead(s)
    if lead:
        extras.append(lead)
    if hosts:
        extras.append(f"Hosted by {hosts}")
    extras.append(WA_CLOSE[s.format])
    while True:
        body = "\n".join([*must, *extras, tail])
        if len(body) <= cap or not extras:
            break
        extras.pop(0)
    if len(body) > cap:  # still long: shorten the venue line, keep the link last
        must = [m for m in must if not m.startswith("📍")] + ([f"📍 {s.venue.name or s.venue.city}"] if (s.venue.name or s.venue.city) else [])
        body = "\n".join([*must, tail])
    return body


def _templates(s: EventSpec, mems: list[Memory] | None = None) -> dict[str, str]:
    mems = mems or []
    return {
        "page_copy": _t_page(s),
        "newsletter_subject": _t_subject(s),
        "newsletter_html": _t_html(s),
        "linkedin": _t_linkedin(s, mems),
        "whatsapp": _t_whatsapp(s),
    }


# ---------- Grok tier ----------

SYSTEM = ("You are the copywriter for a community events team. Write warm, specific, concise copy in the "
          "community's voice. Do not invent facts (speakers, prices, sponsors, times) that are absent from "
          "the Event or Community memory sections.")


def _prompt(s: EventSpec, mems: list[Memory]) -> str:
    event = s.model_dump_json(include={"title", "one_liner", "description", "date", "start", "end", "venue",
                                       "expected_attendees", "audience", "format", "hosts", "ticket_price",
                                       "currency", "luma_url"}, indent=1)
    mem = "\n".join(f"- {m.text} — from {m.provenance or 'past events'}" for m in mems) or "(none)"
    return f"""## Event
{event}

## Community memory
{mem}

## Task
Return JSON with keys page_copy, newsletter_subject, newsletter_html, linkedin, whatsapp.
- page_copy: 120-180 words, never end a sentence with '...', Markdown, lead with the hook, include date, venue and price.
- newsletter_subject: under 60 characters.
- newsletter_html: simple <p>, <h2>, <ul> only, exactly one call-to-action link to {s.luma_url or '(no link available)'}.
- linkedin: at most 1300 characters, at most 3 hashtags.
- whatsapp: at most 600 characters, casual, emoji ok, ends with the link {s.luma_url or ''}.
Use only facts in Event or Community memory. Do not invent speakers or prices."""


def _valid(name: str, text: str, s: EventSpec) -> bool:
    """Post-validate one Grok artifact; failures fall back to the template for that artifact."""
    t = (text or "").strip()
    if not t:
        return False
    url = s.luma_url
    if name == "page_copy":
        return PAGE_WORDS[0] - 10 <= _words(t) <= PAGE_WORDS[1] + 10
    if name == "newsletter_subject":
        return len(t) <= LIMITS["subject"]
    if name == "newsletter_html":
        return (not url) or (t.count(url) >= 1 and "<p" in t)
    if name == "linkedin":
        return len(t) <= LIMITS["linkedin"] and len(re.findall(r"(?<!\w)#\w+", t)) <= 3
    if name == "whatsapp":
        return len(t) <= LIMITS["whatsapp"] and ((not url) or t.rstrip().endswith(url))
    return False


def generate(spec: EventSpec, memories: list[Memory]) -> tuple[Artifacts, dict[str, str]]:
    """Return (Artifacts, {artifact_name: 'grok'|'template'})."""
    tmpl = _templates(spec, memories)
    out = dict(tmpl)
    tiers = {k: "template" for k in tmpl}
    try:
        parsed, tier = llm.complete_json(SYSTEM, _prompt(spec, memories), CopyOut, fast=True)
    except Exception:
        parsed, tier = None, "template"
    if parsed is not None:
        for k in tmpl:
            val = getattr(parsed, k).strip()
            if _valid(k, val, spec):
                out[k] = val
                tiers[k] = tier
    return Artifacts(**out), tiers
