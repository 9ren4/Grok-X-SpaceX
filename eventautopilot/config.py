"""Environment config. Everything is optional: missing keys degrade, never crash."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
OUT.mkdir(exist_ok=True)

load_dotenv(ROOT / ".env")


def _clean(name: str, default: str = "") -> str:
    # Treat the placeholder values in .env.example as "not set".
    v = (os.getenv(name) or default).strip()
    return "" if v.startswith("<") else v


GROK_API_KEY = _clean("GROK_API_KEY")
GROK_BASE_URL = _clean("GROK_BASE_URL", "https://api.x.ai/v1")
GROK_MODEL_SMART = _clean("GROK_MODEL_SMART", "grok-4")
GROK_MODEL_FAST = _clean("GROK_MODEL_FAST", "grok-4-fast")

SUPABASE_URL = _clean("SUPABASE_URL").rstrip("/")
SUPABASE_SERVICE_KEY = _clean("SUPABASE_SERVICE_KEY")

GHOST_ADMIN_URL = _clean("GHOST_ADMIN_URL").rstrip("/")
GHOST_ADMIN_KEY = _clean("GHOST_ADMIN_KEY")
GHOST_NEWSLETTER_SLUG = _clean("GHOST_NEWSLETTER_SLUG", "default-newsletter")

HAS_GROK = bool(GROK_API_KEY)
HAS_SUPABASE = bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)
HAS_GHOST = bool(GHOST_ADMIN_URL and GHOST_ADMIN_KEY)


def status() -> dict:
    """Shown in the web app header so the demo never lies about what is live."""
    return {
        "grok": HAS_GROK,
        "supabase": HAS_SUPABASE,
        "ghost": HAS_GHOST,
        "luma": True,  # public API, no key
    }

NOTION_TOKEN = _clean("NOTION_TOKEN")
NOTION_PARENT_PAGE_ID = _clean("NOTION_PARENT_PAGE_ID")
SUPABASE_ANON_KEY = _clean("SUPABASE_ANON_KEY")
HAS_NOTION = bool(NOTION_TOKEN and NOTION_PARENT_PAGE_ID)
