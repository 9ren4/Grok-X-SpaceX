"""Fetch a public Luma event and normalise it to an EventSpec. No API key needed."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from .config import OUT
from .models import EventSpec, Person, Venue

API = "https://api.lu.ma"
TIMEOUT = httpx.Timeout(10.0, connect=5.0)
DEFAULT_ATTENDEES = 50


class LumaError(Exception):
    """Base error for Luma lookups."""


class LumaNotFound(LumaError):
    """The link or id does not match any public Luma event."""


def parse_ref(url_or_slug: str) -> str:
    """Reduce any Luma link, slug or evt- id to a bare slug or evt- id."""
    s = (url_or_slug or "").strip()
    if not s:
        raise LumaNotFound("Empty Luma link")
    if s.startswith("evt-"):
        return s
    if "://" not in s and "/" in s:
        s = "https://" + s
    if "://" in s:
        parts = [p for p in urlparse(s).path.split("/") if p]
        s = parts[-1] if parts else ""
    s = s.split("?")[0].split("#")[0].strip()
    if not s:
        raise LumaNotFound(f"No event slug found in {url_or_slug!r}")
    return s


def _get(client: httpx.Client, path: str, **params: str) -> dict[str, Any]:
    try:
        r = client.get(f"{API}{path}", params=params)
    except httpx.HTTPError as e:
        raise LumaError(f"Could not reach Luma: {e}") from e
    if r.status_code == 404 or "could not find what you were looking for" in r.text[:300]:
        raise LumaNotFound("Luma event not found (check the link is public)")
    if r.status_code >= 400:
        raise LumaError(f"Luma returned HTTP {r.status_code} for {path}")
    try:
        return r.json()
    except ValueError as e:
        raise LumaError(f"Luma returned non-JSON for {path}") from e


def _cache_path(key: str):
    return OUT / f"luma_{re.sub(r'[^A-Za-z0-9_-]', '_', key)}.json"


def fetch_raw(ref: str) -> dict[str, Any]:
    """Return the raw event/get payload for a slug or evt- id, using out/ as a cache."""
    cache = _cache_path(ref)
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except ValueError:
            pass
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        evt_id = ref
        if not ref.startswith("evt-"):
            resolved = _get(client, "/url", url=ref)
            data = resolved.get("data") or {}
            if resolved.get("kind") != "event" or not data.get("api_id"):
                raise LumaNotFound(f"{ref!r} is not a Luma event page")
            evt_id = data["api_id"]
        raw = _get(client, "/event/get", event_api_id=evt_id)
    if not raw.get("event"):
        raise LumaNotFound(f"Luma returned no event for {ref!r}")
    text = json.dumps(raw)
    for key in {ref, evt_id}:
        _cache_path(key).write_text(text)
    return raw


def flatten_doc(node: Any) -> str:
    """Flatten a Luma rich-text doc (ProseMirror JSON) to plain text."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(flatten_doc(n) for n in node)
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "hard_break":
        return "\n"
    text = node.get("text", "") + flatten_doc(node.get("content", []))
    if node.get("type") in {"paragraph", "heading", "list_item", "bullet_list", "ordered_list"}:
        text += "\n"
    return text


_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\U00002B00-\U00002BFF"
    "\U0000FE0F\U0000200D\U00002190-\U000021FF\U00002300-\U000023FF]+"
)
_BOILER = re.compile(
    r"^\s*(?:please\s+)?(?:come\s+and\s+)?(?:join\s+(?:us|me)(?:\s+(?:for|at|on|in))?|you(?:'|’)re\s+invited(?:\s+to)?|"
    r"we(?:'|’)re\s+(?:excited|delighted|thrilled|happy)\s+to\s+(?:invite\s+you\s+to|announce|host))\s*"
    r"(?:(?:a|an|the|our)\s+)?", re.I)
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“‘(])")


def strip_emoji(t: str) -> str:
    """Remove emoji and tidy the whitespace they leave behind."""
    return re.sub(r"\s{2,}", " ", _EMOJI.sub("", t)).strip()


def sentences(text: str) -> list[str]:
    """Split plain text into sentences (line breaks also end a sentence)."""
    out: list[str] = []
    for line in text.splitlines():
        line = re.sub(r"\s+", " ", strip_emoji(line)).strip()
        if line:
            out += [p.strip() for p in _SENT_SPLIT.split(line) if p.strip()]
    return out


