"""WP1 + WP5: push a RunResult into Notion as an event page + Approvals rows + hours dashboard.

push(result, visual_url="", ghost_url="") -> str   (page URL, or path to dry-run Markdown)
update_dashboard() -> None                          (running "Hours saved" callout on the parent page)

No token -> renders the same blocks as Markdown to out/<id>.md. API errors -> same fallback. Never crashes.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from . import config
from .models import RunResult

API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
MAX_TEXT = 2000       # rich_text content cap per text object
MAX_CHILDREN = 100    # children per append / create call
MAX_RICH_ITEMS = 100  # rich_text array length cap
STATE_PATH = config.OUT / "notion_state.json"
APPROVALS_TITLE = "Approvals"
DASHBOARD_ICON = "⏱️"

# PROJECT.md §9 baselines: task -> (manual minutes, with-us minutes)
BASELINES: dict[str, tuple[int, int]] = {
    "Run sheet": (45, 3),
    "Budget": (30, 2),
    "Volunteer rota": (60, 3),
    "Event page copy": (30, 2),
    "Visual": (30, 1),
    "Newsletter": (60, 4),
    "Channel posts": (30, 2),
}


def log(msg: str) -> None:
    print(f"notion: {msg}", flush=True)


# ---------------------------------------------------------------- state

def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_PATH.write_text(json.dumps(state, indent=2))
    except Exception as e:  # never crash on state
        log(f"could not save state ({e})")


# ---------------------------------------------------------------- rich text / block builders

def _u16(ch: str) -> int:
    return 2 if ord(ch) > 0xFFFF else 1


def _chunks(text: str, n: int = MAX_TEXT) -> list[str]:
    """Split so each piece is <= n UTF-16 units (Notion's limit; emoji count as 2)."""
    text = text or ""
    out, cur, size = [], [], 0
    for ch in text:
        w = _u16(ch)
        if size + w > n:
            out.append("".join(cur))
            cur, size = [], 0
        cur.append(ch)
        size += w
    out.append("".join(cur))
    return [c for c in out if c] or [""]


def rt(text: str, link: str = "", bold: bool = False) -> list[dict]:
    """Rich text array, chunked to the 2000-char limit."""
    out = []
    for c in _chunks(str(text)):
        t: dict[str, Any] = {"type": "text", "text": {"content": c}}
        if link:
            t["text"]["link"] = {"url": link}
        if bold:
            t["annotations"] = {"bold": True}
        out.append(t)
    return out[:MAX_RICH_ITEMS]


def _block(kind: str, rich: list[dict], **extra) -> dict:
    return {"object": "block", "type": kind, kind: {"rich_text": rich, **extra}}


def h2(text: str) -> dict:
    return _block("heading_2", rt(text))


def para(text: str = "", link: str = "") -> dict:
    return _block("paragraph", rt(text, link) if text else [])


def para_rich(rich: list[dict]) -> dict:
    return _block("paragraph", rich[:MAX_RICH_ITEMS])


def bullet_rich(rich: list[dict]) -> dict:
    return _block("bulleted_list_item", rich)


def bullet(text: str) -> dict:
    return _block("bulleted_list_item", rt(text))


def callout(text: str, emoji: str, color: str = "gray_background") -> dict:
    return _block("callout", rt(text), icon={"type": "emoji", "emoji": emoji}, color=color)


def divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def table(header: list[str], rows: list[list[str]]) -> dict:
    width = len(header)
    all_rows = [header] + [[str(c) for c in r] for r in rows]
    all_rows = all_rows[:MAX_CHILDREN]  # nested children cap
    children = [{
        "object": "block", "type": "table_row",
        "table_row": {"cells": [rt(c) if c != "" else [] for c in (r + [""] * width)[:width]]},
    } for r in all_rows]
    return {"object": "block", "type": "table", "table": {
        "table_width": width, "has_column_header": True, "has_row_header": False, "children": children}}


def image(url: str) -> dict:
    return {"object": "block", "type": "image", "image": {"type": "external", "external": {"url": url}}}


def _is_http(u: str) -> bool:
    return bool(u) and u.startswith(("http://", "https://"))


# ---------------------------------------------------------------- page content

def _money(x: float, cur: str = "GBP") -> str:
    sym = {"GBP": "£", "USD": "$", "EUR": "€"}.get(cur, "")
    return f"{sym}{x:,.2f}" if sym else f"{x:,.2f} {cur}"


def _num(x: float) -> str:
    return str(int(x)) if float(x).is_integer() else f"{x:g}"


def _glance(result: RunResult) -> str:
    s = result.spec
    date = s.date.strftime("%a %d %b %Y") if s.date else "date TBC"
    if s.start:
        date += f" · {s.start}" + (f"–{s.end}" if s.end else "")
    venue = ", ".join(p for p in [s.venue.name, s.venue.city] if p) or "venue TBC"
    return (f"{date} · {venue} · {s.expected_attendees or '?'} attendees · "
            f"budget {_money(s.budget_total, s.currency)} · {_money(s.per_head, s.currency)}/head")


def time_saved_lines() -> list[str]:
    return [f"{k}: {m} min manual → {u} min review" for k, (m, u) in BASELINES.items()]


def baseline_hours() -> float:
    return round(sum(m - u for m, u in BASELINES.values()) / 60, 1)


def build_blocks(result: RunResult, visual_url: str = "", ghost_url: str = "",
                 approvals_url: str = "", sections: list[tuple[str, str]] | None = None) -> list[dict]:
    s = result.spec
    cur = s.currency
    hours = result.hours_saved or baseline_hours()
    blocks: list[dict] = []

    if s.missing:
        blocks.append(callout("Needs you: " + "; ".join(s.missing), "❓", "yellow_background"))
    blocks.append(callout("At a glance: " + _glance(result), "📊", "blue_background"))
    blocks.append(callout(f"{_num(hours)} h of volunteer admin saved on this event", "⏱️", "green_background"))
    if s.one_liner or s.description:
        blocks.append(para(s.description or s.one_liner))
    if s.luma_url:
        blocks.append(para("Luma page ↗", s.luma_url))

    blocks.append(h2("🗓️ Run sheet"))
    blocks.append(table(["Time", "Duration", "Item", "Owner"],
                        [[x.time, f"{x.duration_min} min", x.item, x.owner] for x in s.run_sheet]))

    blocks.append(h2("💷 Budget"))
    rows = [[i.label, _num(i.qty), _money(i.unit_cost, i.currency or cur),
             _money(i.total, i.currency or cur), i.source or "—"] for i in s.budget]
    rows.append(["Total", "", "", _money(s.budget_total, cur), f"{_money(s.per_head, cur)}/head"])
    blocks.append(table(["Item", "Qty", "Unit", "Total", "Source"], rows))

    blocks.append(h2("🙋 Volunteer rota"))
    blocks.append(table(["Role", "Volunteer", "Start", "End"],
                        [[x.role, x.volunteer or "⚠️ unfilled", x.start, x.end] for x in s.rota]))

    blocks.append(h2("🧠 Community memory used"))
    if result.memories_used:
        for m in result.memories_used:
            blocks.append(bullet(f"{m.text} — from {m.provenance or 'community memory'}"))
    else:
        blocks.append(para("No memories recalled for this event."))

    blocks.append(h2("⏱️ Where the time went"))
    for line in time_saved_lines():
        blocks.append(bullet(line))

    blocks.append(h2("🎨 Visual & newsletter"))
    if _is_http(visual_url):
        blocks.append(image(visual_url))
    elif visual_url:
        blocks.append(para(f"Visual rendered locally: {visual_url}"))
    gurl = ghost_url or result.ghost_url
    if _is_http(gurl):
        blocks.append(para("📰 Newsletter draft in Ghost ↗", gurl))
    elif result.artifacts.newsletter_subject:
        blocks.append(para(f"📰 Newsletter subject: {result.artifacts.newsletter_subject}"))

    blocks.append(h2("✅ Approvals"))
    if sections:
        blocks.append(para("Every post is a Draft in 📣 Posts. Every cost is linked into 💷 Money."))
        for label, link in sections:
            blocks.append(bullet_rich(rt(label + " ↗", link, bold=True)))
    elif approvals_url:
        blocks.append(para_rich(rt("Flip Status → Approved for each artifact in the ") +
                                rt("Approvals database ↗", approvals_url, bold=True)))
    else:
        blocks.append(para("Approvals: Event page copy, Newsletter, LinkedIn, WhatsApp, Visual — all Draft."))
    return blocks


def approval_rows(result: RunResult, visual_url: str = "", ghost_url: str = "") -> list[dict]:
    a = result.artifacts
    gurl = ghost_url or result.ghost_url
    news = (a.newsletter_subject + "\n\n" if a.newsletter_subject else "") + re.sub(r"<[^>]+>", " ", a.newsletter_html)
    return [
        {"name": "Event page copy", "preview": a.page_copy, "image": "", "link": ""},
        {"name": "Newsletter", "preview": re.sub(r"[ \t]+", " ", news).strip(), "image": "", "link": gurl},
        {"name": "LinkedIn", "preview": a.linkedin, "image": "", "link": ""},
        {"name": "WhatsApp", "preview": a.whatsapp, "image": "", "link": ""},
        {"name": "Visual", "preview": visual_url or "(not rendered yet)",
         "image": visual_url if _is_http(visual_url) else "", "link": ""},
    ]


# ---------------------------------------------------------------- markdown (dry-run)

def _md_rich(rich: list[dict]) -> str:
    out = []
    for t in rich:
        c = t.get("text", {}).get("content", "")
        link = (t.get("text", {}).get("link") or {}).get("url")
        if t.get("annotations", {}).get("bold"):
            c = f"**{c}**"
        out.append(f"[{c}]({link})" if link else c)
    return "".join(out)


def to_markdown(title: str, blocks: list[dict], cover: str = "", extra: str = "") -> str:
    lines = [f"# {title}", ""]
    if cover:
        lines += [f"![cover]({cover})", ""]
    for b in blocks:
        k = b["type"]
        body = b[k]
        if k == "heading_2":
            lines += [f"## {_md_rich(body['rich_text'])}", ""]
        elif k == "paragraph":
            lines += [_md_rich(body["rich_text"]), ""]
        elif k == "callout":
            lines += [f"> {body['icon']['emoji']} {_md_rich(body['rich_text'])}", ""]
        elif k == "bulleted_list_item":
            lines.append(f"- {_md_rich(body['rich_text'])}")
        elif k == "divider":
            lines += ["---", ""]
        elif k == "image":
            lines += [f"![visual]({body['external']['url']})", ""]
        elif k == "table":
            rows = [[_md_rich(c).replace("|", "\\|") for c in r["table_row"]["cells"]] for r in body["children"]]
            if rows:
                lines.append("| " + " | ".join(rows[0]) + " |")
                lines.append("|" + "---|" * len(rows[0]))
                lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
            lines.append("")
        if k != "bulleted_list_item" and lines and lines[-1] != "":
            lines.append("")
    # close bullet lists
    out = "\n".join(lines)
    out = re.sub(r"(\n- [^\n]*)\n(?=[^-\n])", r"\1\n\n", out)
    return out + extra


def _dry_run(result: RunResult, visual_url: str, ghost_url: str) -> str:
    s = result.spec
    key = s.id or _slug(s.title)
    state = _load_state()
    ev = state.setdefault("events", {}).setdefault(key, {})
    ev.update({"title": s.title, "hours_saved": result.hours_saved or baseline_hours()})
    _save_state(state)
    total, n = _totals(state)

    rows = approval_rows(result, visual_url, ghost_url)
    extra = "\n## ✅ Approvals (database rows)\n\n| Name | Event | Status | Preview | Image | Link |\n|---|---|---|---|---|---|\n"
    for r in rows:
        prev = r["preview"].replace("\n", " ").replace("|", "\\|")
        prev = prev[:120] + ("…" if len(prev) > 120 else "")
        extra += f"| {r['name']} | {s.title} | Draft | {prev} | {r['image']} | {r['link']} |\n"
    extra += f"\n---\n\n> {DASHBOARD_ICON} Dashboard: Hours saved: {_num(total)} h across {n} event{'s' if n != 1 else ''}\n"

    md = to_markdown(f"📅 {s.title}", build_blocks(result, visual_url, ghost_url), s.cover_url, extra)
    path = config.OUT / f"{key}.md"
    path.write_text(md)
    log(f"wrote {path}")
    return str(path)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "event").lower()).strip("-") or "event"


