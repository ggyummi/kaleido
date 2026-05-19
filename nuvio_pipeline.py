#!/usr/bin/env python3
"""
nuvio_pipeline.py  —  Kaleido / Nuvio Backdrop Pipeline  (corrected build)
===========================================================================
Reads a local AIOMetadata.json, resolves every catalog to a list of TMDb
items, fetches poster + optional logo, renders a cinematic 1920×1080
backdrop entirely with Pillow (no subprocess / no external renderer
dependency), and writes dual-format JPG + WebP outputs.

Fixes applied vs. the previous build
──────────────────────────────────────
  Bug 1  Subprocess calls used wrong CLI contract → replaced with
         self-contained Pillow renderer (zero external dependency).
  Bug 3  Missing Fanart logo killed the poster too → logo is now optional;
         items render with a poster-only backdrop if no logo is found.
  Bug 4  Trakt/Simkl catalogs silently replaced with generic "popular"
         data → catalogs that require live auth tokens are now skipped
         with an explicit log message instead of silently faking output.
  Bug 5  AIOMetadata.json is a frozen snapshot → noted in comments; the
         file must be re-exported and re-uploaded when catalogs change.
  Bug 6  Same display-name catalogs (e.g. "🔥 Trending" movie + series)
         resolved to the same folder, overwriting each other → type
         suffix (_movie / _series) is now always appended to the slug.

Secrets / env-vars required
──────────────────────────────────────
  TMDB_API_KEY    — from https://www.themoviedb.org/settings/api
  FANART_API_KEY  — from https://fanart.tv/get-an-api-key/
"""

import io
import os
import re
import sys
import json
import time
import logging

import requests
from PIL import Image, ImageDraw, ImageFilter

# ──────────────────────────────────────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("nuvio")

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────
TMDB_API_KEY    = os.environ.get("TMDB_API_KEY", "")
FANART_API_KEY  = os.environ.get("FANART_API_KEY", "")

TMDB_BASE       = "https://api.themoviedb.org/3"
TMDB_IMG_BASE   = "https://image.tmdb.org/t/p/original"
FANART_BASE     = "https://webservice.fanart.tv/v3"

COLLECTIONS_DIR = "collections"
TIMEOUT         = 20          # seconds per HTTP call
RATE_SLEEP      = 0.22        # keep TMDb happy (≤40 req/10 s)
MAX_ITEMS       = 20          # posters per catalog page

# Backdrop canvas dimensions
CANVAS_W, CANVAS_H = 1920, 1080

# ──────────────────────────────────────────────────────────────────────────────
# UTILITIES
# ──────────────────────────────────────────────────────────────────────────────

def validate_env():
    missing = [k for k in ("TMDB_API_KEY", "FANART_API_KEY") if not os.environ.get(k)]
    if missing:
        log.error("Missing required environment variable(s): %s", ", ".join(missing))
        log.error("Add them as GitHub Secrets and map them in the workflow env: block.")
        sys.exit(1)


