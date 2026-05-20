#!/usr/bin/env python3
"""
generate_catalog_assets.py — Nuvio TV Media Catalog Asset Generator
===========================================================================
Reads nuvio-collections.json, resolves each catalog entry whose ID matches
the pattern  collections.{folder}.{catalog},  fetches landscape backdrop
artwork from Stremio addon endpoints (or TMDb as fallback), and writes four
asset types per catalog into a FLAT directory structure:

  collections/{folder}/backdrop/{catalog}.jpg(.webp)  — landscape collage grid
  collections/{folder}/focused/{catalog}.jpg(.webp)   — hero banner + glow text
  collections/{folder}/cover/{catalog}.jpg(.webp)     — hero banner, no glow
  collections/{folder}/title/                         — init only; never overwritten

Backdrop images are fetched from ALL catalogSources (movies + series mixed).
No OAuth tokens are required — uses public Stremio addon HTTP endpoints.

Optional environment variables:
  AIOMETADATA_URL   Base URL of AIOMetadata/Stremio addon (preferred)
  TMDB_API_KEY      TMDb API key (fallback for entries without catalogSources)
"""

import io
import os
import re
import sys
import json
import math
import time
import logging
import argparse
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ─── Logging ─────────────────────────────────────────────────────────────────────────────

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

# Collage grid: landscape-only tiles adapted from prism-wallpapers T2 layout
GRID_COLS = 5
GRID_ROWS = 3
GRID_GAP  = 10
MAX_ITEMS = GRID_COLS * GRID_ROWS   # 15 landscape backdrop images

TIMEOUT    = 20      # seconds per HTTP call
RATE_SLEEP = 0.25    # keep TMDb within 40 req / 10 s

# Candidate paths for Helvetica Neue Bold equivalents on Linux CI runners
_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/urw-base35/NimbusSans-Bold.otf",
    "/usr/share/fonts/urw-base35/NimbusSans-Bold.otf",
    "/usr/share/fonts/type1/urw-base35/NimbusSans-Bold.otf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

# ─── Environment Validation ────────────────────────────────────────────────────────

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
    if isinstance(data, list):
        return data
    return data.get("catalogs", [])


def parse_collection_id(catalog_id: str) -> tuple[str, str] | None:
    """
    Match IDs of the form  collections.{folder}.{catalog}  (exactly 3 segments).
    Returns (folder, catalog_slug) or None if the pattern doesn't match.
    """
    parts = catalog_id.split(".")
    if len(parts) == 3 and parts[0] == "collections":
        return parts[1], parts[2]
    return None

# ─── Stremio Catalog Fetching (primary, unauthenticated) ─────────────────────────────
#
# The script calls {AIOMETADATA_URL}/catalog/{type}/{id}.json — a plain GET with no
# auth headers. AIOMetadata handles any provider auth internally on the server side.
# Without AIOMETADATA_URL it falls back to the public Cinemeta addon (top content).

# Cinemeta catalog IDs used when no AIOMetadata instance is configured
_CINEMETA_ID = {
    "movie":  "top",
    "series": "top",
}


def fetch_stremio_catalog(media_type: str, catalog_id: str) -> list[dict]:
    """
    Fetch metas from a Stremio addon catalog endpoint (unauthenticated GET).
    With AIOMETADATA_URL: calls {instance}/catalog/{type}/{id}.json
    Without:              falls back to Cinemeta top (generic popular content).
    Returns the metas list, or [] on failure.
    """
    if AIOMETADATA_URL:
        url = f"{AIOMETADATA_URL}/catalog/{media_type}/{catalog_id}.json"
    else:
        cinemeta_id = _CINEMETA_ID.get(media_type, "top")
        url = f"{CINEMETA_URL}/catalog/{media_type}/{cinemeta_id}.json"
        if catalog_id not in ("top", "top.byReviews"):
            log.info(
                "    No AIOMETADATA_URL — mapping '%s' to Cinemeta '%s'",
                catalog_id, cinemeta_id,
            )
    log.info("    GET %s", url)
    data = safe_get(url)
    metas = (data or {}).get("metas", [])
    log.info("    → %d meta(s)", len(metas))
    return metas


