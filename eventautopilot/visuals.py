"""WP2 · Visuals: render a branded event PNG from a RunResult and upload it.

render(result, size="square"|"banner") -> Path   (out/visual_{id}.png, out/banner_{id}.png)
upload(path) -> str                               (public Supabase URL, or "" if unavailable)

Data is handed to the template as a JS argument (page.evaluate) and written with
textContent only, so no event field can inject markup or script.
"""
from __future__ import annotations

import re
from pathlib import Path

from .config import HAS_SUPABASE, OUT, ROOT, SUPABASE_SERVICE_KEY, SUPABASE_URL
from .models import RunResult

TEMPLATES = ROOT / "visuals" / "templates"
SIZES = {
    "square": ("square.html", 1080, 1080, "visual"),
    "banner": ("banner.html", 1200, 627, "banner"),
}
BUCKET = "visuals"
_SYMBOLS = {"GBP": "£", "USD": "$", "EUR": "€"}


def _join_names(names: list[str]) -> str:
    names = [n for n in names if n]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " & " + names[-1]


def _price(spec) -> str:
    p = spec.ticket_price
    if not p:
        return "Free"
    sym = _SYMBOLS.get((spec.currency or "GBP").upper(), "")
    amount = f"{p:.0f}" if float(p).is_integer() else f"{p:.2f}"
    return f"{sym}{amount}" if sym else f"{amount} {spec.currency}"


def _payload(result: RunResult) -> dict:
    s = result.spec
    date = s.date.strftime("%a %-d %b") if s.date else ""
    start = s.start or (s.date.strftime("%H:%M") if s.date and (s.date.hour or s.date.minute) else "")
    time = f"{start}–{s.end}" if start and s.end else start
    date_line = f"{date} · {start}" if date and start else (date or start)
    return {
        "title": s.title or "Untitled event",
        "format": s.format or "",
        "date": date_line or "Date TBC",
        "time": time if s.end else "",
        "venue": s.venue.name or "Venue TBC",
        "city": s.venue.city or "",
        "price": _price(s),
        "hosts": _join_names([h.name for h in s.hosts]),
        "url": re.sub(r"^https?://(www\.)?", "", s.luma_url or "").rstrip("/"),
        "cover_url": s.cover_url if (s.cover_url or "").startswith(("http://", "https://")) else "",
    }


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-") or "event"


def render(result: RunResult, size: str = "square") -> Path:
    """Render the event visual at exact pixel size and return the PNG path."""
    from playwright.sync_api import sync_playwright

    template, w, h, prefix = SIZES[size]
    out = OUT / f"{prefix}_{_slug(result.spec.id or result.spec.title)}.png"
    data = _payload(result)
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": w, "height": h}, device_scale_factor=1)
            try:
                page.goto((TEMPLATES / template).as_uri(), wait_until="load", timeout=15000)
            except Exception:
                # Fonts CSS unreachable (offline): page still works with system fallback.
                pass
            page.evaluate("d => window.render(d)", data)
            page.wait_for_selector("body[data-ready='1']", state="attached", timeout=20000)
            page.screenshot(path=str(out), clip={"x": 0, "y": 0, "width": w, "height": h})
        finally:
            browser.close()
    return out


def upload(path: Path) -> str:
    """Upload a PNG to the public Supabase bucket. Returns its public URL, or "" on any failure."""
    if not HAS_SUPABASE:
        print("visuals: upload skipped (no supabase)")
        return ""
    try:
        import httpx

        path = Path(path)
        name = path.name
        auth = {"Authorization": f"Bearer {SUPABASE_SERVICE_KEY}", "apikey": SUPABASE_SERVICE_KEY}
        obj_url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{name}"
        headers = {**auth, "Content-Type": "image/png", "x-upsert": "true"}
        body = path.read_bytes()
        with httpx.Client(timeout=30) as c:
            r = c.post(obj_url, headers=headers, content=body)
            if r.status_code >= 400 and ("bucket" in r.text.lower() or r.status_code == 404):
                c.post(f"{SUPABASE_URL}/storage/v1/bucket", headers=auth,
                       json={"id": BUCKET, "name": BUCKET, "public": True})
                r = c.post(obj_url, headers=headers, content=body)
            if r.status_code >= 400:
                print(f"visuals: upload failed ({r.status_code}) {r.text[:200]}")
                return ""
        return f"{SUPABASE_URL}/storage/v1/object/public/{BUCKET}/{name}"
    except Exception as e:  # never crash the pipeline
        print(f"visuals: upload failed ({e})")
        return ""