def _totals(state: dict) -> tuple[float, int]:
    evs = state.get("events", {})
    return round(sum(float(e.get("hours_saved", 0) or 0) for e in evs.values()), 1), len(evs)


# ---------------------------------------------------------------- Notion HTTP

class NotionError(Exception):
    pass


class Notion:
    def __init__(self, token: str):
        self.c = httpx.Client(base_url=API, timeout=30, headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })

    def req(self, method: str, path: str, body: dict | None = None) -> dict:
        for attempt in range(5):
            try:
                r = self.c.request(method, path, json=body)
            except httpx.HTTPError as e:
                if attempt == 4:
                    raise NotionError(f"{method} {path}: {e}") from e
                time.sleep(1 + attempt)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                wait = float(r.headers.get("Retry-After", 1 + attempt))
                log(f"{r.status_code} on {method} {path}, retry in {wait}s")
                time.sleep(min(wait, 30))
                continue
            if r.status_code >= 400:
                raise NotionError(f"{method} {path} → {r.status_code}: {r.text[:500]}")
            return r.json()
        raise NotionError(f"{method} {path}: gave up after retries")

    def append(self, block_id: str, children: list[dict], after: str = "") -> list[dict]:
        out = []
        for i in range(0, len(children), MAX_CHILDREN):
            body: dict[str, Any] = {"children": children[i:i + MAX_CHILDREN]}
            if after and i == 0:
                body["after"] = after
            out += self.req("PATCH", f"/blocks/{block_id}/children", body).get("results", [])
        return out

    def archive_page(self, page_id: str) -> None:
        try:
            self.req("PATCH", f"/pages/{page_id}", {"archived": True})
        except NotionError as e:
            log(f"archive {page_id} skipped ({e})")

    def children(self, block_id: str) -> list[dict]:
        out, cursor = [], ""
        while True:
            q = f"?page_size=100{'&start_cursor=' + cursor if cursor else ''}"
            res = self.req("GET", f"/blocks/{block_id}/children{q}")
            out += res.get("results", [])
            cursor = res.get("next_cursor") or ""
            if not res.get("has_more") or not cursor:
                return out