def _tokens(t: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", t.lower()))


def _shorten(s: str, limit: int) -> str:
    """Cut at the last clause boundary under limit; '' if no sensible cut exists."""
    window = s[: limit - 1]
    cuts = [m.start() for m in re.finditer(r",\s|;\s|\s[—–-]\s|:\s", window)]
    cuts = [c for c in cuts if c >= 50]
    return window[: cuts[-1]].rstrip(" ,;:-–—") + "." if cuts else ""


def derive_one_liner(description: str, title: str = "", limit: int = 140) -> str:
    """First real sentence of the description, boilerplate/emoji stripped, <= limit chars."""
    tt = _tokens(title)
    cands: list[str] = []
    for sent in sentences(description):
        s = _BOILER.sub("", sent).strip(" -–—:•*")
        s = re.sub(r"^[A-Z ]{3,12}:\s+", "", s)  # 'WHAT: ' style labels
        n = len(s.split())
        if n < 6 or re.match(r"https?://", s) or re.search(r"version below|\bbelow\)", s, re.I):
            continue
        if n < 12 and s[-1] not in ".!?":
            continue  # a heading, not a sentence
        st = _tokens(s)
        if tt and st and len(st & tt) / len(st | tt) > 0.6:
            continue  # restates the title
        cands.append(s[0].upper() + s[1:])
    weak = re.compile(r"^(and|but|so|or|this|that|these|those|it|its|we|we'd|we'll|they|there|you|then|also|if)\b", re.I)
    if cands and len(cands[0]) <= limit:
        return cands[0]
    for s in cands[1:6]:
        if len(s) <= limit and len(s.split()) >= 8 and not weak.match(s):
            return s
    for s in cands[:1]:
        short = _shorten(s, limit)
        if short:
            return short
    if cands:
        w = cands[0][: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-–—")
        return w + "…"
    return ""


_AUDIENCE = [
    (r"founders?|entrepreneurs?", "founders and entrepreneurs"),
    (r"students?", "students"),
    (r"developers?|engineers?|hackers?|builders?", "developers and builders"),
    (r"designers?", "designers"),
    (r"investors?", "investors and founders"),
    (r"women", "women in the industry"),
    (r"finance|banking|fintech", "finance professionals"),
    (r"researchers?|academics?", "researchers"),
    (r"artists?|creatives?", "creatives"),
]


def infer_audience(title: str, description: str) -> str:
    """Short audience phrase: a keyword in the title, or one after 'for/welcome/aimed at'. Else ''."""
    for pat, phrase in _AUDIENCE:
        if re.search(rf"\b(?:{pat})\b", title, re.I):
            return phrase
    for pat, phrase in _AUDIENCE:
        if re.search(rf"\b(?:for|welcomes?|calling|aimed at|designed for|open to)\b[^.\n]{{0,40}}\b(?:{pat})\b",
                     description, re.I):
            return phrase
    return ""


_FORMATS = [
    ("hackathon", r"hackathon|build\s*night|buildathon|hack\s*house|hacker\s*house"),
    ("workshop", r"workshop|masterclass|bootcamp|hands-on|hands on"),
    ("talk", r"screening|panel|summit|conversation|\btalks?\b|fireside|keynote|lecture|paper reading|conference"),
    ("social", r"drinks|comedy|social|networking|\bclub\b|party|mixer|fuckup|celebration|clubhouse"),
    ("meetup", r"meet-?up|user group"),
]


def detect_format(title: str, description: str) -> str:
    """Keyword-map the format; a title hit counts x5. Ties go to list order; default meetup."""
    best, score = "meetup", 0
    for fmt, pat in _FORMATS:
        n = 5 * len(re.findall(pat, title.lower())) + len(re.findall(pat, description.lower()))
        if n > score:
            best, score = fmt, n
    return best


_NAME = r"[A-Z][\w'’.\-]+(?:\s+[A-Z][\w'’.\-]+){1,3}"


def parse_speakers(description: str) -> list[str]:
    """Names from an explicit 'Speakers: A, B and C' style line; [] if not confidently parseable."""
    names: list[str] = []
    for line in description.splitlines():
        m = re.match(r"\s*(?:speakers?|panell?ists?|featuring|line-?up|guest speakers?)\s*:\s*(.+)$", line, re.I)
        if not m:
            continue
        for part in re.split(r",|;|\band\b|&|\|", m.group(1)):
            part = strip_emoji(part).strip(" .")
            if re.fullmatch(_NAME, part):
                names.append(part)
    return list(dict.fromkeys(names))[:8]


def _venue_from_geo(geo: dict[str, Any]) -> tuple[str, str]:
    """(name, address) from a non-obfuscated geo_address_info. Never invents."""
    place = (geo.get("address") or geo.get("description") or "").strip()
    full = (geo.get("full_address") or "").strip()
    country = (geo.get("country") or "").strip()
    rest = full
    name = ""
    if place and not re.match(r"^\d", place):
        name = place
        if full.lower().startswith(place.lower()):
            rest = full[len(place):].lstrip(", ")
    elif not full:
        rest = place
    if not rest:
        rest = (geo.get("short_address") or "").strip() if not name else ""
    for tail in (country, "UK", "USA"):
        if tail and rest.endswith(", " + tail):
            rest = rest[: -len(tail) - 2]
            break
    return name, rest


def _localise(iso: str | None, tz: ZoneInfo) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz)
    except ValueError:
        return None


def to_spec(raw: dict[str, Any]) -> EventSpec:
    """Map a raw Luma event/get payload onto an EventSpec."""
    ev = raw.get("event") or {}
    missing: list[str] = []

    tzname = ev.get("timezone") or ""
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        tz = ZoneInfo("Europe/London")
        tzname = "Europe/London"
        missing.append("timezone")

    start = _localise(ev.get("start_at"), tz)
    end = _localise(ev.get("end_at"), tz)
    if not start:
        missing.append("date")
    if not end:
        missing.append("end")

    geo = ev.get("geo_address_info") or {}
    venue = Venue(city=geo.get("city") or geo.get("city_state") or "")
    if geo.get("mode") not in (None, "obfuscated"):
        venue.name, venue.address = _venue_from_geo(geo)
    if not venue.city:
        missing.append("venue.city")
    if not venue.name:
        missing.append("venue.name")
    if not venue.address:
        missing.append("venue.address")

    tickets = [t for t in raw.get("ticket_types") or [] if not t.get("is_hidden")]
    prices = [t["cents"] for t in tickets if t.get("cents") is not None]
    price: float | None
    if prices:
        price = round(min(prices) / 100, 2)
    elif tickets:
        price = 0.0
    else:
        price = None
        missing.append("ticket_price")
    currency = next(
        ((t.get("currency") or "").upper() for t in tickets if t.get("cents") is not None and t.get("currency")),
        "GBP",
    )

    guests = raw.get("guest_count") or 0
    caps = [t["max_capacity"] for t in tickets if t.get("max_capacity")]
    attendees = guests or sum(caps)
    if not attendees:
        attendees = DEFAULT_ATTENDEES
        missing.append("attendees")
    if venue.capacity is None and caps:
        venue.capacity = sum(caps)

    hosts = [
        Person(name=h.get("name") or " ".join(filter(None, [h.get("first_name"), h.get("last_name")])), role="host")
        for h in raw.get("hosts") or []
        if h.get("name") or h.get("first_name")
    ]
    if not hosts:
        missing.append("hosts")

    description = flatten_doc(raw.get("description_mirror")).strip()
    if not description:
        missing.append("description")
    title = ev.get("name") or ""
    one_liner = derive_one_liner(description, title)
    audience = infer_audience(title, description)
    if not one_liner:
        missing.append("one_liner")
    if not audience:
        missing.append("audience")
    fmt = detect_format(title, description)
    speakers = parse_speakers(description)
    if speakers:
        raw = {**raw, "speakers": speakers}

    slug = ev.get("url") or ""
    return EventSpec(
        id=ev.get("api_id") or raw.get("api_id") or "",
        title=title,
        one_liner=one_liner,
        audience=audience,
        format=fmt,  # type: ignore[arg-type]
        description=description,
        date=start,
        start=start.strftime("%H:%M") if start else "",
        end=end.strftime("%H:%M") if end else "",
        timezone=tzname,
        venue=venue,
        expected_attendees=attendees,
        hosts=hosts,
        ticket_price=price,
        currency=currency,
        luma_url=f"https://lu.ma/{slug}" if slug else "",
        cover_url=ev.get("cover_url") or "",
        missing=missing,
        raw=raw,
    )


def fetch_event(url_or_slug: str) -> EventSpec:
    """Fetch any public Luma link, slug or evt- id as an EventSpec. Raises LumaNotFound."""
    return to_spec(fetch_raw(parse_ref(url_or_slug)))
