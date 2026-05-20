#!/usr/bin/env python3
"""
generate_catalog_assets.py — Nuvio TV Media Catalog Asset Generator
===========================================================================
Reads nuvio-collections.json, resolves each catalog entry whose ID matches
the pattern  collections.{folder}.{catalog},  fetches landscape backdrop
artwork from Stremio addon endpoints (or TMDb as fallback), and writes four
asset types per catalog into a FLAT directory structure:

  collections/{folder}/backdrop/{catalog}.jpg(.webp)  — Prism 3D tilted-grid collage
  collections/{folder}/focused/{catalog}.jpg(.webp)   — hero banner + glow text
  collections/{folder}/cover/{catalog}.jpg(.webp)     — hero banner, no glow
  collections/{folder}/title/                         — init only; never overwritten

Backdrop images are fetched from ALL catalogSources (movies + series mixed)
and rendered using the Prism-style tilted-grid engine adapted from
luckynumb3rs/stremio-perfect-setup (collections/scripts/backdrop.py).
No OAuth tokens are required — uses public Stremio addon HTTP endpoints.

Optional environment variables:
  AIOMETADATA_URL   Base URL of AIOMetadata/Stremio addon (preferred)
  TMDB_API_KEY      TMDb API key (fallback for entries without catalogSources)
"""

import colorsys
import io
import itertools
import math
import os
import sys
import json
import time
import logging
import argparse
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("nuvio.catalog")

# ─── Global Config ───────────────────────────────────────────────────────────────

TMDB_API_KEY    = os.environ.get("TMDB_API_KEY", "")
TMDB_BASE       = "https://api.themoviedb.org/3"
TMDB_IMG_BASE   = "https://image.tmdb.org/t/p"

# Optional: base URL of your AIOMetadata / Stremio addon instance.
# When set the script calls {AIOMETADATA_URL}/catalog/{type}/{id}.json directly —
# fully anonymous HTTP, no OAuth tokens required in the request.
# When absent the script falls back to the public Cinemeta addon.
AIOMETADATA_URL = os.environ.get("AIOMETADATA_URL", "").rstrip("/")
CINEMETA_URL    = "https://v3-cinemeta.strem.io"

COLLECTIONS_DIR = Path("collections")
SOURCE_JSON     = Path("nuvio-collections.json")

CANVAS_W, CANVAS_H = 1920, 1080

# Backdrop images to fetch per catalog.  The Prism engine tiles internally, so
# even a modest pool gives a full grid; 40 gives good visual variety.
MAX_TILES = 40

TIMEOUT    = 20      # seconds per HTTP call
RATE_SLEEP = 0.25    # polite rate limiting between image downloads

# ─── Prism Tile Geometry Constants ─────────────────────────────────────────────────────
# Source: luckynumb3rs/stremio-perfect-setup  collections/scripts/backdrop.py

CARD_RADIUS = 9    # rounded-corner radius (px at 1x tile size)
TILT_DEG    = 10   # clockwise tilt of the entire grid
TILE_W      = 372  # nominal tile width  (at 1080p / scale=1.0)
TILE_H      = 210  # nominal tile height (at 1080p / scale=1.0)
GAP         = 9    # gap between tiles
ROWS        = 10   # logical rows (buffer rows added internally)
COLS        = 10   # logical cols (buffer cols added internally)
STAGGER     = 0.5  # per-row horizontal offset as a fraction of (tile+gap)
FOCUS_X     = 0.5  # horizontal focal point fraction (0=left, 1=right)
FOCUS_Y     = 0.53 # vertical   focal point fraction (0=top,  1=bottom)

# ─── Font Candidates ──────────────────────────────────────────────────────────────────

_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/urw-base35/NimbusSans-Bold.otf",
    "/usr/share/fonts/urw-base35/NimbusSans-Bold.otf",
    "/usr/share/fonts/type1/urw-base35/NimbusSans-Bold.otf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

# ─── Environment Validation ─────────────────────────────────────────────────────────

def validate_env() -> None:
    if not AIOMETADATA_URL and not TMDB_API_KEY:
        log.warning(
            "Neither AIOMETADATA_URL nor TMDB_API_KEY is set. "
            "Catalog images will be fetched from the public Cinemeta addon "
            "(generic top content only — provider-specific catalogs will not be accurate)."
        )
    elif not AIOMETADATA_URL:
        log.info(
            "AIOMETADATA_URL not set — provider/genre-specific catalogs will fall back "
            "to TMDb Discover (TMDB_API_KEY present)."
        )

# ─── HTTP Helpers ──────────────────────────────────────────────────────────────────

