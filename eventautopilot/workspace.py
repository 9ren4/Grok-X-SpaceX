"""The Notion workspace: five sections, with Money as the hub every other table links to.

    💷 Money          Money ledger
    🤝 Sponsorship    Sponsors
    🙋 Volunteers     Volunteer shifts
    🏛️ Venues         Spots · Food suppliers
    📣 Posts          Newsletter · Posters · Event page · Social posts (Discord, LinkedIn, WhatsApp group)

ensure() finds or creates all of it under NOTION_PARENT_PAGE_ID (safe to call from any machine);
populate() writes one event's rows, replacing that event's previous rows.
"""
from __future__ import annotations

import re
from typing import Any

from . import config
from .models import RunResult
from .notion_sync import Notion, NotionError, _norm, callout, log, rt

SECTIONS = {  # key -> (icon, page title)
    "money": ("💷", "Money"),
    "sponsorship": ("🤝", "Sponsorship"),
    "volunteers": ("🙋", "Volunteers"),
    "venues": ("🏛️", "Venues"),
    "posts": ("📣", "Posts"),
}

GBP = {"number": {"format": "pound"}}
TEXT = {"rich_text": {}}


def _select(*names: str) -> dict:
    return {"select": {"options": [{"name": n} for n in names]}}


POST_STATUS = _select("Draft", "Approved", "Rejected", "Published")
POST_COMMON = {"Event": TEXT, "Status": POST_STATUS, "Content": TEXT,
               "Paid promotion": {"checkbox": {}}, "Cost £": GBP}

# key -> (section, title, icon, properties). "Money" relations are added after the ledger exists.
TABLES: dict[str, tuple[str, str, str, dict]] = {
    "money": ("money", "Money ledger", "💷", {
        "Event": TEXT,
        "Kind": _select("Expense", "Income"),
        "Source": _select("Sponsorship", "Venue", "Food", "Volunteers", "Posts", "Other"),
        "Estimated £": GBP, "Committed £": GBP, "Paid £": GBP,
        "Status": _select("Estimated", "Committed", "Invoiced", "Partially paid", "Paid", "Cancelled"),
        "Where the number came from": TEXT,
    }),
    "sponsors": ("sponsorship", "Sponsors", "🤝", {
        "Event": TEXT,
        "Stage": _select("Prospect", "Pitched", "Negotiating", "Confirmed", "Lost"),
        "Amount £": GBP,
        "Payment": _select("Expected", "Invoiced", "Partially received", "Received", "Overdue"),
        "Contact": TEXT,
    }),
    "volunteers": ("volunteers", "Volunteer shifts", "🙋", {
        "Event": TEXT, "Role": TEXT, "Start": TEXT, "End": TEXT,
        "Expenses £": GBP,
        "Reimbursement": _select("None", "Pending approval", "Approved", "Paid", "Rejected"),
    }),
    "spots": ("venues", "Spots", "📍", {
        "Event": TEXT,
        "Status": _select("Considering", "Shortlisted", "Confirmed", "Rejected"),
        "City": TEXT, "Capacity": {"number": {}}, "Hire £": GBP, "Deposit £": GBP,
        "Where the number came from": TEXT,
    }),
    "food": ("venues", "Food suppliers", "🍕", {
        "Event": TEXT, "Qty": {"number": {}}, "Unit £": GBP, "Total £": GBP,
        "Status": _select("Estimated", "Ordered", "Paid"),
        "Where the number came from": TEXT,
    }),
    "newsletter": ("posts", "Newsletter", "📰", {**POST_COMMON, "Subject": TEXT}),
    "posters": ("posts", "Posters", "🖼️", {**POST_COMMON, "Image": {"url": {}},
                                           "Format": _select("Square 1080×1080", "LinkedIn banner 1200×627")}),
    "event_page": ("posts", "Event page", "📄", {**POST_COMMON}),
    "social": ("posts", "Social posts", "💬", {**POST_COMMON,
                                              "Channel": _select("Discord", "LinkedIn", "WhatsApp group"),
                                              "Image": {"url": {}}}),
}

FOOD = re.compile(r"pizza|food|drink|cater|coffee|tea\b|snack|pastr|breakfast|lunch|dinner|beverage|beer|wine", re.I)
VENUE = re.compile(r"venue|hire|room|space|hall", re.I)
PROMO = re.compile(r"print|poster|signage|flyer|design|ads?\b|advert|promo", re.I)