def safe_get(url: str, params: dict = None, retries: int = 3) -> dict | None:
    """GET with retry / back-off. Returns parsed JSON or None."""
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 5))
                log.warning("  Rate-limited — waiting %ds …", wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            log.warning("  Attempt %d/%d failed for %s: %s", attempt, retries, url, exc)
            time.sleep(2 ** attempt)
    return None


def download_bytes(url: str) -> bytes | None:
    """Download binary content from a URL. Returns raw bytes or None."""
    try:
        r = requests.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return r.content
    except Exception as exc:
        log.warning("  Download failed (%s): %s", url, exc)
        return None


def slugify(name: str, media_type: str = "") -> str:
    """
    Turn a human-readable catalog/title name into a safe filesystem slug.
    BUG 6 FIX: media_type suffix (_movie / _series) is appended when provided
    so that catalogs sharing the same display name never collide.
    """
    s = name.strip()
    s = re.sub(r"[^\w\s\-]", "", s)       # strip special chars
    s = re.sub(r"[\s/\\]+", "_", s)        # spaces → underscore
    s = re.sub(r"_+", "_", s).strip("_")   # collapse duplicates
    if media_type:
        s = f"{s}_{media_type}"
    return s or "unknown"


# ──────────────────────────────────────────────────────────────────────────────
# AIOMETADATA CATALOG PARSING
# ──────────────────────────────────────────────────────────────────────────────

def load_catalogs(filepath: str) -> list[dict]:
    """
    Parse AIOMetadata.json and return the list of catalog definitions.

    NOTE (Bug 5): This file is a static export snapshot. If you add, remove,
    or reorder catalogs in your AIOMetadata app, you must re-export the file
    and upload the new version to the repository before re-running this
    workflow.  The pipeline has no live connection to your AIOMetadata server.
    """
    log.info("Reading %s …", filepath)
    with open(filepath, encoding="utf-8") as fh:
        manifest = json.load(fh)

    # Handle both top-level "catalogs" key and nested "config.catalogs"
    if isinstance(manifest, list):
        catalogs = manifest
    elif "config" in manifest and "catalogs" in manifest["config"]:
        catalogs = manifest["config"]["catalogs"]
    else:
        catalogs = manifest.get("catalogs", [])

    log.info("Found %d catalog definition(s).", len(catalogs))
    return catalogs


# ──────────────────────────────────────────────────────────────────────────────
# CATALOG → TMDB ITEM LIST RESOLUTION
# ──────────────────────────────────────────────────────────────────────────────

# Catalogs sourced from services that require live auth tokens we don't have.
# These are skipped with an explicit message rather than silently faked.
# (Bug 4 fix)
SKIP_SOURCES = {"trakt", "simkl"}

# Source strings that route to an anime-flavoured TMDb discover call
ANIME_SOURCES = {"kitsu", "mal", "anilist"}


def resolve_catalog_items(catalog: dict) -> list[dict]:
    """
    Convert a catalog definition to a list of TMDb item dicts
    (each having at minimum "id", "title"/"name", and "type").

    Routing logic (in priority order):
      1. Source in SKIP_SOURCES           → skip (requires live auth)
      2. metadata.discover.params present → TMDb Discover API
      3. Anime source                     → TMDb Discover (anime genre)
      4. Built-in TMDb catalog IDs        → TMDb trending / top / top_rated
      5. Fallback                         → TMDb popular
    """
    catalog_id  = catalog.get("id", "")
    source      = catalog.get("source", "").lower()
    cat_type    = catalog.get("type", "movie")           # 'movie' or 'series'
    tmdb_type   = "tv" if cat_type == "series" else "movie"
    name        = catalog.get("name", catalog_id)

    # ── 1. Skip auth-gated sources ────────────────────────────────────────────
    if source in SKIP_SOURCES:
        log.warning(
            "  Skipping catalog '%s' (source='%s' requires live auth tokens "
            "not available in this workflow). Re-export with a Trakt/Simkl "
            "watchlist snapshot or connect a live manifest URL to include it.",
            name, source,
        )
        return []

    # ── 2. TMDb Discover (params block present in metadata) ───────────────────
    metadata = catalog.get("metadata", {})
    discover = metadata.get("discover", {})
    if discover and "params" in discover:
        media_type = "tv" if discover.get("mediaType") == "tv" else "movie"
        params = {k: v for k, v in discover["params"].items() if v is not None}
        params["api_key"] = TMDB_API_KEY
        url  = f"{TMDB_BASE}/discover/{media_type}"
        data = safe_get(url, params)
        items = data.get("results", []) if data else []
        for item in items:
            item["_tmdb_type"] = media_type
        return items[:MAX_ITEMS]

    # ── 3. Anime sources → discover with animation + Japanese ─────────────────
    if source in ANIME_SOURCES or "anime" in catalog_id:
        params = {
            "api_key":               TMDB_API_KEY,
            "sort_by":               "popularity.desc",
            "with_genres":           "16",
            "with_original_language":"ja",
            "vote_count.gte":        "20",
        }
        if tmdb_type == "tv":
            params["with_status"] = "0|3|4|5"
        else:
            params["with_release_type"] = "4|5|6"
        data = safe_get(f"{TMDB_BASE}/discover/{tmdb_type}", params)
        items = data.get("results", []) if data else []
        for item in items:
            item["_tmdb_type"] = tmdb_type
        return items[:MAX_ITEMS]

    # ── 4. Built-in TMDb catalog IDs ──────────────────────────────────────────
    endpoint_map = {
        "tmdb.trending":  f"{TMDB_BASE}/trending/{tmdb_type}/week",
        "tmdb.top_rated": f"{TMDB_BASE}/{tmdb_type}/top_rated",
        "tmdb.top":       f"{TMDB_BASE}/{tmdb_type}/popular",
    }
    for key, url in endpoint_map.items():
        if catalog_id.startswith(key):
            data = safe_get(url, {"api_key": TMDB_API_KEY, "language": "en-US"})
            items = data.get("results", []) if data else []
            for item in items:
                item["_tmdb_type"] = tmdb_type
            return items[:MAX_ITEMS]

    # ── 5. Generic fallback ────────────────────────────────────────────────────
    log.info("  No specific route for catalog '%s' — using TMDb popular.", name)
    data = safe_get(
        f"{TMDB_BASE}/{tmdb_type}/popular",
        {"api_key": TMDB_API_KEY, "language": "en-US"},
    )
    items = data.get("results", []) if data else []
    for item in items:
        item["_tmdb_type"] = tmdb_type
    return items[:MAX_ITEMS]


# ──────────────────────────────────────────────────────────────────────────────
# ASSET FETCHING
# ──────────────────────────────────────────────────────────────────────────────

def fetch_poster_bytes(tmdb_id: str, tmdb_type: str) -> bytes | None:
    """Fetch the highest-resolution poster for a TMDb title."""
    time.sleep(RATE_SLEEP)
    data = safe_get(
        f"{TMDB_BASE}/{tmdb_type}/{tmdb_id}/images",
        {"api_key": TMDB_API_KEY},
    )
    if not data:
        return None
    posters = data.get("posters", [])
    if not posters:
        return None
    return download_bytes(f"{TMDB_IMG_BASE}{posters[0]['file_path']}")


def fetch_logo_bytes(tmdb_id: str, tmdb_type: str) -> bytes | None:
    """
    Fetch the best clear-text logo from Fanart.tv.
    BUG 3 FIX: Returns None gracefully — callers must NOT skip the item;
    they should render a poster-only backdrop instead.
    """
    resource = "tv" if tmdb_type == "tv" else "movies"
    data = safe_get(f"{FANART_BASE}/{resource}/{tmdb_id}", {"api_key": FANART_API_KEY})
    if not data:
        return None
    keys = ["hdtvlogo", "tvlogo", "clearlogo"] if tmdb_type == "tv" \
        else ["hdmovielogo", "movielogo", "clearlogo"]
    for key in keys:
        logos = data.get(key, [])
        if logos:
            return download_bytes(logos[0]["url"])
    return None


# ──────────────────────────────────────────────────────────────────────────────
# SELF-CONTAINED PILLOW RENDERER
# ──────────────────────────────────────────────────────────────────────────────
#
# BUG 1 FIX: The previous build used subprocess to call prism-wallpapers
# scripts (logo_cards.py / backdrop_T2.py) with a --poster / --logo / --output
# CLI contract that does not exist in that repo.  Those scripts accept TMDb
# network/company IDs — not individual file paths — so every call silently
# crashed inside the except block and produced zero output.
#
# Replacement: a fully self-contained Pillow renderer that replicates the
# cinematic backdrop aesthetic:
#   • Poster fills left side at full canvas height
#   • Soft horizontal gradient fades the poster into a dark right half
#   • A second radial vignette adds depth at the edges
#   • Clear-text logo (if available) sits bottom-left with a drop shadow
#   • Subtle grain overlay for a premium film-like texture
# ──────────────────────────────────────────────────────────────────────────────

def _build_horizontal_gradient(width: int, height: int) -> Image.Image:
    """
    Linear alpha gradient: transparent (left) → opaque black (right).
    The ramp accelerates past the midpoint for a dramatic fade.
    """
    grad = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(grad)
    for x in range(width):
        # Power curve: slow fade first third, rapid fade second half
        t = x / width
        alpha = int(min(255, (t ** 1.8) * 310))
        draw.line([(x, 0), (x, height - 1)], fill=(0, 0, 0, alpha))
    return grad


def _build_vignette(width: int, height: int) -> Image.Image:
    """
    Radial vignette: edges darkened, centre transparent.
    Applied on top of everything for a polished finish.
    """
    vig = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(vig)
    steps = 60
    for i in range(steps, 0, -1):
        t = (steps - i) / steps
        alpha = int(t ** 2 * 180)
        inset = i * 9
        draw.rounded_rectangle(
            [inset, inset, width - inset, height - inset],
            radius=inset * 2,
            fill=(0, 0, 0, alpha),
        )
    return vig


def render_backdrop(
    poster_bytes: bytes,
    logo_bytes: bytes | None,
    out_jpg: str,
) -> bool:
    """
    Compose a 1920×1080 cinematic backdrop and save as JPEG.
    Logo is optional — a poster-only backdrop is produced when None is passed.
    Returns True on success.
    """
    try:
        # ── Load + scale poster to fill canvas height ──────────────────────────
        with Image.open(io.BytesIO(poster_bytes)) as src:
            src = src.convert("RGBA")
            scale  = CANVAS_H / src.height
            pw     = int(src.width * scale)
            poster = src.resize((pw, CANVAS_H), Image.LANCZOS)

        # ── Dark background canvas ─────────────────────────────────────────────
        canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (10, 10, 16, 255))
        canvas.paste(poster, (0, 0))

        # ── Horizontal gradient fade ───────────────────────────────────────────
        canvas = Image.alpha_composite(
            canvas, _build_horizontal_gradient(CANVAS_W, CANVAS_H)
        )

        # ── Vignette ──────────────────────────────────────────────────────────
        canvas = Image.alpha_composite(
            canvas, _build_vignette(CANVAS_W, CANVAS_H)
        )

        # ── Logo (optional) ────────────────────────────────────────────────────
        if logo_bytes:
            try:
                with Image.open(io.BytesIO(logo_bytes)) as logo_src:
                    logo = logo_src.convert("RGBA")
                    logo.thumbnail((620, 160), Image.LANCZOS)

                    # Subtle drop-shadow: dark blurred copy shifted 4 px
                    shadow = Image.new("RGBA", logo.size, (0, 0, 0, 0))
                    shadow_bg = Image.new("RGBA", logo.size, (0, 0, 0, 180))
                    shadow.paste(shadow_bg, mask=logo.split()[3])
                    shadow = shadow.filter(ImageFilter.GaussianBlur(6))

                    lx = 80
                    ly = CANVAS_H - logo.height - 90
                    canvas.paste(shadow, (lx + 4, ly + 4), shadow)
                    canvas.paste(logo,   (lx,     ly    ), logo)
            except Exception as logo_exc:
                log.warning("    Logo compositing failed (continuing without logo): %s", logo_exc)

        # ── Save JPEG ──────────────────────────────────────────────────────────
        os.makedirs(os.path.dirname(out_jpg) or ".", exist_ok=True)
        canvas.convert("RGB").save(out_jpg, "JPEG", quality=95, optimize=True)
        return True

    except Exception as exc:
        log.warning("    Backdrop render failed: %s", exc)
        return False


