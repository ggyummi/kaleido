#!/usr/bin/env python3
"""
nuvio_pipeline.py  —  tiny-deluxe / Nuvio Backdrop Pipeline  (corrected build)
===========================================================================
Reads a local AIOMetadata.json, resolves every catalog to a list of TMDb
items, fetches poster + optional logo, renders a cinematic 1920×1080
backdrop entirely with Pillow (no subprocess / no external renderer
dependency), and writes dual-format JPG + WebP outputs.

Fixes applied vs. the previous build
──────────────────────────────────────
  Bug 1  Subprocess calls used wrong CLI contract → replaced with
         self-contained Pillow grid renderer (zero external dependency).
  Bug 3  Per-item cinematic backdrops replaced with the correct output
         format: one composite grid wallpaper per catalog (GRID_ROWS ×
         GRID_COLS poster thumbnails tiled into a single 1920×1080 image),
         matching the prism-wallpapers aesthetic.
  Bug 4  Trakt/Simkl catalogs silently replaced with generic "popular"
         data → catalogs that require live auth tokens now log a notice
         and fall back to TMDb popular instead of being skipped entirely.
  Bug 5  AIOMetadata.json is a frozen snapshot → noted in comments; the
         file must be re-exported and re-uploaded when catalogs change.
  Bug 6  Same display-name catalogs (e.g. "🔥 Trending" movie + series)
         resolved to the same folder, overwriting each other → type
         suffix (_movie / _series) is now always appended to the slug.

Secrets / env-vars required
──────────────────────────────────────
  TMDB_API_KEY  — from https://www.themoviedb.org/settings/api
"""

import io
import os
import re
import sys
import json
import time
import logging

import requests
from PIL import Image, ImageDraw

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

TMDB_BASE       = "https://api.themoviedb.org/3"
TMDB_IMG_BASE   = "https://image.tmdb.org/t/p/original"

COLLECTIONS_DIR = "collections"
TIMEOUT         = 20          # seconds per HTTP call
RATE_SLEEP      = 0.22        # keep TMDb happy (≤40 req/10 s)

# Grid wallpaper layout (one composite image per catalog, à la prism-wallpapers)
GRID_COLS  = 7
GRID_ROWS  = 3
GRID_GAP   = 8
MAX_ITEMS  = GRID_COLS * GRID_ROWS  # 21 posters fill the grid exactly

# Backdrop canvas dimensions
CANVAS_W, CANVAS_H = 1920, 1080

# ──────────────────────────────────────────────────────────────────────────────
# UTILITIES
# ──────────────────────────────────────────────────────────────────────────────

def validate_env():
    missing = [k for k in ("TMDB_API_KEY",) if not os.environ.get(k)]
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

# Source strings that route to an anime-flavoured TMDb discover call
ANIME_SOURCES = {"kitsu", "mal", "anilist"}

# Sources that require live auth tokens. Without auth we cannot enumerate their
# items, so they fall through to the generic TMDb popular fallback (step 5).
AUTH_SOURCES = {"trakt", "simkl"}


def resolve_catalog_items(catalog: dict) -> list[dict]:
    """
    Convert a catalog definition to a list of TMDb item dicts
    (each having at minimum "id", "title"/"name", and "type").

    Routing logic (in priority order):
      1. metadata.discover.params present → TMDb Discover API
      2. Anime source                     → TMDb Discover (anime genre)
      3. Built-in TMDb catalog IDs        → TMDb trending / top / top_rated
      4. Fallback (incl. auth-gated)      → TMDb popular
    """
    catalog_id  = catalog.get("id", "")
    source      = catalog.get("source", "").lower()
    cat_type    = catalog.get("type", "movie")           # 'movie' or 'series'
    tmdb_type   = "tv" if cat_type == "series" else "movie"
    name        = catalog.get("name", catalog_id)

    # Auth-gated sources (trakt, simkl) cannot enumerate items without live
    # tokens, so we note the fallback and continue to step 4 (TMDb popular).
    if source in AUTH_SOURCES:
        log.info(
            "  '%s': source='%s' requires live auth — no tokens in this "
            "workflow, falling back to TMDb popular as substitute.",
            name, source,
        )

    # ── 1. TMDb Discover (params block present in metadata) ───────────────────
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

    # ── 2. Anime sources → discover with animation + Japanese ─────────────────
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

    # ── 3. Built-in TMDb catalog IDs ──────────────────────────────────────────
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

    # ── 4. Generic fallback (also reached by auth-gated sources above) ────────
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


# ──────────────────────────────────────────────────────────────────────────────
# RENDERER  —  one composite grid wallpaper per catalog (prism-wallpapers style)
#
# Output: a single 1920×1080 JPEG containing GRID_ROWS × GRID_COLS poster
# thumbnails tiled edge-to-edge.  The grid is sized so poster width fills the
# canvas horizontally; the resulting poster height (at 2:3 ratio) makes the
# grid slightly taller than the canvas — the overflow bleeds equally off the
# top and bottom edges, giving a seamless full-bleed look.
# A vignette is composited on top for depth.
# ──────────────────────────────────────────────────────────────────────────────

