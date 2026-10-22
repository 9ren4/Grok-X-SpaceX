"""CLI: python visuals/render.py [run.json]  -> renders square + banner PNGs into out/."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eventautopilot.models import RunResult  # noqa: E402
from eventautopilot.visuals import render, upload  # noqa: E402


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "seed" / "sample_run.json"
    result = RunResult.model_validate_json(src.read_text())
    for size in ("square", "banner"):
        path = render(result, size)
        print(path)
        url = upload(path)
        if url:
            print(f"  -> {url}")


if __name__ == "__main__":
    main()
