"""Aryan's side in one call: RunResult -> visual, Ghost draft, Notion page.

Mehedi's pipeline calls `publish(result)` at the end of a run. Every step
degrades on its own, so one missing key never stops the rest.
"""
from __future__ import annotations

import sys
from pathlib import Path

from .models import RunResult


def publish(result: RunResult) -> dict:
    out = {"visual_path": "", "visual_url": "", "ghost_url": "", "notion_url": ""}

    try:
        from . import visuals
        path = visuals.render(result)
        out["visual_path"] = str(path)
        try:
            visuals.render(result, size="banner")
        except Exception as e:  # banner is a stretch, never block on it
            print(f"visuals: banner failed ({e})")
        out["visual_url"] = visuals.upload(path)
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
            result, visual_url=out["visual_url"], ghost_url=out["ghost_url"]
        )
    except Exception as e:
        print(f"notion: failed ({e})")

    return out


if __name__ == "__main__":
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "seed/sample_run.json")
    res = publish(RunResult.model_validate_json(src.read_text()))
    for k, v in res.items():
        print(f"{k:12} {v or '-'}")
