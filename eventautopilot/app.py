"""Chat front page: type an idea or paste a Luma link, watch the run, see everything in one place.

    python -m eventautopilot.app      ->  http://localhost:8000
"""
from __future__ import annotations

import json
import queue
import re
import threading
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, pipeline

WEB = config.ROOT / "web"
app = FastAPI(title="Event Autopilot")
app.mount("/out", StaticFiles(directory=config.OUT), name="out")
app.mount("/graph", StaticFiles(directory=config.ROOT / "graph", html=True), name="graph")

_busy = threading.Lock()
_sink: queue.Queue | None = None


class _StreamLog(pipeline._Log):
    """Pipeline log that also forwards each line to the open browser stream."""

    def add(self, msg: str, since: float | None = None) -> None:
        super().add(msg, since)
        if _sink is not None:
            _sink.put({"type": "log", "line": self.lines[-1]})


pipeline._Log = _StreamLog


class RunIn(BaseModel):
    message: str = ""
    brief: dict | None = None


class IntakeIn(BaseModel):
    history: list[dict]
    brief: dict = {}


def _out_url(path: str) -> str:
    p = Path(path)
    try:
        return "/out/" + p.resolve().relative_to(config.OUT.resolve()).as_posix()
    except ValueError:
        return ""


def _links(result) -> dict:
    lines = result.log
    find = lambda pat: next((m.group(1) for ln in reversed(lines) if (m := re.search(pat, ln))), "")
    notion = find(r"\bnotion: (\S+)")
    visual = find(r"\bvisual: (/\S+\.png)")
    sid = pipeline._safe_id(result.spec.id)
    banner = next(config.OUT.glob(f"banner_*{sid}*.png"), None)
    news = next(config.OUT.glob(f"newsletter_*{sid}*.html"), None)
    return {
        "notion": notion if notion.startswith("http") else "",
        "notion_md": _out_url(notion) if notion and not notion.startswith("http") else "",
        "visual": _out_url(visual) if visual else "",
        "banner": _out_url(str(banner)) if banner else "",
        "newsletter": _out_url(str(news)) if news else "",
        "ghost": result.ghost_url or find(r"\bghost: (https?://\S+)"),
        "graph": "/graph/index.html",
    }


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB / "index.html")


@app.get("/api/status")
def status() -> dict:
    return {**config.status(), "notion": config.HAS_NOTION}


@app.post("/api/intake")
def intake_turn(body: IntakeIn) -> dict:
    from . import intake
    before = intake.Brief.model_validate(body.brief or {})
    last = next((m["content"] for m in reversed(body.history) if m.get("role") == "user"), "")
    luma_info = None
    m = intake.LUMA_LINK.search(last)
    if m and m.group(0) != before.luma_url:  # check who's registered before Grok asks about headcount
        before.luma_url = m.group(0) if "://" in m.group(0) else "https://" + m.group(0)
        got = intake.luma_registrations(before.luma_url)
        if got:  # what Luma already knows is not asked again
            before.luma_registered, before.luma_title = got["registered"], got["title"]
            before.title = before.title or got["title"]
            before.when = before.when or (got["start"] + " UTC" if got["start"] else "")
            before.venue_name = before.venue_name or got["venue"]
            before.venue_city = before.venue_city or got["city"]
            luma_info = {**got, "url": before.luma_url}
    asked = next((m["content"] for m in reversed(body.history) if m.get("role") == "assistant"), "")
    topic = intake.current_topic(before, asked)
    if topic in ("venue", "food") and intake.HELP.search(last):
        # They asked us to look: stay on this question and show options instead of moving on.
        opts = intake.suggest_options(topic, before)
        heads = before.attendees or before.luma_registered
        what = "food and drink" if topic == "food" else "venues"
        reply = (f"Here's what your community has used before{f' for {heads} people' if heads else ''}. "
                 f"Pick one, tell me something else, or say skip." if opts else
                 f"I couldn't find any past {what} in your community's records. Search online below, "
                 f"tell me what you have in mind, or say skip.")
        return {"reply": reply, "brief": before.model_dump(), "done": False, "luma": luma_info,
                "helper": None, "options": {"topic": topic, "items": opts,
                                            "search_url": "https://www.google.com/search?q=" +
                                            ("catering+" if topic == "food" else "event+venue+hire+") +
                                            (before.venue_city or "London").replace(" ", "+")}}
    helper = None
    if topic in ("venue", "food") and intake.UNSURE.search(last):
        # They said what it is but not the number: let the venue/food answer land first, then look it up.
        probe = intake.step(body.history, before) if not (before.venue_name if topic == "venue" else before.food) else None
        b2 = probe.brief if probe else before
        helper = intake.help_unsure(topic, b2)
    turn = intake.step(body.history, before, lookup=helper["summary"] if helper else "")
    out = turn.model_dump()
    out["luma"] = luma_info
    out["helper"] = helper
    b = turn.brief
    if b.venue_name and b.venue_name != before.venue_name:  # new venue mentioned: find it on the map
        out["venue_match"] = intake.lookup_venue(b.venue_name, b.venue_city)
    return out


class DraftIn(BaseModel):
    topic: str
    brief: dict = {}
    contact: str = ""


@app.post("/api/draft")
def draft(body: DraftIn) -> dict:
    from . import intake
    d = intake.draft_message(body.topic, intake.Brief.model_validate(body.brief or {}), body.contact)
    return {**d.model_dump(), "contact": body.contact}


@app.post("/api/run")
def run(body: RunIn):
    if not _busy.acquire(blocking=False):
        return JSONResponse({"error": "A run is already in progress. Give it a few seconds."}, 409)
    global _sink
    q: queue.Queue = queue.Queue()
    _sink = q

    def work() -> None:
        global _sink
        try:
            from . import intake
            brief = intake.Brief.model_validate(body.brief) if body.brief else None
            source = intake.to_source(brief) if brief and not body.message else body.message
            res = pipeline.run(source, brief=brief)
            q.put({"type": "result", "result": json.loads(res.model_dump_json()), "links": _links(res)})
        except pipeline.PipelineError as e:
            q.put({"type": "error", "error": str(e)})
        except Exception as e:  # noqa: BLE001
            q.put({"type": "error", "error": f"{type(e).__name__}: {e}"})
        finally:
            _sink = None
            q.put(None)
            _busy.release()

    threading.Thread(target=work, daemon=True).start()

    def stream():
        while (item := q.get()) is not None:
            yield json.dumps(item) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