def _norm(i: str) -> str:
    return (i or "").replace("-", "").lower()


APPROVALS_PROPS = {
    "Name": {"title": {}},
    "Event": {"rich_text": {}},
    "Status": {"select": {"options": [
        {"name": "Draft", "color": "yellow"},
        {"name": "Approved", "color": "green"},
        {"name": "Rejected", "color": "red"},
    ]}},
    "Preview": {"rich_text": {}},
    "Image": {"url": {}},
    "Link": {"url": {}},
}


def _approvals_db(n: Notion, state: dict) -> tuple[str, str]:
    parent = config.NOTION_PARENT_PAGE_ID
    db_id = state.get("approvals_db_id", "")
    if db_id:
        try:
            db = n.req("GET", f"/databases/{db_id}")
            if not db.get("archived"):
                missing = {k: v for k, v in APPROVALS_PROPS.items() if k not in db.get("properties", {})}
                if missing:
                    n.req("PATCH", f"/databases/{db_id}", {"properties": missing})
                return db_id, db.get("url", "")
        except NotionError:
            pass
    # Look for an existing child database titled "Approvals" under the parent page.
    try:
        res = n.req("POST", "/search", {"query": APPROVALS_TITLE,
                                        "filter": {"property": "object", "value": "database"}})
        for db in res.get("results", []):
            title = "".join(t.get("plain_text", "") for t in db.get("title", []))
            if (title.strip() == APPROVALS_TITLE and not db.get("archived")
                    and _norm(db.get("parent", {}).get("page_id", "")) == _norm(parent)):
                missing = {k: v for k, v in APPROVALS_PROPS.items() if k not in db.get("properties", {})}
                if missing:
                    n.req("PATCH", f"/databases/{db['id']}", {"properties": missing})
                state["approvals_db_id"] = db["id"]
                return db["id"], db.get("url", "")
    except NotionError as e:
        log(f"search failed ({e}); creating Approvals DB")
    db = n.req("POST", "/databases", {
        "parent": {"type": "page_id", "page_id": parent},
        "icon": {"type": "emoji", "emoji": "✅"},
        "title": rt(APPROVALS_TITLE),
        "properties": APPROVALS_PROPS,
    })
    state["approvals_db_id"] = db["id"]
    log(f"created Approvals DB {db.get('url', db['id'])}")
    return db["id"], db.get("url", "")