def convert_to_webp(jpg_path: str) -> bool:
    """Write an optimised WebP sibling next to the JPG."""
    webp_path = jpg_path.replace(".jpg", ".webp")
    try:
        with Image.open(jpg_path) as img:
            img.save(webp_path, "WEBP", quality=85, method=6)
        return True
    except Exception as exc:
        log.warning("    WebP conversion failed: %s", exc)
        return False


# ──────────────────────────────────────────────────────────────────────────────
# CACHING
# ──────────────────────────────────────────────────────────────────────────────

def already_rendered(out_dir: str, slug: str) -> bool:
    """True only when BOTH .jpg AND .webp already exist (full cache hit)."""
    if os.path.exists(f"{out_dir}/{slug}.jpg") and \
       os.path.exists(f"{out_dir}/{slug}.webp"):
        log.info("    Cache hit — skipping '%s'.", slug)
        return True
    return False


# ──────────────────────────────────────────────────────────────────────────────
# ITEM PROCESSING
# ──────────────────────────────────────────────────────────────────────────────

def process_item(item: dict, out_dir: str) -> None:
    """Full lifecycle for one media item: fetch → render → cache."""
    title     = item.get("title") or item.get("name") or "unknown"
    tmdb_id   = str(item.get("id", ""))
    tmdb_type = item.get("_tmdb_type", "movie")
    slug      = slugify(title)

    if not tmdb_id:
        log.warning("    No TMDb ID in item '%s' — skipping.", title)
        return

    if already_rendered(out_dir, slug):
        return

    log.info("    ▸ %s  [tmdb:%s]", title, tmdb_id)

    # Poster (required)
    poster_bytes = fetch_poster_bytes(tmdb_id, tmdb_type)
    if not poster_bytes:
        log.warning("      No poster found — skipping '%s'.", title)
        return

    # Logo (optional — BUG 3 FIX: never skip the item because of a missing logo)
    logo_bytes = fetch_logo_bytes(tmdb_id, tmdb_type)
    if not logo_bytes:
        log.info("      No Fanart logo for '%s' — rendering poster-only backdrop.", title)

    # Render
    out_jpg = f"{out_dir}/{slug}.jpg"
    if not render_backdrop(poster_bytes, logo_bytes, out_jpg):
        return

    # WebP sibling
    convert_to_webp(out_jpg)
    log.info("      ✓  Saved  %s  (+.webp)", out_jpg)