def backdrop_from_meta(meta: dict) -> Image.Image | None:
    """
    Download the landscape backdrop for a Stremio meta object.
    Tries 'background'/'backgroundImage' first (landscape), then falls
    back to 'poster' (portrait, cropped later) if no backdrop exists.
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
    """Return the catalogSources (or sources) list; [] if absent."""
    return catalog.get("catalogSources", catalog.get("sources", []))


def fetch_all_backdrops(
    catalog: dict, limit: int = MAX_ITEMS
) -> tuple[list[Image.Image], "Image.Image | None"]:
    """
    Primary data path — mixes backdrops from every entry in catalogSources,
    deduplicating by Stremio meta ID, capping at `limit` images.

    Falls back to the TMDb-based resolver when catalogSources is absent
    (backward-compatible with entries that still use metadata.discover.params).

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
            img = backdrop_from_meta(meta)
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
    items = resolve_items(catalog, limit)
    backdrops = []
    top = None
    for item in items[:limit]:
        title = item.get("title") or item.get("name") or "?"
        img = fetch_backdrop_tmdb(item)
        if img:
            log.info("    ✓ %s", title)
            if top is None:
                top = img
            backdrops.append(img)
        else:
            log.warning("    ✗ No backdrop — %s", title)
    return backdrops, top


# ─── TMDb Item Resolution (fallback when catalogSources is absent) ────────────────────

_AUTH_SOURCES  = {"trakt", "simkl"}
_ANIME_SOURCES = {"kitsu", "mal", "anilist"}

# Map catalog slug keywords → TMDb list endpoints
_SLUG_ENDPOINT = {
    "trending":    "/trending/{t}/week",
    "popular":     "/{t}/popular",
    "top_rated":   "/{t}/top_rated",
    "top-rated":   "/{t}/top_rated",
    "upcoming":    "/movie/upcoming",
    "new":         "/{t}/popular",
    "recommended": "/{t}/popular",
}


def resolve_items(catalog: dict, limit: int = MAX_ITEMS) -> list[dict]:
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
        params = {k: v for k, v in discover["params"].items() if v is not None}
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

# ─── TMDb Backdrop Fetching (fallback only) ───────────────────────────────────────────

def fetch_backdrop_tmdb(item: dict) -> Image.Image | None:
    """
    Fetch the highest-quality landscape backdrop for a TMDb item dict.
    Used only by the TMDb fallback path (when catalogSources is absent).
    Tries the /images endpoint first, then falls back to backdrop_path.
    """
    tmdb_id   = str(item.get("id", ""))
    tmdb_type = item.get("_tmdb_type", "movie")
    if not tmdb_id:
        return None

    time.sleep(RATE_SLEEP)

    data = safe_get(f"{TMDB_BASE}/{tmdb_type}/{tmdb_id}/images",
                    {"api_key": TMDB_API_KEY})
    if data:
        backdrops = sorted(data.get("backdrops", []),
                           key=lambda b: b.get("vote_average", 0), reverse=True)
        if backdrops:
            img = download_image(f"{TMDB_IMG_BASE}/original{backdrops[0]['file_path']}")
            if img:
                return img

    bp = item.get("backdrop_path")
    if bp:
        return download_image(f"{TMDB_IMG_BASE}/w1280{bp}")
    return None

# ─── Collage Backdrop (T2-style landscape grid) ───────────────────────────────────────
#
# Adapted from bramst0ne/prism-wallpapers backdrop_T2 — tiling logic only,
# simplified to a pure landscape (16:9) grid on a single 1920×1080 canvas.
# The grid is intentionally ~10% taller than the canvas so tiles bleed off
# the top and bottom edges, giving a seamless full-bleed appearance.
# A radial vignette + subtle left edge gradient are composited on top.