PAGE_PREFIX = "📅 "
_HOURS_RE = re.compile(r"([\d.]+) h of volunteer admin saved")


def _plain(block: dict) -> str:
    return "".join(t.get("plain_text", "") for t in block.get(block["type"], {}).get("rich_text", []))


def _sync_from_notion(n: Notion, state: dict) -> dict[str, str]:
    """Rebuild state from what is actually on the parent page, so several machines can push
    to the same workspace without duplicating pages or the dashboard. Returns {title: [page_id, ...]}."""
    kids = n.children(config.NOTION_PARENT_PAGE_ID)
    dash = next((b["id"] for b in kids if b["type"] == "callout"
                 and _plain(b).startswith("Hours saved:")), "")
    if dash:
        state["dashboard_block_id"] = dash
    pages: dict[str, list[str]] = {}
    for b in kids:
        if b["type"] == "child_page" and b["child_page"]["title"].startswith(PAGE_PREFIX):
            pages.setdefault(b["child_page"]["title"].removeprefix(PAGE_PREFIX), []).append(b["id"])
    known = {e.get("page_id"): (k, e) for k, e in state.get("events", {}).items() if e.get("page_id")}
    events = {}
    for title, pids in pages.items():
        pid = pids[-1]
        if pid in known:
            k, e = known[pid]
            events[k] = e
            continue
        hours = 0.0  # pushed from another machine: read its hours off the page
        for b in n.children(pid)[:6]:
            m = b["type"] == "callout" and _HOURS_RE.search(_plain(b))
            if m:
                hours = float(m.group(1))
                break
        events[f"notion:{_norm(pid)}"] = {"page_id": pid, "title": title, "hours_saved": hours}
    state["events"] = events  # dry-run entries and pages deleted in Notion drop out
    return pages