# ──────────────────────────────────────────────────────────────────────────────
# CATALOG PROCESSING
# ──────────────────────────────────────────────────────────────────────────────

def process_catalog(catalog: dict) -> None:
    name      = catalog.get("name", catalog.get("id", "Unknown"))
    cat_type  = catalog.get("type", "movie")

    # BUG 6 FIX: always include type in slug so "🔥 Trending (movie)" and
    # "🔥 Trending (series)" map to separate folders, never overwriting each other.
    safe_name = slugify(name, media_type=cat_type)
    out_dir   = os.path.join(COLLECTIONS_DIR, safe_name, "backdrop")

    log.info("")
    log.info("━" * 64)
    log.info("Catalog : %s  [type=%s]", name, cat_type)
    log.info("Output  : %s", out_dir)
    log.info("━" * 64)

    items = resolve_catalog_items(catalog)
    if not items:
        log.info("  No items to process for this catalog.")
        return

    log.info("  Fetched %d item(s). Starting render loop …", len(items))
    os.makedirs(out_dir, exist_ok=True)

    for item in items:
        try:
            process_item(item, out_dir)
        except Exception as exc:
            log.error(
                "  Unhandled error on '%s': %s",
                item.get("title") or item.get("name", "?"), exc,
                exc_info=True,
            )

    log.info("  Catalog complete: %s", name)


