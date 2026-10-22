"""WP4 copy: page, newsletter, LinkedIn, WhatsApp. Grok when available, templates otherwise."""
from __future__ import annotations

import html
import re

from pydantic import BaseModel

from . import llm
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
    d = f"{s.date.strftime('%A')} {s.date.day} {s.date.strftime('%B %Y')}"
    return d


def _when(s: EventSpec) -> str:
    d = _date(s)
    if d and s.start:
        return f"{d}, {s.start}" + (f"-{s.end}" if s.end else "")
    return d or s.start


def _venue(s: EventSpec) -> str:
    v = s.venue
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
    return ", ".join(h.name for h in s.hosts if h.name)


def _words(t: str) -> int:
    return len(t.split())


# ---------- template tier ----------

def _t_page(s: EventSpec) -> str:
    title, when, venue, price, hosts = _title(s), _when(s), _venue(s), _price(s), _hosts(s)
    fmt = s.format
    lead = s.one_liner.strip() or f"Come and join us for {title}."
    lines = [f"# {title}", "", f"**{lead}**", ""]
    facts = []
    if when:
        facts.append(f"**When:** {when}")
    if venue:
        facts.append(f"**Where:** {venue}")
    if price:
        facts.append(f"**Price:** {price}")
    if hosts:
        facts.append(f"**Hosted by:** {hosts}")
    if facts:
        lines += ["  \n".join(facts), ""]
    desc = re.sub(r"\s+", " ", s.description).strip()
    if desc:
        nd = len(desc.split())
        desc = " ".join(desc.split()[:45])
        lines += [desc + ("..." if nd > 45 else ""), ""]
    aud = s.audience.strip()
    lines.append(
        f"This {fmt} is built for {aud if aud else 'anyone curious who wants to meet people and learn something new'}. "
        "Expect a welcoming room, good conversation and plenty of time to talk to the people around you, "
        "whether you are coming along on your own or with friends."
    )
    lines.append("")
    if s.expected_attendees:
        lines.append(f"We are expecting around {s.expected_attendees} people, so it is a great chance to make new connections "
                     "and pick up ideas you can use straight away.")
        lines.append("")
    lines.append("Spaces can fill up, so reserve yours now and we will see you there.")
    lines.append("")
    lines.append(f"[Register on Luma]({s.luma_url})" if s.luma_url else "Register via our community channels.")
    text = "\n".join(lines)
    # Pad up to the minimum with neutral, non-factual sentences.
    pad = ["Everyone is welcome, and no preparation is needed beyond bringing your curiosity.",
           "Come early to settle in, say hello and grab a seat before we begin.",
           "Stay afterwards to keep the conversation going with the community.",
           "Bring a friend or colleague if you like, because the more voices in the room, the better the conversation gets.",
           "If you have questions before the day, reach out to the organisers and we will happily help.",
           "We keep things friendly and informal, so you can simply turn up, relax and enjoy the evening."]
    i = 0
    while _words(text) < PAGE_WORDS[0] + 8 and i < len(pad):
        text = text.replace("Spaces can fill up", pad[i] + " Spaces can fill up", 1)
        i += 1
    return _fit_page(text)


def _fit_page(text: str) -> str:
    if _words(text) <= PAGE_WORDS[1]:
        return text
    paras = text.split("\n\n")
    # drop the longest middle paragraph until it fits (keep head and CTA)
    while _words("\n\n".join(paras)) > PAGE_WORDS[1] and len(paras) > 4:
        mid = max(range(2, len(paras) - 1), key=lambda k: _words(paras[k]))
        paras.pop(mid)
    out = "\n\n".join(paras)
    if _words(out) > PAGE_WORDS[1]:  # still long: trim description words
        w = out.split(" ")
        out = " ".join(w[: PAGE_WORDS[1]])
    return out


def _t_subject(s: EventSpec) -> str:
    title = _title(s)
    d = s.date.strftime("%d %b").lstrip("0") if s.date else ""
    subj = f"{title}: {d}" if d else f"You're invited: {title}"
    return _clip(subj, LIMITS["subject"])


def _clip(t: str, n: int) -> str:
    t = t.strip()
    if len(t) <= n:
        return t
    return t[: n - 1].rstrip() + "…"


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
    parts = [f"<h2>{e(title)}</h2>",
             f"<p>{e(s.one_liner.strip() or 'We would love to see you at our next event.')}</p>"]
    if items:
        parts.append("<ul>" + "".join(items) + "</ul>")
    parts.append("<p>Spaces can fill up, so grab yours while you can.</p>")
    if s.luma_url:
        parts.append(f'<p><a href="{e(s.luma_url, quote=True)}">Reserve your spot</a></p>')
    return "\n".join(parts)


def _t_linkedin(s: EventSpec) -> str:
    title, when, venue, price = _title(s), _when(s), _venue(s), _price(s)
    lines = [f"We're hosting {title}." if s.title else "We're hosting our next community event.", ""]
    if s.one_liner.strip():
        lines += [s.one_liner.strip(), ""]
    if when:
        lines.append(f"📅 {when}")
    if venue:
        lines.append(f"📍 {venue}")
    if price:
        lines.append(f"🎟 {price}")
    if when or venue or price:
        lines.append("")
    lines.append("Come and meet the community, learn something new and leave with useful connections.")
    if s.luma_url:
        lines += ["", f"Register: {s.luma_url}"]
    lines += ["", "#community #events"]
    return _clip_tail(lines, LIMITS["linkedin"])


def _clip_tail(lines: list[str], n: int) -> str:
    text = "\n".join(lines)
    if len(text) <= n:
        return text
    return text[: n - 1].rstrip() + "…"


def _t_whatsapp(s: EventSpec) -> str:
    title, when, venue, price = _title(s), _when(s), _venue(s), _price(s)
    head = f"Hey! 👋 {title} is coming up." if s.title else "Hey! 👋 Our next event is coming up."
    bits = []
    if when:
        bits.append(f"📅 {when}")
    if venue:
        bits.append(f"📍 {venue}")
    if price:
        bits.append(f"🎟 {price}")
    link = s.luma_url
    tail = f"Grab your spot here: {link}" if link else "Reply here if you're in!"
    body = "\n".join([head, *bits, "Would be great to see you there!", tail])
    if len(body) > LIMITS["whatsapp"]:  # shrink head/bits, keep link last
        room = LIMITS["whatsapp"] - len(tail) - 1
        body = _clip(body[: -len(tail) - 1], room) + "\n" + tail
    return body


def _templates(s: EventSpec) -> dict[str, str]:
    return {
        "page_copy": _t_page(s),
        "newsletter_subject": _t_subject(s),
        "newsletter_html": _t_html(s),
        "linkedin": _t_linkedin(s),
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
- page_copy: 120-180 words, Markdown, lead with the hook, include date, venue and price.
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
    tmpl = _templates(spec)
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