def safe_get(url: str, params: dict | None = None, retries: int = 3) -> dict | None:
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 5))
                log.warning("Rate-limited — waiting %ds …", wait)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            log.warning("Attempt %d/%d failed for %s: %s", attempt, retries, url, exc)
            time.sleep(2 ** attempt)
    return None


def download_image(url: str) -> Image.Image | None:
    try:
        r = requests.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGB")
    except Exception as exc:
        log.warning("Download failed (%s): %s", url, exc)
        return None

# ─── JSON Parsing ────────────────────────────────────────────────────────────────────

def load_catalogs(json_path: Path) -> list[dict]:
    log.info("Loading %s …", json_path)
    with open(json_path, encoding="utf-8") as fh:
        data = json.load(fh)
    items = data if isinstance(data, list) else data.get("catalogs", [])
    # New nested format: top-level items are groups with a "folders" array.
    # Unwrap so downstream code always sees a flat list of catalog entries.
    flat: list[dict] = []
    for item in items:
        if "folders" in item:
            flat.extend(item["folders"])
        else:
            flat.append(item)
    return flat


def parse_collection_id(catalog_id: str) -> tuple[str, str] | None:
    """
    Match IDs of the form  collections.{folder}.{catalog}  (exactly 3 segments).
    Returns (folder, catalog_slug) or None if the pattern doesn't match.
    """
    parts = catalog_id.split(".")
    if len(parts) == 3 and parts[0] == "collections":
        return parts[1], parts[2]
    return None

# ─── Stremio Catalog Fetching (primary, unauthenticated) ────────────────────────────
#
# Calls {AIOMETADATA_URL}/catalog/{type}/{id}.json — a plain GET with no auth.
# AIOMetadata handles any provider auth server-side.
# Falls back to the public Cinemeta addon when AIOMETADATA_URL is not set.

_CINEMETA_ID = {
    "movie":  "top",
    "series": "top",
}


def fetch_stremio_catalog(media_type: str, catalog_id: str) -> list[dict]:
    """
    Fetch metas from a Stremio addon catalog endpoint (unauthenticated GET).

    Priority:
      1. AIOMETADATA_URL set → call {instance}/catalog/{type}/{id}.json
         If the call fails (404, 5xx, network error), fall through to step 2.
      2. Cinemeta public addon → /catalog/{type}/top.json
    """
    if AIOMETADATA_URL:
        url = f"{AIOMETADATA_URL}/catalog/{media_type}/{catalog_id}.json"
        log.info("    GET %s", url)
        data = safe_get(url)
        if data is not None:
            metas = data.get("metas", [])
            log.info("    → %d meta(s)", len(metas))
            return metas
        log.warning(
            "    AIOMETADATA fetch failed for '%s/%s' — falling back to Cinemeta.",
            media_type, catalog_id,
        )

    cinemeta_id = _CINEMETA_ID.get(media_type, "top")
    url = f"{CINEMETA_URL}/catalog/{media_type}/{cinemeta_id}.json"
    log.info("    GET %s (Cinemeta fallback)", url)
    data  = safe_get(url)
    metas = (data or {}).get("metas", [])
    log.info("    → %d meta(s)", len(metas))
    return metas


def backdrop_from_meta(meta: dict) -> Image.Image | None:
    """
    Download the landscape backdrop for a Stremio meta object.
    Tries background/backgroundImage (landscape) first, then poster as fallback.
    """
    time.sleep(RATE_SLEEP)
    for key in ("background", "backgroundImage"):
        url = meta.get(key)
        if url:
            img = download_image(url)
            if img:
                return img
    poster = meta.get("poster")
    if poster:
        return download_image(poster)
    return None


def get_catalog_sources(catalog: dict) -> list[dict]:
    """Return normalized catalog sources, guaranteed to have an 'id' key."""
    raw = catalog.get("catalogSources", catalog.get("sources", []))
    # New format uses catalogId instead of id — normalize for uniform downstream use.
    result = []
    for src in raw:
        if "catalogId" in src and "id" not in src:
            src = {**src, "id": src["catalogId"]}
        result.append(src)
    return result