def _title_of(obj: dict) -> str:
    t = obj.get("child_page", {}).get("title") or obj.get("child_database", {}).get("title")
    return t or "".join(x.get("plain_text", "") for x in obj.get("title", []))


def ensure(n: Notion, state: dict) -> dict[str, Any]:
    """Find or create the five section pages and their tables. Returns state['ws']."""
    ws = state.setdefault("ws", {})
    parent = config.NOTION_PARENT_PAGE_ID
    kids = {_title_of(b): b["id"] for b in n.children(parent) if b["type"] == "child_page"}
    pages = {}
    for key, (icon, title) in SECTIONS.items():
        pid = kids.get(title)
        if not pid:
            body: dict[str, Any] = {"parent": {"page_id": parent}, "icon": {"type": "emoji", "emoji": icon},
                                    "properties": {"title": {"title": rt(title)}}}
            if key == "money":  # summary callout first, so it sits above the ledger
                body["children"] = [callout("Projected spend: filled in after the first event", "💷",
                                            "green_background")]
            pid = n.req("POST", "/pages", body)["id"]
            log(f"created section {title}")
        pages[key] = pid
    ws["pages"] = pages

    dbs: dict[str, str] = {}
    existing: dict[str, dict[str, str]] = {}
    for key, pid in pages.items():
        existing[key] = {_title_of(b): b["id"] for b in n.children(pid) if b["type"] == "child_database"}
    for key in ["money", *[k for k in TABLES if k != "money"]]:  # ledger first, others relate to it
        section, title, icon, props = TABLES[key]
        db_id = existing[section].get(title)
        if not db_id:
            props = {"Name": {"title": {}}, **props}
            if key != "money":
                props["Money"] = {"relation": {"database_id": dbs["money"], "type": "dual_property",
                                               "dual_property": {}}}
            db_id = n.req("POST", "/databases", {
                "parent": {"type": "page_id", "page_id": pages[section]},
                "icon": {"type": "emoji", "emoji": icon}, "title": rt(title), "properties": props})["id"]
            log(f"created table {title}")
        dbs[key] = db_id
    ws["dbs"] = dbs
    return ws


def _clear(n: Notion, db_id: str, event: str) -> None:
    try:
        res = n.req("POST", f"/databases/{db_id}/query",
                    {"filter": {"property": "Event", "rich_text": {"equals": event}}, "page_size": 100})
        for row in res.get("results", []):
            if not (row.get("archived") or row.get("in_trash")):
                n.archive_page(row["id"])
    except NotionError as e:
        log(f"could not clear old rows ({e})")


def _row(n: Notion, db_id: str, props: dict) -> str:
    try:
        return n.req("POST", "/pages", {"parent": {"database_id": db_id}, "properties": props})["id"]
    except NotionError as e:
        log(f"row failed ({e})")
        return ""


def _t(text: str) -> dict:
    return {"rich_text": rt(text or "")}


def _sel(name: str) -> dict:
    return {"select": {"name": name}}


def _rel(*ids: str) -> dict:
    return {"relation": [{"id": i} for i in ids if i]}


def _plain(html: str) -> str:
    text = re.sub(r"</(p|h\d|li)>", "\n\n", html or "")
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"<[^>]+>", "", text)).strip()