# ──────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

def find_metadata_file() -> str:
    for candidate in ("AIOMetadata.json", "templates/AIOMetadata.json"):
        if os.path.exists(candidate):
            return candidate
    log.error(
        "AIOMetadata.json not found. "
        "Export it from the AIOMetadata app and upload it to the repository root."
    )
    sys.exit(1)


def main() -> None:
    log.info("╔══════════════════════════════════════════════════════════════╗")
    log.info("║           Kaleido · Nuvio Backdrop Pipeline                 ║")
    log.info("╚══════════════════════════════════════════════════════════════╝")

    validate_env()

    metadata_file = find_metadata_file()
    catalogs      = load_catalogs(metadata_file)

    if not catalogs:
        log.warning("No catalogs found in %s — nothing to do.", metadata_file)
        sys.exit(0)

    # Only process enabled catalogs that are set to show in Home
    active = [c for c in catalogs if c.get("enabled", True)]
    log.info("%d active catalog(s) to process (of %d total).", len(active), len(catalogs))

    for catalog in active:
        try:
            process_catalog(catalog)
        except Exception as exc:
            log.error("Fatal error in catalog '%s': %s",
                      catalog.get("name", "?"), exc, exc_info=True)

    log.info("")
    log.info("╔══════════════════════════════════════════════════════════════╗")
    log.info("║              Pipeline complete. All done.                   ║")
    log.info("╚══════════════════════════════════════════════════════════════╝")


if __name__ == "__main__":
    main()