def _archive_rows_for(n: Notion, db_id: str, title: str) -> None:
    try:
        res = n.req("POST", f"/databases/{db_id}/query", {
            "filter": {"property": "Event", "rich_text": {"equals": title}}, "page_size": 100})
        for row in res.get("results", []):
            if not (row.get("archived") or row.get("in_trash")):
                n.archive_page(row["id"])
    except NotionError as e:
        log(f"could not clear old approval rows ({e})")


def _dashboard_text(state: dict) -> str:
    total, n = _totals(state)
    return f"Hours saved: {_num(total)} h across {n} event{'s' if n != 1 else ''}"


def _ensure_dashboard(n: Notion, state: dict) -> None:
    """Create the dashboard callout on the parent page if it doesn't exist yet (before the first event page,
    so it sits at the top). Notion's API can only append, so 'top' relies on it being created first."""
    bid = state.get("dashboard_block_id", "")
    if bid:
        try:
            b = n.req("GET", f"/blocks/{bid}")
            if not b.get("archived") and not b.get("in_trash"):
                return
        except NotionError:
            pass
    blk = callout(_dashboard_text(state), DASHBOARD_ICON, "green_background")
    blk["callout"]["rich_text"][0]["annotations"] = {"bold": True}
    res = n.append(config.NOTION_PARENT_PAGE_ID, [blk])
    if res:
        state["dashboard_block_id"] = res[0]["id"]