def populate(n: Notion, ws: dict, result: RunResult, visual_url: str = "", banner_url: str = "") -> None:
    s, a = result.spec, result.artifacts
    ev = s.title or "Untitled event"
    dbs = ws["dbs"]
    for db_id in dbs.values():
        _clear(n, db_id, ev)

    def name(text: str) -> dict:
        return {"title": rt(text)}

    # Money first: one ledger line per budget line, tagged with the module it belongs to.
    lines = []  # (item, money_row_id, source)
    for item in s.budget:
        src = ("Food" if FOOD.search(item.label) else "Venue" if VENUE.search(item.label)
               else "Posts" if PROMO.search(item.label) else "Other")
        mid = _row(n, dbs["money"], {
            "Name": name(item.label), "Event": _t(ev), "Kind": _sel("Expense"), "Source": _sel(src),
            "Estimated £": {"number": item.total}, "Status": _sel("Estimated"),
            "Where the number came from": _t(item.source or "estimate")})
        lines.append((item, mid, src))

    # Venues: the spot, plus food suppliers, each linked to its ledger line.
    venue_lines = [(i, m) for i, m, src in lines if src == "Venue"]
    hire = sum(i.total for i, _ in venue_lines)
    _row(n, dbs["spots"], {
        "Name": name(s.venue.name or "Venue TBC"), "Event": _t(ev),
        "Status": _sel("Confirmed" if s.venue.name else "Considering"),
        "City": _t(s.venue.city), "Capacity": {"number": s.venue.capacity},
        "Hire £": {"number": hire if venue_lines else None},
        "Where the number came from": _t(", ".join(i.source or "estimate" for i, _ in venue_lines)),
        "Money": _rel(*(m for _, m in venue_lines))})
    for item, mid, src in lines:
        if src == "Food":
            _row(n, dbs["food"], {
                "Name": name(item.label), "Event": _t(ev), "Qty": {"number": item.qty},
                "Unit £": {"number": item.unit_cost}, "Total £": {"number": item.total},
                "Status": _sel("Estimated"), "Where the number came from": _t(item.source or "estimate"),
                "Money": _rel(mid)})

    # Volunteers: shifts carry no money unless someone claims expenses.
    for sh in s.rota:
        _row(n, dbs["volunteers"], {
            "Name": name(sh.volunteer or "⚠️ Unfilled"), "Event": _t(ev), "Role": _t(sh.role),
            "Start": _t(sh.start), "End": _t(sh.end), "Reimbursement": _sel("None")})

    # Posts: every artifact is a Draft waiting for the lead's approval.
    promo = [m for _, m, src in lines if src == "Posts"]
    draft = _sel("Draft")
    _row(n, dbs["newsletter"], {"Name": name(a.newsletter_subject or ev), "Event": _t(ev), "Status": draft,
                                "Subject": _t(a.newsletter_subject), "Content": _t(_plain(a.newsletter_html))})
    _row(n, dbs["event_page"], {"Name": name(f"{ev} — event page"), "Event": _t(ev), "Status": draft,
                                "Content": _t(a.page_copy)})
    for fmt, url in (("Square 1080×1080", visual_url), ("LinkedIn banner 1200×627", banner_url)):
        _row(n, dbs["posters"], {"Name": name(f"{ev} — {fmt.split(' ')[0].lower()}"), "Event": _t(ev),
                                 "Status": draft, "Format": _sel(fmt), "Image": {"url": url or None},
                                 "Content": _t("" if url else "Rendered locally; no public URL yet"),
                                 "Money": _rel(*promo)})
    for channel, text, img in (("LinkedIn", a.linkedin, banner_url or visual_url),
                               ("WhatsApp group", a.whatsapp, visual_url),
                               ("Discord", a.whatsapp, visual_url)):
        _row(n, dbs["social"], {"Name": name(f"{ev} — {channel}"), "Event": _t(ev), "Status": draft,
                                "Channel": _sel(channel), "Content": _t(text), "Image": {"url": img or None}})

    _money_summary(n, ws)
    log(f"workspace rows written for {ev}")


def _money_summary(n: Notion, ws: dict) -> None:
    """Callout at the top of the Money page: projected spend by source, across all events."""
    totals: dict[str, float] = {}
    cursor = ""
    try:
        while True:
            body: dict[str, Any] = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            res = n.req("POST", f"/databases/{ws['dbs']['money']}/query", body)
            for row in res.get("results", []):
                p = row["properties"]
                if (p["Status"].get("select") or {}).get("name") == "Cancelled":
                    continue
                src = (p["Source"].get("select") or {}).get("name", "Other")
                totals[src] = totals.get(src, 0) + (p["Estimated £"].get("number") or 0)
            cursor = res.get("next_cursor") or ""
            if not res.get("has_more"):
                break
        spend = sum(v for k, v in totals.items() if k != "Sponsorship")
        parts = " · ".join(f"{k} £{v:,.2f}" for k, v in sorted(totals.items(), key=lambda kv: -kv[1]))
        text = f"Projected spend £{spend:,.2f} across all events — {parts}"
        page = ws["pages"]["money"]
        found = next((b for b in n.children(page) if b["type"] == "callout"), None)
        blk = callout(text, "💷", "green_background")
        if found:
            n.req("PATCH", f"/blocks/{found['id']}", {"callout": blk["callout"]})
        else:
            n.append(page, [blk])
    except (NotionError, KeyError) as e:
        log(f"money summary skipped ({e})")


def section_links(ws: dict) -> list[tuple[str, str]]:
    return [(f"{icon} {title}", f"https://www.notion.so/{_norm(ws['pages'][key])}")
            for key, (icon, title) in SECTIONS.items() if key in ws.get("pages", {})]