def fetch_all_backdrops(
    catalog: dict, limit: int = MAX_TILES
) -> tuple[list[Image.Image], "Image.Image | None"]:
    """
    Primary data path — mixes backdrops from every entry in catalogSources,
    deduplicating by Stremio meta ID, capping at `limit` images.

    Falls back to the TMDb-based resolver when catalogSources is absent
    (backward-compatible with entries using metadata.discover.params).

    Returns (backdrop_images_list, top_backdrop_or_None).
    """
    sources = get_catalog_sources(catalog)

    if sources:
        # ── Stremio path: call addon endpoints, mix movies + series ──────────
        all_metas: list[dict] = []
        seen: set[str] = set()
        for src in sources:
            metas = fetch_stremio_catalog(src["type"], src["id"])
            for meta in metas:
                mid = meta.get("id", "")
                if mid and mid not in seen:
                    seen.add(mid)
                    all_metas.append(meta)

        backdrops: list[Image.Image] = []
        top: Image.Image | None = None
        for meta in all_metas[:limit]:
            name = meta.get("name", meta.get("id", "?"))
            img  = backdrop_from_meta(meta)
            if img:
                log.info("    ✓ %s", name)
                if top is None:
                    top = img
                backdrops.append(img)
            else:
                log.warning("    ✗ No image — %s", name)
        return backdrops, top

    # ── TMDb fallback path (no catalogSources field) ─────────────────────────
    log.info("  No catalogSources — using TMDb resolver as fallback.")
    items     = resolve_items(catalog, limit)
    backdrops = []
    top       = None
    for item in items[:limit]:
        title = item.get("title") or item.get("name") or "?"
        img   = fetch_backdrop_tmdb(item)
        if img:
            log.info("    ✓ %s", title)
            if top is None:
                top = img
            backdrops.append(img)
        else:
            log.warning("    ✗ No backdrop — %s", title)
    return backdrops, top

# ─── TMDb Item Resolution (fallback when catalogSources is absent) ─────────────────

_AUTH_SOURCES  = {"trakt", "simkl"}
_ANIME_SOURCES = {"kitsu", "mal", "anilist"}

_SLUG_ENDPOINT = {
    "trending":    "/trending/{t}/week",
    "popular":     "/{t}/popular",
    "top_rated":   "/{t}/top_rated",
    "top-rated":   "/{t}/top_rated",
    "upcoming":    "/movie/upcoming",
    "new":         "/{t}/popular",
    "recommended": "/{t}/popular",
}


def resolve_items(catalog: dict, limit: int = MAX_TILES) -> list[dict]:
    """
    Resolve a catalog definition to a ranked list of TMDb item dicts.
    Each item will have '_tmdb_type' set to 'movie' or 'tv'.

    Resolution priority:
      1. metadata.discover.params  → TMDb Discover API
      2. Anime source              → Discover (animation + Japanese)
      3. Slug keyword match        → Named TMDb list endpoint
      4. Fallback                  → TMDb popular
    """
    cat_id    = catalog.get("id", "")
    source    = catalog.get("source", "").lower()
    cat_type  = catalog.get("type", "movie")
    tmdb_type = "tv" if cat_type == "series" else "movie"
    name      = catalog.get("name", cat_id)

    if source in _AUTH_SOURCES:
        log.info("'%s' requires live auth (%s) — falling back to TMDb popular.", name, source)

    def _tag(items_list, t):
        for item in items_list:
            item["_tmdb_type"] = t
        return items_list

    # 1. discover params block (most explicit)
    meta     = catalog.get("metadata", {})
    discover = meta.get("discover", {})
    if discover and "params" in discover:
        media_type = "tv" if discover.get("mediaType") == "tv" else "movie"
        params     = {k: v for k, v in discover["params"].items() if v is not None}
        params["api_key"] = TMDB_API_KEY
        data  = safe_get(f"{TMDB_BASE}/discover/{media_type}", params)
        items = (data or {}).get("results", [])
        return _tag(items, media_type)[:limit]

    # 2. Anime sources
    if source in _ANIME_SOURCES or "anime" in cat_id:
        params = {
            "api_key": TMDB_API_KEY, "sort_by": "popularity.desc",
            "with_genres": "16", "with_original_language": "ja", "vote_count.gte": "20",
        }
        data  = safe_get(f"{TMDB_BASE}/discover/{tmdb_type}", params)
        items = (data or {}).get("results", [])
        return _tag(items, tmdb_type)[:limit]

    # 3. Slug keyword match
    parts = cat_id.split(".")
    slug  = parts[2].lower() if len(parts) >= 3 else cat_id.lower()
    for keyword, tpl in _SLUG_ENDPOINT.items():
        if slug == keyword or slug.startswith(keyword):
            url   = f"{TMDB_BASE}{tpl.replace('{t}', tmdb_type)}"
            data  = safe_get(url, {"api_key": TMDB_API_KEY, "language": "en-US"})
            items = (data or {}).get("results", [])
            return _tag(items, tmdb_type)[:limit]

    # 4. Generic popular fallback
    log.info("No specific route for '%s' — using TMDb popular.", name)
    data  = safe_get(f"{TMDB_BASE}/{tmdb_type}/popular",
                     {"api_key": TMDB_API_KEY, "language": "en-US"})
    items = (data or {}).get("results", [])
    return _tag(items, tmdb_type)[:limit]