def update_dashboard() -> None:
    """Refresh the running 'Hours saved' callout on the parent page in place."""
    state = _load_state()
    if not config.HAS_NOTION:
        total, n = _totals(state)
        log(f"dashboard (dry-run): {_dashboard_text(state)}")
        return
    try:
        n = Notion(config.NOTION_TOKEN)
        _ensure_dashboard(n, state)
        rich = rt(_dashboard_text(state), bold=True)
        n.req("PATCH", f"/blocks/{state['dashboard_block_id']}", {"callout": {
            "rich_text": rich, "icon": {"type": "emoji", "emoji": DASHBOARD_ICON}, "color": "green_background"}})
        _save_state(state)
        log(f"dashboard → {_dashboard_text(state)}")
    except Exception as e:
        log(f"dashboard update failed ({e})")


# ---------------------------------------------------------------- push

def push(result: RunResult, visual_url: str = "", ghost_url: str = "", banner_url: str = "") -> str:
    """Create (or archive + recreate) the event page in Notion. Returns the page URL, or a Markdown path in dry-run."""
    if not config.HAS_NOTION:
        log("dry-run (no token)")
        return _dry_run(result, visual_url, ghost_url)
    try:
        return _push_live(result, visual_url, ghost_url, banner_url)
    except Exception as e:
        log(f"API error, falling back to dry-run ({e})")
        return _dry_run(result, visual_url, ghost_url)


def _push_live(result: RunResult, visual_url: str, ghost_url: str, banner_url: str = "") -> str:
    s = result.spec
    key = s.id or _slug(s.title)
    n = Notion(config.NOTION_TOKEN)
    state = _load_state()
    pages = _sync_from_notion(n, state)
    events = state["events"]

    from . import workspace  # imported here: workspace imports this module

    _ensure_dashboard(n, state)
    ws = workspace.ensure(n, state)
    _save_state(state)

    # Idempotency: archive the previous page for this event, whoever pushed it.
    title = s.title or "Untitled event"
    for k in [k for k, e in events.items() if e.get("title") == title or k == key]:
        old = events.pop(k)
        if old.get("page_id"):
            log(f"archiving previous page for {title}")
            n.archive_page(old["page_id"])
        for rid in old.get("approval_row_ids", []):
            n.archive_page(rid)
    for pid in pages.get(title, []):
        n.archive_page(pid)

    sections = workspace.section_links(ws) + [("🙋 Volunteer sign-up form", workspace.signup_url(title))]
    blocks = build_blocks(result, visual_url, ghost_url, sections=sections)
    page: dict[str, Any] = {
        "parent": {"type": "page_id", "page_id": config.NOTION_PARENT_PAGE_ID},
        "properties": {"title": {"title": rt(f"{PAGE_PREFIX}{title}")}},
        "children": blocks[:MAX_CHILDREN],
    }
    if _is_http(s.cover_url):
        page["cover"] = {"type": "external", "external": {"url": s.cover_url}}
    created = n.req("POST", "/pages", page)
    page_id, url = created["id"], created.get("url", "")
    if len(blocks) > MAX_CHILDREN:
        n.append(page_id, blocks[MAX_CHILDREN:])
    log(f"page created {url}")

    workspace.populate(n, ws, result, visual_url, banner_url)
    row_ids: list[str] = []

    events[key] = {"page_id": page_id, "url": url, "title": s.title,
                   "hours_saved": result.hours_saved or baseline_hours(), "approval_row_ids": row_ids}
    _save_state(state)
    update_dashboard()
    return url


if __name__ == "__main__":
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else config.ROOT / "seed" / "sample_run.json"
    res = RunResult.model_validate_json(src.read_text())
    print(push(res, *(sys.argv[2:4])))
