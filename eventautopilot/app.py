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
    turn = intake.step(body.history, intake.Brief.model_validate(body.brief or {}))
    return turn.model_dump()


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