def fetch_backdrop_tmdb(item: dict) -> Image.Image | None:
    """
    Fetch the highest-quality landscape backdrop for a TMDb item dict.
    Used only by the TMDb fallback path (when catalogSources is absent).
    """
    tmdb_id   = str(item.get("id", ""))
    tmdb_type = item.get("_tmdb_type", "movie")
    if not tmdb_id:
        return None

    time.sleep(RATE_SLEEP)
    data = safe_get(f"{TMDB_BASE}/{tmdb_type}/{tmdb_id}/images",
                    {"api_key": TMDB_API_KEY})
    if data:
        bds = sorted(data.get("backdrops", []),
                     key=lambda b: b.get("vote_average", 0), reverse=True)
        if bds:
            img = download_image(f"{TMDB_IMG_BASE}/original{bds[0]['file_path']}")
            if img:
                return img

    bp = item.get("backdrop_path")
    if bp:
        return download_image(f"{TMDB_IMG_BASE}/w1280{bp}")
    return None

# ─── Prism Backdrop Engine ────────────────────────────────────────────────────────────────
#
# Adapted from luckynumb3rs/stremio-perfect-setup  collections/scripts/backdrop.py
#
# Changes for our integration:
#   • No TMDB API calls — we supply pre-downloaded PIL Images directly.
#   • Canvas size fixed to 1920×1080 (scale=1.0).
#   • Accent color derived deterministically from catalog slug (no cover scan).
#   • render_prism_backdrop() is the single public entry point.


def default_accent_for_label(label: str) -> tuple[int, int, int]:
    """Derive a deterministic HSV-based accent color from the catalog slug."""
    seed = sum((i + 1) * ord(c) for i, c in enumerate(label or "Backdrop"))
    hue  = (seed % 360) / 360.0
    r, g, b = colorsys.hsv_to_rgb(hue, 0.65, 0.88)
    return (int(r * 255), int(g * 255), int(b * 255))


def rounded_rect_mask(width: int, height: int, radius: int = CARD_RADIUS) -> Image.Image:
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([0, 0, width - 1, height - 1], radius=radius, fill=255)
    return mask


def make_tile(image: Image.Image, tile_width: int, tile_height: int) -> Image.Image:
    """Crop to ratio, resize, and apply rounded-corner mask — returns RGBA tile."""
    sw, sh       = image.size
    target_ratio = tile_width / tile_height
    src_ratio    = sw / sh
    if src_ratio > target_ratio:
        new_w  = int(sh * target_ratio)
        left   = (sw - new_w) // 2
        image  = image.crop((left, 0, left + new_w, sh))
    else:
        new_h  = int(sw / target_ratio)
        top    = (sh - new_h) // 2
        image  = image.crop((0, top, sw, top + new_h))
    image          = image.resize((tile_width, tile_height), Image.LANCZOS)
    scaled_radius  = max(8, int(CARD_RADIUS * tile_width / TILE_W))
    mask           = rounded_rect_mask(tile_width, tile_height, radius=scaled_radius)
    result         = Image.new("RGBA", (tile_width, tile_height), (0, 0, 0, 0))
    result.paste(image, mask=mask)
    return result


