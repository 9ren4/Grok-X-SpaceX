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
        venue.address = geo.get("full_address") or geo.get("address") or ""
        venue.name = geo.get("description") or ""
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
    missing += ["one_liner", "audience"]

    slug = ev.get("url") or ""
    return EventSpec(
        id=ev.get("api_id") or raw.get("api_id") or "",
        title=ev.get("name") or "",
        one_liner="",
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