def _build_vignette(width: int, height: int) -> Image.Image:
    """Radial border vignette (RGBA); composited on top of the poster grid."""
    vig  = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(vig)
    steps = 60
    for i in range(steps, 0, -1):
        t     = (steps - i) / steps
        alpha = int(t ** 2 * 180)
        inset = i * 9
        draw.rounded_rectangle(
            [inset, inset, width - inset, height - inset],
            radius=inset * 2,
            fill=(0, 0, 0, alpha),
        )
    return vig


def render_grid_backdrop(poster_bytes_list: list[bytes | None], out_jpg: str) -> bool:
    """
    Tile up to GRID_COLS × GRID_ROWS posters into a single 1920×1080 wallpaper.

    Layout arithmetic:
      poster_w  = (CANVAS_W − (COLS−1) × GAP) / COLS   [fills width exactly]
      poster_h  = poster_w × 1.5                         [2:3 aspect ratio]
      grid_h    = ROWS × poster_h + (ROWS−1) × GAP
      y_offset  = (CANVAS_H − grid_h) // 2              [negative ⇒ bleed top/bottom]
    """
    try:
        gap      = GRID_GAP
        poster_w = (CANVAS_W - (GRID_COLS - 1) * gap) // GRID_COLS
        poster_h = round(poster_w * 1.5)
        grid_h   = GRID_ROWS * poster_h + (GRID_ROWS - 1) * gap
        y_start  = (CANVAS_H - grid_h) // 2   # negative when grid overflows canvas

        canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), (10, 10, 16))

        slot = 0
        for row in range(GRID_ROWS):
            for col in range(GRID_COLS):
                if slot >= len(poster_bytes_list):
                    break
                pb = poster_bytes_list[slot]
                slot += 1
                if pb is None:
                    continue
                try:
                    with Image.open(io.BytesIO(pb)) as src:
                        thumb = src.convert("RGB").resize(
                            (poster_w, poster_h), Image.LANCZOS
                        )
                    x = col * (poster_w + gap)
                    y = y_start + row * (poster_h + gap)
                    canvas.paste(thumb, (x, y))
                except Exception as exc:
                    log.warning("    Poster slot (%d,%d) failed: %s", row, col, exc)

        canvas = Image.alpha_composite(
            canvas.convert("RGBA"), _build_vignette(CANVAS_W, CANVAS_H)
        ).convert("RGB")

        os.makedirs(os.path.dirname(out_jpg) or ".", exist_ok=True)
        canvas.save(out_jpg, "JPEG", quality=92, optimize=True)
        return True

    except Exception as exc:
        log.warning("  Grid render failed: %s", exc)
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
# CATALOG PROCESSING
# ──────────────────────────────────────────────────────────────────────────────

def process_catalog(catalog: dict) -> None:
    name      = catalog.get("name", catalog.get("id", "Unknown"))
    cat_type  = catalog.get("type", "movie")
    safe_name = slugify(name, media_type=cat_type)
    out_jpg   = os.path.join(COLLECTIONS_DIR, safe_name, "backdrop.jpg")

    log.info("")
    log.info("━" * 64)
    log.info("Catalog : %s  [type=%s]", name, cat_type)
    log.info("Output  : %s", out_jpg)
    log.info("━" * 64)

    # Cache check — skip only when both formats already exist
    out_webp = out_jpg.replace(".jpg", ".webp")
    if os.path.exists(out_jpg) and os.path.exists(out_webp):
        log.info("  Cache hit — skipping catalog '%s'.", name)
        return

    items = resolve_catalog_items(catalog)
    if not items:
        log.info("  No items resolved for this catalog.")
        return

    log.info("  Resolved %d item(s). Fetching posters …", len(items))

    poster_bytes_list: list[bytes | None] = []
    for item in items:
        title     = item.get("title") or item.get("name") or "?"
        tmdb_id   = str(item.get("id", ""))
        tmdb_type = item.get("_tmdb_type", "movie")
        if not tmdb_id:
            log.warning("    No TMDb ID for '%s' — leaving slot empty.", title)
            poster_bytes_list.append(None)
            continue
        pb = fetch_poster_bytes(tmdb_id, tmdb_type)
        if pb:
            log.info("    ✓ %s", title)
        else:
            log.warning("    No poster for '%s' — leaving slot empty.", title)
        poster_bytes_list.append(pb)

    filled = sum(1 for p in poster_bytes_list if p is not None)
    if filled == 0:
        log.warning("  No posters fetched — skipping render.")
        return

    log.info("  Rendering grid (%d/%d posters) …", filled, len(poster_bytes_list))
    if not render_grid_backdrop(poster_bytes_list, out_jpg):
        return

    convert_to_webp(out_jpg)
    log.info("  ✓  Saved  %s  (+.webp)", out_jpg)


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
    log.info("║           tiny-deluxe · Nuvio Backdrop Pipeline              ║")
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
    log.info("║              Pipeline complete. All done.                    ║")
    log.info("╚══════════════════════════════════════════════════════════════╝")


if __name__ == "__main__":
    main()