def build_tilted_grid(
    tiles: list[Image.Image],
    canvas_width: int,
    canvas_height: int,
    scale: float = 1.0,
    focus_x: float | None = None,
    focus_y: float | None = None,
) -> Image.Image:
    """
    Compose a staggered TILT_DEG-degree tilted grid of tile images onto a dark
    canvas centred on the focal point.  Best images are placed closest to the
    focal point; the pool is cycled to fill all grid slots.
    Returns an RGBA image at (canvas_width, canvas_height).
    """
    fx = FOCUS_X if focus_x is None else focus_x
    fy = FOCUS_Y if focus_y is None else focus_y

    tile_width  = int(TILE_W * scale)
    tile_height = int(TILE_H * scale)
    gap         = int(GAP   * scale)

    cols       = COLS + 3
    rows       = ROWS + 3
    needed     = rows * cols
    tile_list  = (tiles * (needed // len(tiles) + 1))[:needed]
    stagger_px = int(STAGGER * (tile_width + gap))

    grid_width  = cols * (tile_width + gap) + rows * stagger_px
    grid_height = rows * (tile_height + gap)
    grid = Image.new("RGBA", (grid_width, grid_height), (0, 0, 0, 0))

    focal_x   = fx * grid_width
    focal_y   = fy * grid_height
    focal_row = max(0, min(rows - 1, int(focal_y / (tile_height + gap))))
    focal_col = max(0, min(cols - 1,
                           int((focal_x - focal_row * stagger_px) / (tile_width + gap))))

    # Sort cells nearest-to-focal first so the best images land at the focal area.
    cells = [(row, col) for row in range(rows) for col in range(cols)]
    cells.sort(key=lambda pos: abs(pos[0] - focal_row) + abs(pos[1] - focal_col))

    for index, (row, col) in enumerate(cells):
        if index >= len(tile_list):
            break
        x    = row * stagger_px + col * (tile_width + gap)
        y    = row * (tile_height + gap)
        tile = make_tile(tile_list[index], tile_width, tile_height)
        grid.paste(tile, (x, y), tile)

    rotated = grid.rotate(TILT_DEG, expand=True, resample=Image.BICUBIC)
    rw, rh  = rotated.size

    # Map the focal point through the rotation transform to find where to anchor it.
    angle_rad    = math.radians(-TILT_DEG)
    pre_cx       = fx * grid_width  - grid_width  / 2
    pre_cy       = fy * grid_height - grid_height / 2
    rot_cx       = pre_cx * math.cos(angle_rad) - pre_cy * math.sin(angle_rad)
    rot_cy       = pre_cx * math.sin(angle_rad) + pre_cy * math.cos(angle_rad)
    focus_in_rx  = rw / 2 + rot_cx
    focus_in_ry  = rh / 2 + rot_cy

    paste_x = int(canvas_width  / 2 - focus_in_rx)
    paste_y = int(canvas_height / 2 - focus_in_ry)

    canvas = Image.new("RGBA", (canvas_width, canvas_height), (10, 10, 12, 255))
    canvas.paste(rotated, (paste_x, paste_y), rotated)
    return canvas


def ensure_minimum_tiles(
    tile_images: list[Image.Image], minimum_count: int
) -> list[Image.Image]:
    """Repeat available tiles until we reach the minimum count for the grid."""
    if len(tile_images) >= minimum_count or not tile_images:
        return tile_images
    padded = list(tile_images)
    for tile in itertools.cycle(tile_images):
        if len(padded) >= minimum_count:
            break
        padded.append(tile.copy())
    return padded


def apply_gradient(
    canvas: Image.Image, accent: tuple[int, int, int]
) -> Image.Image:
    """
    Composite four directional gradient overlays onto the canvas (RGBA in, RGBA out):
      • dark left-edge fade   (readability for text rendered on focused/cover)
      • dark bottom vignette  (grounds the grid)
      • dark bottom-left corner radial
      • accent-coloured top-right corner glow (blurred for a soft halo)
    """
    width, height = canvas.size

    def make_linear_gradient(gw: int, gh: int, direction: str) -> Image.Image:
        img    = Image.new("RGBA", (gw, gh), (0, 0, 0, 0))
        pixels = img.load()

        if direction == "left":
            for x in range(gw):
                mix   = max(0.0, 1.0 - x / (gw * 0.45))
                alpha = int(200 * mix ** 1.6)
                if alpha:
                    color = (6, 6, 8, alpha)
                    for y in range(gh):
                        pixels[x, y] = color

        elif direction == "bottom":
            for y in range(gh):
                mix   = max(0.0, (y - gh * 0.50) / (gh * 0.50))
                alpha = int(200 * mix ** 1.4)
                if alpha:
                    color = (6, 6, 8, alpha)
                    for x in range(gw):
                        pixels[x, y] = color

        elif direction == "corner_bl":
            max_diag = math.hypot(gw, gh)
            for x in range(gw):
                for y in range(gh):
                    dist  = math.hypot(x, gh - y)
                    mix   = dist / max_diag
                    base  = max(0.0, 1.0 - mix / 0.60)
                    alpha = int(230 * base ** 2.2)
                    if alpha:
                        pixels[x, y] = (6, 6, 8, min(255, alpha))

        elif direction == "corner_tr_color":
            max_diag     = math.hypot(gw, gh)
            red, grn, bl = accent
            for x in range(gw):
                for y in range(gh):
                    dist  = math.hypot(gw - x, y)
                    mix   = dist / max_diag
                    base  = max(0.0, 1.0 - mix / 0.72)
                    alpha = int(118 * base ** 1.9)
                    if alpha:
                        pixels[x, y] = (red, grn, bl, min(255, alpha))

        return img

    # Corner gradients built at 1/4 size then scaled up (much faster per-pixel loop).
    left_grad    = make_linear_gradient(width,      height,      "left")
    bottom_grad  = make_linear_gradient(width,      height,      "bottom")
    small_bl     = make_linear_gradient(width // 4, height // 4, "corner_bl")
    corner_grad  = small_bl.resize((width, height), Image.BILINEAR)
    small_tr     = make_linear_gradient(width // 4, height // 4, "corner_tr_color")
    accent_grad  = small_tr.resize((width, height), Image.BILINEAR)
    accent_grad  = accent_grad.filter(ImageFilter.GaussianBlur(radius=max(28, width // 64)))

    result = Image.alpha_composite(canvas,  corner_grad)
    result = Image.alpha_composite(result,  left_grad)
    result = Image.alpha_composite(result,  bottom_grad)
    result = Image.alpha_composite(result,  accent_grad)
    return result


def render_prism_backdrop(images: list[Image.Image], slug: str) -> Image.Image:
    """
    Build a 1920x1080 Prism-style tilted-grid backdrop from downloaded PIL Images.
    Accent color is derived deterministically from the catalog slug.
    Returns an RGBA image.
    """
    accent      = default_accent_for_label(slug)
    tile_images = ensure_minimum_tiles(images, 12)
    canvas      = build_tilted_grid(
        tile_images, CANVAS_W, CANVAS_H, scale=1.0,
        focus_x=FOCUS_X, focus_y=FOCUS_Y,
    )
    return apply_gradient(canvas, accent)

# ─── Hero Banner (focused / cover) ────────────────────────────────────────────────────────────

def _find_font_path() -> str | None:
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def _load_font(size: int, font_path: str | None = None):
    path = font_path or _find_font_path()
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    log.warning("Helvetica-equivalent font not found — using Pillow built-in.")
    return ImageFont.load_default()


def _text_bbox(text: str, font) -> tuple[int, int]:
    dummy = Image.new("RGB", (1, 1))
    draw  = ImageDraw.Draw(dummy)
    bb    = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0], bb[3] - bb[1]


def _fit_font(text: str, max_w: int, max_h: int, font_path: str | None):
    """Binary-search for the largest font size that fits text within max_w x max_h."""
    lo, hi = 28, 300
    best   = _load_font(lo, font_path)
    while lo <= hi:
        mid    = (lo + hi) // 2
        f      = _load_font(mid, font_path)
        tw, th = _text_bbox(text, f)
        if tw <= max_w and th <= max_h:
            best = f
            lo   = mid + 1
        else:
            hi = mid - 1
    return best


def _crop_to_ratio(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    iw, ih   = img.size
    target_r = target_w / target_h
    src_r    = iw / ih
    if src_r > target_r:
        new_w = int(ih * target_r)
        return img.crop(((iw - new_w) // 2, 0, (iw - new_w) // 2 + new_w, ih))
    new_h = int(iw / target_r)
    return img.crop((0, (ih - new_h) // 2, iw, (ih - new_h) // 2 + new_h))


def _make_left_gradient(w: int, h: int, solid_pct: float = 0.25) -> Image.Image:
    """Solid black to cubic ease-out to transparent over the left 65% of width."""
    grad      = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    pixels    = grad.load()
    solid_end = int(w * solid_pct)
    fade_end  = int(w * 0.65)
    for x in range(fade_end):
        if x <= solid_end:
            alpha = 255
        else:
            t     = (x - solid_end) / (fade_end - solid_end)
            alpha = int(255 * (1.0 - t) ** 2.2)
        for y in range(h):
            pixels[x, y] = (0, 0, 0, alpha)
    return grad


def _render_glow_text(
    canvas: Image.Image,
    text: str,
    font,
    pos: tuple[int, int],
    text_rgb: tuple[int, int, int] = (255, 255, 255),
    glow_rgb: tuple[int, int, int] = (255, 255, 255),
    glow_radius: int = 22,
    layer_opacity: float = 0.88,
) -> Image.Image:
    """Composite text with dual-pass Gaussian glow onto canvas."""
    size        = canvas.size
    glow_base   = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(glow_base).text(pos, text, font=font, fill=(*glow_rgb, 230))
    glow_wide   = glow_base.filter(ImageFilter.GaussianBlur(radius=glow_radius))
    glow_narrow = glow_base.filter(ImageFilter.GaussianBlur(radius=glow_radius // 2))
    glow_layer  = Image.alpha_composite(glow_wide, glow_narrow)
    text_layer  = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(text_layer).text(pos, text, font=font, fill=(*text_rgb, 255))
    overlay = Image.alpha_composite(glow_layer, text_layer)
    base    = canvas.convert("RGBA")
    result  = Image.alpha_composite(base, overlay)
    if layer_opacity < 1.0:
        result = Image.blend(base, result, layer_opacity)
    return result


def _render_plain_text(
    canvas: Image.Image,
    text: str,
    font,
    pos: tuple[int, int],
    text_rgb: tuple[int, int, int] = (255, 255, 255),
    layer_opacity: float = 0.88,
) -> Image.Image:
    """Composite plain text (no glow) onto canvas at layer_opacity."""
    text_layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(text_layer).text(pos, text, font=font, fill=(*text_rgb, 255))
    base   = canvas.convert("RGBA")
    result = Image.alpha_composite(base, text_layer)
    if layer_opacity < 1.0:
        result = Image.blend(base, result, layer_opacity)
    return result


def render_hero_banner(
    backdrop: Image.Image,
    catalog_slug: str,
    with_glow: bool,
) -> Image.Image:
    """
    Compose a 1920x1080 hero banner:
      1. Crop/scale backdrop to fill canvas (accepts any PIL Image mode/size).
      2. Apply left-side gradient (solid black to transparent at ~65% width).
      3. Fit ALL-CAPS catalog_slug text into the left third, vertically centred.
      4. Render with outer glow (focused) or without (cover).
    Returns an RGB Image.
    """
    bg = _crop_to_ratio(backdrop.convert("RGBA"), CANVAS_W, CANVAS_H).resize(
        (CANVAS_W, CANVAS_H), Image.LANCZOS
    )
    bg = Image.alpha_composite(bg, _make_left_gradient(CANVAS_W, CANVAS_H, solid_pct=0.25))

    label     = catalog_slug.upper()
    font_path = _find_font_path()
    max_tw    = int(CANVAS_W * 0.33) - 80
    max_th    = int(CANVAS_H * 0.45)
    font      = _fit_font(label, max_tw, max_th, font_path)

    _, th = _text_bbox(label, font)
    x = 60
    y = (CANVAS_H - th) // 2

    if with_glow:
        result = _render_glow_text(bg, label, font, (x, y), layer_opacity=0.88)
    else:
        result = _render_plain_text(bg, label, font, (x, y), layer_opacity=0.88)

    return result.convert("RGB")

# ─── I/O Helpers ──────────────────────────────────────────────────────────────────────────

def save_dual(img: Image.Image, base_path: Path) -> None:
    """Write image as both .jpg and .webp next to each other."""
    rgb = img.convert("RGB")
    rgb.save(base_path.with_suffix(".jpg"),  "JPEG", quality=92, optimize=True)
    rgb.save(base_path.with_suffix(".webp"), "WEBP", quality=85, method=6)


def assets_exist(folder: str, slug: str, mode: str = "all") -> bool:
    """Return True if all outputs for the given mode already exist on disk."""
    base = COLLECTIONS_DIR / folder
    if mode == "backdrop":
        types = ("backdrop",)
    elif mode == "covers":
        types = ("focused", "cover")
    else:
        types = ("backdrop", "focused", "cover")
    return all(
        (base / t / f"{slug}{ext}").exists()
        for t in types
        for ext in (".jpg", ".webp")
    )

# ─── Per-catalog Orchestration ─────────────────────────────────────────────────────────────

def process_catalog(catalog: dict, folder: str, slug: str, force: bool, mode: str = "all") -> None:
    name = catalog.get("name") or catalog.get("title") or slug
    base = COLLECTIONS_DIR / folder   # collections/{folder}/

    do_backdrop = mode in ("all", "backdrop")
    do_covers   = mode in ("all", "covers")

    log.info("")
    log.info("━" * 62)
    log.info("Catalog  : %s  [%s/%s]  mode=%s", name, folder, slug, mode)
    log.info("━" * 62)

    for asset_type in ("backdrop", "cover", "focused", "title"):
        (base / asset_type).mkdir(parents=True, exist_ok=True)

    if not force and assets_exist(folder, slug, mode):
        log.info("  Assets already exist for mode=%s — skipping (use --force to regenerate).", mode)
        return

    top_backdrop: "Image.Image | None" = None

    if do_backdrop:
        log.info("  Fetching backdrop artwork …")
        backdrops, top_backdrop = fetch_all_backdrops(catalog)
        if not backdrops:
            log.warning("  No backdrop images fetched — skipping render.")
            return
        log.info("  Fetched %d backdrop image(s).", len(backdrops))
        log.info("  Rendering Prism backdrop …")
        prism = render_prism_backdrop(backdrops, slug)
        save_dual(prism, base / "backdrop" / slug)
        log.info("  ✓  backdrop/%s.jpg + .webp", slug)

    if do_covers:
        # Re-use existing Prism backdrop from disk if available; avoids re-downloading.
        backdrop_path = base / "backdrop" / f"{slug}.jpg"
        if top_backdrop is None and backdrop_path.exists():
            try:
                top_backdrop = Image.open(backdrop_path).convert("RGB")
                log.info("  Using existing backdrop/%s.jpg as hero base.", slug)
            except Exception:
                top_backdrop = None
        if top_backdrop is None:
            log.info("  Fetching one image for hero base …")
            _, top_backdrop = fetch_all_backdrops(catalog, limit=1)
        if top_backdrop is None:
            log.warning("  No image available for hero banner — skipping covers.")
            return
        log.info("  Rendering focused banner …")
        focused = render_hero_banner(top_backdrop, slug, with_glow=True)
        save_dual(focused, base / "focused" / slug)
        log.info("  ✓  focused/%s.jpg + .webp", slug)
        log.info("  Rendering cover banner …")
        cover = render_hero_banner(top_backdrop, slug, with_glow=False)
        save_dual(cover, base / "cover" / slug)
        log.info("  ✓  cover/%s.jpg + .webp", slug)

    log.info("  title/ initialized (manual assets preserved).")

# ─── CLI & Entry Point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate Nuvio TV catalog assets from nuvio-collections.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Target examples:
  --target all           Process every enabled catalog in the JSON
  --target discover      Process all catalogs under the 'discover' folder
  --target recommended   Process the specific 'recommended' catalog only
""",
    )
    parser.add_argument(
        "--target",
        default="all",
        metavar="TARGET",
        help="Folder name, catalog slug, or 'all' (default: all)",
    )
    parser.add_argument(
        "--json",
        default=str(SOURCE_JSON),
        metavar="PATH",
        help="Path to nuvio-collections.json (default: nuvio-collections.json)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate assets even if output files already exist",
    )
    parser.add_argument(
        "--mode",
        default="all",
        choices=["all", "backdrop", "covers"],
        help="Which asset types to generate: all | backdrop | covers (default: all)",
    )
    args = parser.parse_args()

    log.info("╔═════════════════════════════════════════════════════════╗")
    log.info("║          Nuvio TV · Catalog Asset Generator              ║")
    log.info("║          mode: %-40s║", args.mode)
    log.info("╚═════════════════════════════════════════════════════════╝")

    validate_env()

    json_path = Path(args.json)
    if not json_path.exists():
        log.error("Config file not found: %s", json_path)
        sys.exit(1)

    catalogs = load_catalogs(json_path)
    log.info("Loaded %d catalog definition(s) from %s.", len(catalogs), json_path)

    target  = args.target.strip().lower()
    matched: list[tuple[dict, str, str]] = []

    for catalog in catalogs:
        if not catalog.get("enabled", True):
            continue
        cat_id = catalog.get("id", "")
        parsed = parse_collection_id(cat_id)
        if parsed is None:
            continue
        folder, slug = parsed
        if target == "all" or target == folder or target == slug:
            matched.append((catalog, folder, slug))

    if not matched:
        log.warning(
            "No matching collections.* catalogs found for --target '%s'. "
            "Check that nuvio-collections.json has IDs matching "
            "'collections.{folder}.{catalog}'.",
            target,
        )
        sys.exit(0)

    log.info(
        "Processing %d catalog(s) for --target='%s' --mode='%s' (force=%s).",
        len(matched), target, args.mode, args.force,
    )

    errors = 0
    for catalog, folder, slug in matched:
        try:
            process_catalog(catalog, folder, slug, force=args.force, mode=args.mode)
        except Exception as exc:
            log.error("Fatal error in '%s/%s': %s", folder, slug, exc, exc_info=True)
            errors += 1

    log.info("")
    if errors:
        log.info("╔═════════════════════════════════════════════════════════╗")
        log.info("║  Done with %d error(s). Check logs above.               ║", errors)
        log.info("╚═════════════════════════════════════════════════════════╝")
        sys.exit(1)
    else:
        log.info("╔═════════════════════════════════════════════════════════╗")
        log.info("║              All done — no errors.                       ║")
        log.info("╚═════════════════════════════════════════════════════════╝")


if __name__ == "__main__":
    main()