def _crop_to_ratio(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    iw, ih = img.size
    target_r = target_w / target_h
    src_r    = iw / ih
    if src_r > target_r:
        new_w = int(ih * target_r)
        return img.crop(((iw - new_w) // 2, 0, (iw - new_w) // 2 + new_w, ih))
    new_h = int(iw / target_r)
    return img.crop((0, (ih - new_h) // 2, iw, (ih - new_h) // 2 + new_h))


def _build_vignette(w: int, h: int) -> Image.Image:
    """Radial border vignette composited over the tile grid for depth."""
    vig  = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(vig)
    steps = 50
    for i in range(steps, 0, -1):
        t     = (steps - i) / steps
        alpha = int(t ** 2.2 * 165)
        inset = i * max(w, h) // (steps * 6)
        draw.rounded_rectangle(
            [inset, inset, w - inset, h - inset],
            radius=max(1, inset * 2),
            fill=(0, 0, 0, alpha),
        )
    return vig


def _collage_edge_gradient(w: int, h: int) -> Image.Image:
    """Subtle left/right edge darkening for the collage backdrop."""
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    pixels  = overlay.load()
    fade_w  = int(w * 0.18)
    for x in range(fade_w):
        t     = 1.0 - (x / fade_w)
        alpha = int(140 * (t ** 1.6))
        if alpha:
            for y in range(h):
                pixels[x, y] = (6, 8, 14, alpha)
    for x in range(w - fade_w, w):
        t     = (x - (w - fade_w)) / fade_w
        alpha = int(140 * (t ** 1.6))
        if alpha:
            for y in range(h):
                pixels[x, y] = (6, 8, 14, alpha)
    return overlay


def render_collage_backdrop(images: list[Image.Image]) -> Image.Image:
    """
    Tile landscape images (16:9) into a 1920x1080 canvas in GRID_ROWS x GRID_COLS
    cells, repeating images cyclically to fill the grid. The grid height slightly
    overflows the canvas so tiles bleed equally off top and bottom.
    A vignette and edge gradient are composited on top.
    """
    gap     = GRID_GAP
    tile_w  = (CANVAS_W - (GRID_COLS - 1) * gap) // GRID_COLS
    tile_h  = round(tile_w * 9 / 16)
    grid_h  = GRID_ROWS * tile_h + (GRID_ROWS - 1) * gap
    y_start = (CANVAS_H - grid_h) // 2     # negative -> bleed top + bottom

    canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), (10, 10, 16))

    total_slots = GRID_ROWS * GRID_COLS
    pool = (images * math.ceil(total_slots / max(len(images), 1)))[:total_slots]

    for slot, img in enumerate(pool):
        row = slot // GRID_COLS
        col = slot %  GRID_COLS
        try:
            thumb = _crop_to_ratio(img, tile_w, tile_h).resize(
                (tile_w, tile_h), Image.LANCZOS
            )
            x = col * (tile_w + gap)
            y = y_start + row * (tile_h + gap)
            canvas.paste(thumb, (x, y))
        except Exception as exc:
            log.warning("  Tile [%d,%d] failed: %s", row, col, exc)

    rgba = canvas.convert("RGBA")
    rgba = Image.alpha_composite(rgba, _build_vignette(CANVAS_W, CANVAS_H))
    rgba = Image.alpha_composite(rgba, _collage_edge_gradient(CANVAS_W, CANVAS_H))
    return rgba.convert("RGB")

# ─── Hero Banner (focused / cover) ───────────────────────────────────────────────────────────

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
    """Return (width, height) of the rendered text string."""
    dummy = Image.new("RGB", (1, 1))
    draw  = ImageDraw.Draw(dummy)
    bb    = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0], bb[3] - bb[1]


def _fit_font(text: str, max_w: int, max_h: int, font_path: str | None):
    """Binary-search for the largest font size where text fits in max_w x max_h."""
    lo, hi = 28, 300
    best   = _load_font(lo, font_path)
    while lo <= hi:
        mid = (lo + hi) // 2
        f   = _load_font(mid, font_path)
        tw, th = _text_bbox(text, f)
        if tw <= max_w and th <= max_h:
            best = f
            lo   = mid + 1
        else:
            hi = mid - 1
    return best


def _make_left_gradient(w: int, h: int, solid_pct: float = 0.25) -> Image.Image:
    """
    Solid black for the leftmost solid_pct of the width, then a smooth
    cubic ease-out curve fading to transparent by ~65% of the width.
    """
    grad   = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    pixels = grad.load()
    solid_end = int(w * solid_pct)
    fade_end  = int(w * 0.65)

    for x in range(fade_end):
        if x <= solid_end:
            alpha = 255
        else:
            t     = (x - solid_end) / (fade_end - solid_end)  # 0 -> 1
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
    """
    Composite text with an outer glow onto canvas.
    Glow is built by blurring the text silhouette in two passes
    (wide + narrow) and compositing beneath the sharp text layer.
    The entire overlay (glow + text) is blended at layer_opacity.
    """
    size = canvas.size

    # Glow: wide pass + narrow pass stacked for a pronounced halo
    glow_base = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(glow_base).text(pos, text, font=font,
                                   fill=(*glow_rgb, 230))
    glow_wide   = glow_base.filter(ImageFilter.GaussianBlur(radius=glow_radius))
    glow_narrow = glow_base.filter(ImageFilter.GaussianBlur(radius=glow_radius // 2))
    glow_layer  = Image.alpha_composite(glow_wide, glow_narrow)

    # Sharp text layer
    text_layer = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(text_layer).text(pos, text, font=font,
                                    fill=(*text_rgb, 255))

    # Build overlay: glow first, then crisp text on top
    overlay = Image.alpha_composite(glow_layer, text_layer)

    # Blend overlay onto canvas at layer_opacity
    base   = canvas.convert("RGBA")
    result = Image.alpha_composite(base, overlay)
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
    ImageDraw.Draw(text_layer).text(pos, text, font=font,
                                    fill=(*text_rgb, 255))
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
      1. Crop/scale backdrop to fill the canvas.
      2. Apply left-side gradient (solid black -> transparent at ~65% width).
      3. Fit uppercase catalog_slug text into the left third, vertically centred.
      4. Render text with outer glow (focused) or without (cover).
    Returns an RGB Image.
    """
    # 1. Full-bleed backdrop
    bg = _crop_to_ratio(backdrop, CANVAS_W, CANVAS_H).resize(
        (CANVAS_W, CANVAS_H), Image.LANCZOS
    ).convert("RGBA")

    # 2. Left gradient
    bg = Image.alpha_composite(bg, _make_left_gradient(CANVAS_W, CANVAS_H, solid_pct=0.25))

    # 3. Fit text into left-third zone (with 60 px margins)
    label     = catalog_slug.upper()
    font_path = _find_font_path()
    max_tw    = int(CANVAS_W * 0.33) - 80   # available width inside left third
    max_th    = int(CANVAS_H * 0.45)        # cap at 45% of height
    font      = _fit_font(label, max_tw, max_th, font_path)

    tw, th = _text_bbox(label, font)
    x = 60                          # left margin
    y = (CANVAS_H - th) // 2       # vertically centred

    # 4. Render
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


def assets_exist(folder: str, slug: str) -> bool:
    """Return True if all six generated outputs already exist on disk."""
    base = COLLECTIONS_DIR / folder
    return all(
        (base / asset_type / f"{slug}{ext}").exists()
        for asset_type in ("backdrop", "focused", "cover")
        for ext in (".jpg", ".webp")
    )

# ─── Per-catalog Orchestration ─────────────────────────────────────────────────────────────

def process_catalog(catalog: dict, folder: str, slug: str, force: bool) -> None:
    name = catalog.get("name", slug)
    # Correct path: collections/{folder}/{asset_type}/{slug}.jpg
    base = COLLECTIONS_DIR / folder

    log.info("")
    log.info("━" * 62)
    log.info("Catalog  : %s  [%s/%s]", name, folder, slug)
    log.info("Output   : %s/{backdrop,cover,focused}/%s.jpg", base, slug)
    log.info("━" * 62)

    # Initialize all asset-type directories; title/ is never written by automation
    for asset_type in ("backdrop", "cover", "focused", "title"):
        (base / asset_type).mkdir(parents=True, exist_ok=True)

    if not force and assets_exist(folder, slug):
        log.info("  All assets already exist — skipping (use --force to regenerate).")
        return

    # Fetch backdrops — mixed from all catalogSources (movies + series combined)
    log.info("  Fetching backdrop artwork …")
    backdrops, top_backdrop = fetch_all_backdrops(catalog)

    if not backdrops:
        log.warning("  No backdrop images fetched — skipping render.")
        return

    log.info("  Fetched %d backdrop image(s).", len(backdrops))

    # ── A. backdrop/{slug}.jpg — landscape collage grid ───────────────────────
    log.info("  Rendering collage backdrop …")
    collage = render_collage_backdrop(backdrops)
    save_dual(collage, base / "backdrop" / slug)
    log.info("  ✓  backdrop/%s.jpg + .webp", slug)

    # ── B. focused/{slug}.jpg — hero banner with outer glow ───────────────────
    log.info("  Rendering focused banner …")
    focused = render_hero_banner(top_backdrop, slug, with_glow=True)
    save_dual(focused, base / "focused" / slug)
    log.info("  ✓  focused/%s.jpg + .webp", slug)

    # ── C. cover/{slug}.jpg — hero banner without glow ────────────────────────
    log.info("  Rendering cover banner …")
    cover = render_hero_banner(top_backdrop, slug, with_glow=False)
    save_dual(cover, base / "cover" / slug)
    log.info("  ✓  cover/%s.jpg + .webp", slug)

    # ── D. title/ — initialized above; never written to by automation ─────────
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
    args = parser.parse_args()

    log.info("╔═════════════════════════════════════════════════════════╗")
    log.info("║          Nuvio TV · Catalog Asset Generator              ║")
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
            continue                        # skip non-collections IDs silently
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
        "Processing %d catalog(s) for --target='%s' (force=%s).",
        len(matched), target, args.force,
    )

    errors = 0
    for catalog, folder, slug in matched:
        try:
            process_catalog(catalog, folder, slug, force=args.force)
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
