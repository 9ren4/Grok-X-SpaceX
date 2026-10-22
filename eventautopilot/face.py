"""Aryan's side in one call: RunResult -> visual, Ghost draft, Notion page.

Mehedi's pipeline calls `publish(result)` at the end of a run. Every step
degrades on its own, so one missing key never stops the rest.
"""
from __future__ import annotations

import sys
from pathlib import Path

from .models import RunResult


def publish(result: RunResult, visual_path: Path | None = None) -> dict:
    """Pass visual_path when the square visual is already rendered, to skip re-rendering."""
    out = {"visual_path": "", "visual_url": "", "banner_url": "", "ghost_url": "", "notion_url": ""}

    try:
        from . import visuals
        path = Path(visual_path) if visual_path else visuals.render(result)
        out["visual_path"] = str(path)
        brief = result.spec.raw.get("brief")
        upload = visuals.upload
        if brief is not None and brief.store_consent is False:  # organiser declined storage
            print("visuals: upload skipped (organiser chose not to save)")
            upload = lambda _p: ""  # noqa: E731
        out["visual_url"] = upload(path)
        try:
            out["banner_url"] = upload(visuals.render(result, size="banner"))
        except Exception as e:  # banner is a stretch, never block on it
            print(f"visuals: banner failed ({e})")
    except Exception as e:
        print(f"visuals: failed ({e})")

    try:
        from . import ghost
        out["ghost_url"] = ghost.create_draft(result, feature_image=out["visual_url"])
        if out["ghost_url"]:
            result.ghost_url = out["ghost_url"]
    except Exception as e:
        print(f"ghost: failed ({e})")

    try:
        from . import notion_sync
        out["notion_url"] = notion_sync.push(
            result, visual_url=out["visual_url"], ghost_url=out["ghost_url"], banner_url=out["banner_url"]
        )
    except Exception as e:
        print(f"notion: failed ({e})")

    return out


if __name__ == "__main__":
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "seed/sample_run.json")
    res = publish(RunResult.model_validate_json(src.read_text()))
    for k, v in res.items():
        print(f"{k:12} {v or '-'}")
