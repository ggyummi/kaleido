#!/usr/bin/env python3
"""
generate_catalog_assets.py — Nuvio TV Media Catalog Asset Generator
===========================================================================
Reads nuvio-collections.json, resolves each catalog entry whose ID matches
the pattern  collections.{folder}.{catalog},  fetches landscape backdrop
artwork from Stremio addon endpoints (or TMDb as fallback), and writes four
asset types per catalog into a FLAT directory structure:

  collections/{folder}/backdrop/{catalog}.jpg(.webp)           — Prism 3D tilted-grid collage
  collections/{folder}/backdrop/{catalog}_t1_tilt.jpg(.webp)   — T1 perspective warp
  collections/{folder}/backdrop/{catalog}_t1_flat.jpg(.webp)   — T1 tilt only
  collections/{folder}/backdrop/{catalog}_t2_tilt.jpg(.webp)   — T2 mixed columns, warp
  collections/{folder}/backdrop/{catalog}_t2_flat.jpg(.webp)   — T2 mixed columns, flat
  collections/{folder}/cover/{catalog}_landscape.jpg(.webp)    — full-brightness card, 1920x1080
  collections/{folder}/cover/{catalog}_portrait.jpg(.webp)     — full-brightness card, 680x1000
  collections/{folder}/focused/{catalog}_landscape.jpg(.webp)  — dimmed card, 1920x1080
  collections/{folder}/focused/{catalog}_portrait.jpg(.webp)   — dimmed card, 680x1000
  collections/{folder}/title/                                   — init only; never overwritten

Cover and focused cards feature:
  * A color grade overlay derived from the image's own dominant color
  * A bottom gradient that fades to a darkened version of that same color
  * The full catalog title (emoji stripped) centred in the gradient area

Optional environment variables:
  AIOMETADATA_URL   Base URL of AIOMetadata/Stremio addon (preferred)
  TMDB_API_KEY      TMDb API key (fallback for entries without catalogSources)
  FANART_API_KEY    Fanart.tv API key (English title logo overlays on tiles)
"""

import colorsys
import io
import itertools
import math
import os
import re
import random
import sys
import json
import time
import logging
import argparse
from pathlib import Path

import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

# --- Logging -----------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("nuvio.catalog")

# --- Global Config -----------------------------------------------------------

TMDB_API_KEY    = os.environ.get("TMDB_API_KEY", "")
TMDB_BASE       = "https://api.themoviedb.org/3"
TMDB_IMG_BASE   = "https://image.tmdb.org/t/p"

AIOMETADATA_URL = os.environ.get("AIOMETADATA_URL", "").rstrip("/")
FANART_API_KEY  = os.environ.get("FANART_API_KEY", "")
FANART_BASE     = "https://webservice.fanart.tv/v3"

COLLECTIONS_DIR = Path("collections")
SOURCE_JSON     = Path("nuvio-collections.json")

# Canvas dimensions
CANVAS_W, CANVAS_H     = 1920, 1080
PORTRAIT_W, PORTRAIT_H = 680, 1000

# Cover card tuning
FOCUSED_DIM       = 0.50   # focused state dim (0=black, 1=no change)
COLOR_INTENSITY   = 0.40   # color grade overlay strength
GRADIENT_START    = 0.72   # where bottom fade begins, fraction from top
GRADIENT_DARKNESS = 0.95   # max opacity of gradient at bottom

MAX_TILES  = 40
TIMEOUT    = 20
RATE_SLEEP = 0.25

# --- Prism Tile Geometry Constants -------------------------------------------

CARD_RADIUS = 9
TILT_DEG    = 10
TILE_W      = 372
TILE_H      = 210
GAP         = 9
ROWS        = 10
COLS        = 10
STAGGER     = 0.5
FOCUS_X     = 0.5
FOCUS_Y     = 0.53

# --- Font Candidates (regular weight) ----------------------------------------

_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/urw-base35/NimbusSans-Regular.otf",
    "/usr/share/fonts/urw-base35/NimbusSans-Regular.otf",
    "/usr/share/fonts/type1/urw-base35/NimbusSans-Regular.otf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

# --- Emoji Stripping ---------------------------------------------------------

_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"
    "\U0001F300-\U0001F5FF"
    "\U0001F680-\U0001F6FF"
    "\U0001F1E0-\U0001F1FF"
    "\U00002500-\U00002BFF"
    "\U0001F900-\U0001F9FF"
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "]+",
    flags=re.UNICODE,
)


def strip_emoji(text: str) -> str:
    return _EMOJI_RE.sub("", text).strip()

# --- Environment Validation --------------------------------------------------

def validate_env() -> None:
    if not AIOMETADATA_URL and not TMDB_API_KEY:
        log.warning(
            "Neither AIOMETADATA_URL nor TMDB_API_KEY is set. "
            "Catalogs with catalogSources will produce no images."
        )
    elif not AIOMETADATA_URL:
        log.info("AIOMETADATA_URL not set — falling back to TMDb Discover.")

# --- Manifest Discovery ------------------------------------------------------

def fetch_manifest_catalog_ids(base_url: str) -> set[str]:
    url  = f"{base_url}/manifest.json"
    log.info("Fetching manifest: %s", url)
    data = safe_get(url)
    if not data:
        raise RuntimeError(f"Could not fetch manifest from {url}.")
    ids = {c["id"] for c in data.get("catalogs", []) if "id" in c}
    log.info("Manifest loaded — %d catalog ID(s) available.", len(ids))
    return ids

# --- Fanart.tv Logo Fetching -------------------------------------------------

def _resolve_fanart_id(item_id: str, media_type: str) -> "tuple[str, str] | None":
    if not item_id or not TMDB_API_KEY:
        return None
    if item_id.startswith("tmdb:"):
        tmdb_id = item_id.replace("tmdb:", "").strip()
        if not tmdb_id:
            return None
        if media_type == "movie":
            return tmdb_id, "movies"
        ext = safe_get(f"{TMDB_BASE}/tv/{tmdb_id}/external_ids", {"api_key": TMDB_API_KEY})
        if ext and ext.get("tvdb_id"):
            return str(ext["tvdb_id"]), "tv"
        return tmdb_id, "tv"
    if item_id.startswith("tt"):
        find_data = safe_get(
            f"{TMDB_BASE}/find/{item_id}",
            {"api_key": TMDB_API_KEY, "external_source": "imdb_id"},
        )
        if not find_data:
            return None
        if media_type == "movie":
            results = find_data.get("movie_results", [])
            if results:
                return str(results[0]["id"]), "movies"
            return None
        results = find_data.get("tv_results", [])
        if not results:
            return None
        tmdb_tv_id = results[0]["id"]
        ext = safe_get(f"{TMDB_BASE}/tv/{tmdb_tv_id}/external_ids", {"api_key": TMDB_API_KEY})
        if ext and ext.get("tvdb_id"):
            return str(ext["tvdb_id"]), "tv"
        return str(tmdb_tv_id), "tv"
    return None


def fetch_fanart_logo(imdb_id: str, media_type: str) -> "Image.Image | None":
    if not FANART_API_KEY:
        return None
    resolved = _resolve_fanart_id(imdb_id, media_type)
    if not resolved:
        return None
    fanart_id, fanart_type = resolved
    logo_key = "hdmovielogo" if fanart_type == "movies" else "hdtvlogo"
    data = safe_get(f"{FANART_BASE}/{fanart_type}/{fanart_id}", {"api_key": FANART_API_KEY})
    if not data:
        return None
    logos    = data.get(logo_key, [])
    en_logos = [l for l in logos if l.get("lang") == "en"]
    if not en_logos:
        return None
    en_logos.sort(key=lambda l: int(l.get("likes", 0)), reverse=True)
    url = en_logos[0].get("url")
    if not url:
        return None
    return _download_logo_rgba(url)


def composite_logo_on_tile(tile: Image.Image, logo: Image.Image) -> Image.Image:
    tw, th   = tile.size
    max_lw   = int(tw * 0.65)
    max_lh   = int(th * 0.28)
    lw, lh   = logo.size
    scale    = min(max_lw / lw, max_lh / lh, 1.0)
    new_lw   = max(1, int(lw * scale))
    new_lh   = max(1, int(lh * scale))
    logo_r   = logo.resize((new_lw, new_lh), Image.LANCZOS)
    pad_x    = int(tw * 0.08)
    pad_y    = int(th * 0.08)
    logo_x   = pad_x
    logo_y   = th - new_lh - pad_y
    shadow   = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    draw     = ImageDraw.Draw(shadow)
    grad_top = max(0, logo_y - int(th * 0.06))
    for y in range(grad_top, th):
        t     = (y - grad_top) / max(1, th - grad_top)
        alpha = int(170 * (t ** 1.4))
        draw.line([(0, y), (tw, y)], fill=(0, 0, 0, alpha))
    result = tile.convert("RGBA")
    result = Image.alpha_composite(result, shadow)
    result.paste(logo_r, (logo_x, logo_y), logo_r)
    return result

# --- HTTP Helpers ------------------------------------------------------------

def safe_get(url: str, params: dict | None = None, retries: int = 3) -> dict | None:
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 5))
                log.warning("Rate-limited — waiting %ds ...", wait)
                time.sleep(wait)
                continue
            if 400 <= r.status_code < 500:
                log.warning("HTTP %d for %s — not retrying.", r.status_code, url)
                return None
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


def _download_logo_rgba(url: str) -> "Image.Image | None":
    try:
        r = requests.get(url, timeout=TIMEOUT)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content))
        if img.mode == "P" and "transparency" in img.info:
            return img.convert("RGBA")
        if img.mode not in ("RGBA", "LA"):
            log.warning("Fanart.tv logo has no alpha channel (mode=%s) — skipping: %s", img.mode, url)
            return None
        return img.convert("RGBA")
    except Exception as exc:
        log.warning("Logo download failed (%s): %s", url, exc)
        return None

# --- JSON Parsing ------------------------------------------------------------

def load_catalogs(json_path: Path) -> list[dict]:
    log.info("Loading %s ...", json_path)
    with open(json_path, encoding="utf-8") as fh:
        data = json.load(fh)
    items = data if isinstance(data, list) else data.get("catalogs", [])
    flat: list[dict] = []
    for item in items:
        if "folders" in item:
            flat.extend(item["folders"])
        else:
            flat.append(item)
    return flat


def parse_collection_id(catalog_id: str) -> tuple[str, str] | None:
    parts = catalog_id.split(".")
    if len(parts) == 3 and parts[0] == "collections":
        return parts[1], parts[2]
    return None

# --- Stremio Catalog Fetching ------------------------------------------------

def fetch_stremio_catalog(media_type: str, catalog_id: str) -> list[dict]:
    if not AIOMETADATA_URL:
        log.error("AIOMETADATA_URL is not set. Cannot fetch catalog '%s/%s'.", media_type, catalog_id)
        return []
    url  = f"{AIOMETADATA_URL}/catalog/{media_type}/{catalog_id}.json"
    log.info(" GET %s", url)
    data = safe_get(url)
    if data is not None:
        metas = data.get("metas", [])
        log.info(" -> %d meta(s)", len(metas))
        return metas
    log.error(" AIOMetadata fetch failed for '%s/%s'.", media_type, catalog_id)
    return []


def backdrop_from_meta(meta: dict) -> Image.Image | None:
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
    raw    = catalog.get("catalogSources", catalog.get("sources", []))
    result = []
    for src in raw:
        if "catalogId" in src and "id" not in src:
            src = {**src, "id": src["catalogId"]}
        result.append(src)
    return result


def fetch_all_backdrops(
    catalog: dict, limit: int = MAX_TILES
) -> tuple[list[Image.Image], list, "Image.Image | None"]:
    sources = get_catalog_sources(catalog)

    if sources:
        all_metas: list[dict] = []
        seen: set[str] = set()
        for src in sources:
            metas = fetch_stremio_catalog(src["type"], src["id"])
            for meta in metas:
                mid = meta.get("id", "")
                if mid and mid not in seen:
                    seen.add(mid)
                    meta["_fanart_type"] = src["type"]
                    all_metas.append(meta)

        backdrops: list[Image.Image] = []
        logos: list["Image.Image | None"] = []
        top: Image.Image | None = None
        if not FANART_API_KEY:
            log.warning("FANART_API_KEY not set — skipping logo overlays for this catalog.")
        for meta in all_metas[:limit]:
            name = meta.get("name", meta.get("id", "?"))
            img  = backdrop_from_meta(meta)
            if img:
                if top is None:
                    top = img
                backdrops.append(img)
                logo: "Image.Image | None" = None
                if FANART_API_KEY:
                    item_id  = meta.get("id", "")
                    src_type = meta.get("_fanart_type", "movie")
                    logo     = fetch_fanart_logo(item_id, src_type)
                    if logo:
                        log.info("     + EN logo -- %s", name)
                    else:
                        log.info("     - no EN logo (id=%s, type=%s) -- %s", item_id, src_type, name)
                logos.append(logo)
        return backdrops, logos, top

    log.info("  No catalogSources — using TMDb resolver as fallback.")
    items     = resolve_items(catalog, limit)
    backdrops = []
    top       = None
    for item in items[:limit]:
        title = item.get("title") or item.get("name") or "?"
        img   = fetch_backdrop_tmdb(item)
        if img:
            log.info("    + %s", title)
            if top is None:
                top = img
            backdrops.append(img)
        else:
            log.warning("    - No backdrop -- %s", title)
    return backdrops, [None] * len(backdrops), top

# --- TMDb Item Resolution (fallback) ----------------------------------------

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

    meta     = catalog.get("metadata", {})
    discover = meta.get("discover", {})
    if discover and "params" in discover:
        media_type = "tv" if discover.get("mediaType") == "tv" else "movie"
        params     = {k: v for k, v in discover["params"].items() if v is not None}
        params["api_key"] = TMDB_API_KEY
        data  = safe_get(f"{TMDB_BASE}/discover/{media_type}", params)
        items = (data or {}).get("results", [])
        return _tag(items, media_type)[:limit]

    if source in _ANIME_SOURCES or "anime" in cat_id:
        params = {
            "api_key": TMDB_API_KEY, "sort_by": "popularity.desc",
            "with_genres": "16", "with_original_language": "ja", "vote_count.gte": "20",
        }
        data  = safe_get(f"{TMDB_BASE}/discover/{tmdb_type}", params)
        items = (data or {}).get("results", [])
        return _tag(items, tmdb_type)[:limit]

    parts = cat_id.split(".")
    slug  = parts[2].lower() if len(parts) >= 3 else cat_id.lower()
    for keyword, tpl in _SLUG_ENDPOINT.items():
        if slug == keyword or slug.startswith(keyword):
            url   = f"{TMDB_BASE}{tpl.replace('{t}', tmdb_type)}"
            data  = safe_get(url, {"api_key": TMDB_API_KEY, "language": "en-US"})
            items = (data or {}).get("results", [])
            return _tag(items, tmdb_type)[:limit]

    log.info("No specific route for '%s' — using TMDb popular.", name)
    data  = safe_get(f"{TMDB_BASE}/{tmdb_type}/popular", {"api_key": TMDB_API_KEY, "language": "en-US"})
    items = (data or {}).get("results", [])
    return _tag(items, tmdb_type)[:limit]


def fetch_backdrop_tmdb(item: dict) -> Image.Image | None:
    tmdb_id   = str(item.get("id", ""))
    tmdb_type = item.get("_tmdb_type", "movie")
    if not tmdb_id:
        return None
    time.sleep(RATE_SLEEP)
    data = safe_get(f"{TMDB_BASE}/{tmdb_type}/{tmdb_id}/images", {"api_key": TMDB_API_KEY})
    if data:
        bds = sorted(data.get("backdrops", []), key=lambda b: b.get("vote_average", 0), reverse=True)
        if bds:
            img = download_image(f"{TMDB_IMG_BASE}/original{bds[0]['file_path']}")
            if img:
                return img
    bp = item.get("backdrop_path")
    if bp:
        return download_image(f"{TMDB_IMG_BASE}/w1280{bp}")
    return None

# --- Prism Backdrop Engine ---------------------------------------------------

def default_accent_for_label(label: str) -> tuple[int, int, int]:
    seed = sum((i + 1) * ord(c) for i, c in enumerate(label or "Backdrop"))
    hue  = (seed % 360) / 360.0
    r, g, b = colorsys.hsv_to_rgb(hue, 0.65, 0.88)
    return (int(r * 255), int(g * 255), int(b * 255))


def rounded_rect_mask(width: int, height: int, radius: int = CARD_RADIUS) -> Image.Image:
    mask = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([0, 0, width - 1, height - 1], radius=radius, fill=255)
    return mask


def make_tile(
    image: Image.Image,
    tile_width: int,
    tile_height: int,
    logo: "Image.Image | None" = None,
) -> Image.Image:
    sw, sh = image.size
    target_ratio = tile_width / tile_height
    src_ratio    = sw / sh
    if src_ratio > target_ratio:
        new_w = int(sh * target_ratio)
        left  = (sw - new_w) // 2
        image = image.crop((left, 0, left + new_w, sh))
    else:
        new_h = int(sw / target_ratio)
        top   = (sh - new_h) // 2
        image = image.crop((0, top, sw, top + new_h))
    image         = image.resize((tile_width, tile_height), Image.LANCZOS)
    scaled_radius = max(8, int(CARD_RADIUS * tile_width / TILE_W))
    mask          = rounded_rect_mask(tile_width, tile_height, radius=scaled_radius)
    result        = Image.new("RGBA", (tile_width, tile_height), (0, 0, 0, 0))
    result.paste(image, mask=mask)
    if logo is not None:
        result = composite_logo_on_tile(result, logo)
    return result


def build_tilted_grid(
    tiles: list[Image.Image],
    canvas_width: int,
    canvas_height: int,
    scale: float = 1.0,
    focus_x: float | None = None,
    focus_y: float | None = None,
    logos: "list[Image.Image | None] | None" = None,
) -> Image.Image:
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
    focal_col = max(0, min(cols - 1, int((focal_x - focal_row * stagger_px) / (tile_width + gap))))

    cells = [(row, col) for row in range(rows) for col in range(cols)]
    cells.sort(key=lambda pos: abs(pos[0] - focal_row) + abs(pos[1] - focal_col))

    for index, (row, col) in enumerate(cells):
        if index >= len(tile_list):
            break
        x    = row * stagger_px + col * (tile_width + gap)
        y    = row * (tile_height + gap)
        logo = logos[index] if logos and index < len(logos) else None
        tile = make_tile(tile_list[index], tile_width, tile_height, logo=logo)
        grid.paste(tile, (x, y), tile)

    rotated = grid.rotate(TILT_DEG, expand=True, resample=Image.BICUBIC)
    rw, rh  = rotated.size

    angle_rad   = math.radians(-TILT_DEG)
    pre_cx      = fx * grid_width  - grid_width  / 2
    pre_cy      = fy * grid_height - grid_height / 2
    rot_cx      = pre_cx * math.cos(angle_rad) - pre_cy * math.sin(angle_rad)
    rot_cy      = pre_cx * math.sin(angle_rad) + pre_cy * math.cos(angle_rad)
    focus_in_rx = rw / 2 + rot_cx
    focus_in_ry = rh / 2 + rot_cy

    paste_x = int(canvas_width  / 2 - focus_in_rx)
    paste_y = int(canvas_height / 2 - focus_in_ry)

    canvas = Image.new("RGBA", (canvas_width, canvas_height), (10, 10, 12, 255))
    canvas.paste(rotated, (paste_x, paste_y), rotated)
    return canvas


def ensure_minimum_tiles(tile_images: list[Image.Image], minimum_count: int) -> list[Image.Image]:
    if len(tile_images) >= minimum_count or not tile_images:
        return tile_images
    padded = list(tile_images)
    for tile in itertools.cycle(tile_images):
        if len(padded) >= minimum_count:
            break
        padded.append(tile.copy())
    return padded


def apply_gradient(canvas: Image.Image, accent: tuple[int, int, int]) -> Image.Image:
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

    left_grad   = make_linear_gradient(width,      height,      "left")
    bottom_grad = make_linear_gradient(width,      height,      "bottom")
    small_bl    = make_linear_gradient(width // 4, height // 4, "corner_bl")
    corner_grad = small_bl.resize((width, height), Image.BILINEAR)
    small_tr    = make_linear_gradient(width // 4, height // 4, "corner_tr_color")
    accent_grad = small_tr.resize((width, height), Image.BILINEAR)
    accent_grad = accent_grad.filter(ImageFilter.GaussianBlur(radius=max(28, width // 64)))

    result = Image.alpha_composite(canvas,  corner_grad)
    result = Image.alpha_composite(result,  left_grad)
    result = Image.alpha_composite(result,  bottom_grad)
    result = Image.alpha_composite(result,  accent_grad)
    return result


def render_prism_backdrop(
    images: list[Image.Image],
    slug:   str,
    logos:  "list[Image.Image | None] | None" = None,
) -> Image.Image:
    accent = default_accent_for_label(slug)
    effective_logos: list["Image.Image | None"] = []
    if logos:
        effective_logos = list(logos) + [None] * max(0, len(images) - len(logos))
    tile_images = ensure_minimum_tiles(images, 12)
    canvas = build_tilted_grid(
        tile_images, CANVAS_W, CANVAS_H, scale=1.0,
        focus_x=FOCUS_X, focus_y=FOCUS_Y,
        logos=effective_logos if effective_logos else None,
    )
    return apply_gradient(canvas, accent)

# --- T1 Backdrop Engine ------------------------------------------------------

class _T1Cfg:
    __slots__ = (
        "tilt_deg", "offset_x", "offset_y",
        "landscape_w", "gap", "card_radius",
        "fade_left", "fade_right",
        "pov_x", "pov_y", "warp_strength",
        "dof_blur_max", "dof_focus_x", "dof_focus_y", "dof_falloff",
        "focus_x", "focus_y", "focus_radius", "stagger_axis",
    )

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


_T1_TILT_CFG = _T1Cfg(
    tilt_deg=-10,    offset_x=170,    offset_y=-80,
    landscape_w=400, gap=8,           card_radius=8,
    fade_left=0.30,  fade_right=1.00,
    pov_x=1.0,       pov_y=-1.0,      warp_strength=0.37,
    dof_blur_max=10.0, dof_focus_x=0.75, dof_focus_y=0.25, dof_falloff=1.5,
    focus_x=0.70,    focus_y=0.20,    focus_radius=0.35,
    stagger_axis="row",
)

_T1_FLAT_CFG = _T1Cfg(
    tilt_deg=-10,    offset_x=170,    offset_y=-80,
    landscape_w=400, gap=8,           card_radius=8,
    fade_left=0.30,  fade_right=1.00,
    pov_x=0.0,       pov_y=0.0,       warp_strength=0.0,
    dof_blur_max=10.0, dof_focus_x=0.75, dof_focus_y=0.25, dof_falloff=1.5,
    focus_x=0.75,    focus_y=0.50,    focus_radius=0.35,
    stagger_axis="row",
)


def _t1_rounded_mask(w: int, h: int, radius: int) -> Image.Image:
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    return mask


def _t1_make_tile(
    img: Image.Image, tw: int, th: int, opacity: float, cfg: "_T1Cfg",
    logo: "Image.Image | None" = None,
) -> Image.Image:
    iw, ih = img.size
    tr = tw / th
    sr = iw / ih
    if sr > tr:
        nw  = int(ih * tr)
        img = img.crop(((iw - nw) // 2, 0, (iw - nw) // 2 + nw, ih))
    else:
        nh  = int(iw / tr)
        img = img.crop((0, (ih - nh) // 2, iw, (ih - nh) // 2 + nh))
    img = img.resize((tw, th), Image.LANCZOS)
    r   = max(2, int(cfg.card_radius * tw / max(cfg.landscape_w, 1)))
    out = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    out.paste(img, mask=_t1_rounded_mask(tw, th, r))
    if opacity < 1.0:
        rc, gc, bc, ac = out.split()
        ac = ac.point(lambda v: int(v * opacity))
        out = Image.merge("RGBA", (rc, gc, bc, ac))
    if logo is not None:
        out = composite_logo_on_tile(out, logo)
    return out


def _t1_build_layout(
    landscape_pairs: "list[tuple[Image.Image, Image.Image | None]]",
    canvas_w: int,
    canvas_h: int,
    scale: float,
    cfg: "_T1Cfg",
) -> "tuple[Image.Image, int, int]":
    lw  = int(cfg.landscape_w * scale)
    lh  = int(round(lw * 9 / 16))
    gap = int(cfg.gap * scale)

    bleed_x   = (lw + gap) * 3
    bleed_y   = lh * 2 + gap * 4
    stagger_x = (lw + gap) // 2
    stagger_y = (lh + gap) // 2

    over_w = canvas_w + bleed_x * 2
    over_h = canvas_h + bleed_y * 2
    ox     = bleed_x
    oy     = bleed_y
    canvas = Image.new("RGBA", (over_w, over_h), (10, 12, 16, 255))

    l_cutoff        = max(1, int(len(landscape_pairs) * 0.35))
    pri_landscapes  = landscape_pairs[:l_cutoff]
    rest_landscapes = landscape_pairs[l_cutoff:]

    rng            = random.Random(42)
    tiles_to_place: list[dict] = []

    if cfg.stagger_axis == "row":
        y       = -bleed_y + oy
        row_idx = 0
        while y < over_h:
            row_shift = stagger_x if (row_idx % 2 == 1) else 0
            x = -bleed_x + row_shift + ox
            while x < over_w:
                screen_x     = x - ox + (lw * 0.5)
                screen_y     = y - oy + (lh * 0.5)
                norm_x       = screen_x / canvas_w
                norm_y       = screen_y / canvas_h
                depth        = max(0.0, min(1.0, norm_x))
                opacity      = cfg.fade_left + (cfg.fade_right - cfg.fade_left) * depth
                dist_focus   = math.hypot(norm_x - cfg.focus_x, norm_y - cfg.focus_y)
                is_focal     = dist_focus <= cfg.focus_radius
                is_on_screen = (0.0 <= norm_x <= 1.0) and (0.0 <= norm_y <= 1.0)
                tiles_to_place.append({
                    "x": x, "y": y, "w": lw, "h": lh, "opacity": opacity,
                    "is_focal": is_focal, "is_on_screen": is_on_screen,
                })
                x += lw + gap
            y += lh + gap
            row_idx += 1
    else:
        x       = -bleed_x
        col_idx = 0
        columns: list[dict] = []
        while x < canvas_w + bleed_x:
            columns.append({"x": x, "w": lw, "stagger": col_idx % 2 == 1})
            x += lw + gap
            col_idx += 1
        for col in columns:
            col_x = col["x"] + ox
            col_w = col["w"]
            shift = stagger_y if col["stagger"] else 0
            y     = -bleed_y + shift + oy
            while y < over_h:
                th           = max(4, int(col_w * 9 / 16))
                screen_x     = col_x - ox + (col_w * 0.5)
                screen_y     = y - oy + (th * 0.5)
                norm_x       = screen_x / canvas_w
                norm_y       = screen_y / canvas_h
                depth        = max(0.0, min(1.0, norm_x))
                opacity      = cfg.fade_left + (cfg.fade_right - cfg.fade_left) * depth
                dist_focus   = math.hypot(norm_x - cfg.focus_x, norm_y - cfg.focus_y)
                is_focal     = dist_focus <= cfg.focus_radius
                is_on_screen = (0.0 <= norm_x <= 1.0) and (0.0 <= norm_y <= 1.0)
                tiles_to_place.append({
                    "x": col_x, "y": y, "w": col_w, "h": th, "opacity": opacity,
                    "is_focal": is_focal, "is_on_screen": is_on_screen,
                })
                y += th + gap

    tiles_to_place.sort(key=lambda t: (not t["is_on_screen"], not t["is_focal"]))

    unique_pri  = list(reversed(pri_landscapes))
    unique_rest = list(reversed(rest_landscapes))
    repeat_pri  = list(pri_landscapes)
    repeat_rest = list(rest_landscapes)
    rng.shuffle(repeat_pri)
    rng.shuffle(repeat_rest)
    pri_idx  = 0
    rest_idx = 0

    for t in tiles_to_place:
        if t["is_focal"]:
            if unique_pri:
                src = unique_pri.pop()
            else:
                src = repeat_pri[pri_idx % len(repeat_pri)]
                pri_idx += 1
        else:
            if unique_rest:
                src = unique_rest.pop()
            elif repeat_rest:
                src = repeat_rest[rest_idx % len(repeat_rest)]
                rest_idx += 1
            else:
                src = repeat_pri[pri_idx % len(repeat_pri)]
                pri_idx += 1
        src_img, src_logo = src
        tile = _t1_make_tile(src_img, t["w"], t["h"], opacity=t["opacity"], cfg=cfg, logo=src_logo)
        canvas.paste(tile, (int(t["x"]), int(t["y"])), tile)

    return canvas, ox, oy


def _t1_perspective_warp(
    oversized: Image.Image,
    ox: int,
    oy: int,
    out_w: int,
    out_h: int,
    cfg: "_T1Cfg",
) -> Image.Image:
    if cfg.pov_x == 0.0 and cfg.pov_y == 0.0:
        scale      = out_w / 1920.0
        off_x      = int(cfg.offset_x * scale)
        off_y      = int(cfg.offset_y * scale)
        shifted_ox = ox - off_x
        shifted_oy = oy - off_y
        if cfg.tilt_deg != 0:
            center_x = shifted_ox + out_w / 2
            center_y = shifted_oy + out_h / 2
            rotated  = oversized.rotate(-cfg.tilt_deg, resample=Image.BICUBIC, center=(center_x, center_y))
            return rotated.crop((shifted_ox, shifted_oy, shifted_ox + out_w, shifted_oy + out_h))
        return oversized.crop((shifted_ox, shifted_oy, shifted_ox + out_w, shifted_oy + out_h))

    tl_x, tl_y = 0.0, 0.0
    tr_x, tr_y = float(out_w), 0.0
    br_x, br_y = float(out_w), float(out_h)
    bl_x, bl_y = 0.0, float(out_h)

    if cfg.pov_x > 0:
        inset_y = (out_h * cfg.warp_strength * abs(cfg.pov_x)) / 2
        tl_y += inset_y
        bl_y -= inset_y
    elif cfg.pov_x < 0:
        inset_y = (out_h * cfg.warp_strength * abs(cfg.pov_x)) / 2
        tr_y += inset_y
        br_y -= inset_y

    if cfg.pov_y > 0:
        inset_x = (out_w * cfg.warp_strength * abs(cfg.pov_y)) / 2
        tl_x += inset_x
        tr_x -= inset_x
    elif cfg.pov_y < 0:
        inset_x = (out_w * cfg.warp_strength * abs(cfg.pov_y)) / 2
        bl_x += inset_x
        br_x -= inset_x

    src_pts = [(ox, oy), (ox + out_w, oy), (ox + out_w, oy + out_h), (ox, oy + out_h)]
    dst_pts = [(tl_x, tl_y), (tr_x, tr_y), (br_x, br_y), (bl_x, bl_y)]

    A: list[list[float]] = []
    bv: list[float]      = []
    for (sx, sy), (dx, dy) in zip(src_pts, dst_pts):
        A.append([dx, dy, 1, 0,  0,  0, -sx * dx, -sx * dy])
        bv.append(sx)
        A.append([0,  0,  0, dx, dy, 1, -sy * dx, -sy * dy])
        bv.append(sy)

    try:
        coeffs = np.linalg.solve(np.array(A, dtype=np.float64), np.array(bv, dtype=np.float64))
        return oversized.transform((out_w, out_h), Image.PERSPECTIVE, tuple(coeffs), resample=Image.BICUBIC)
    except Exception:
        return oversized.crop((ox, oy, ox + out_w, oy + out_h))


def _t1_apply_dof(image: Image.Image, scale: float, cfg: "_T1Cfg") -> Image.Image:
    if cfg.dof_blur_max <= 0:
        return image

    w, h  = image.size
    fx    = cfg.dof_focus_x * w
    fy    = cfg.dof_focus_y * h
    diag  = math.hypot(w, h)

    xs       = np.linspace(0, w - 1, w, dtype=np.float32)
    ys       = np.linspace(0, h - 1, h, dtype=np.float32)
    xg, yg   = np.meshgrid(xs, ys)
    dist_map = np.sqrt((xg - fx) ** 2 + (yg - fy) ** 2) / diag
    blur_map = np.clip(dist_map ** cfg.dof_falloff, 0.0, 1.0)

    N      = 5
    max_r  = cfg.dof_blur_max * scale
    layers = [
        image if (i / N) * max_r < 0.5
        else image.filter(ImageFilter.GaussianBlur(radius=(i / N) * max_r))
        for i in range(N + 1)
    ]
    arrs = [np.array(layer, dtype=np.float32) for layer in layers]
    out  = np.zeros_like(arrs[0])

    for i in range(N):
        lo  = i / N
        hi  = (i + 1) / N
        in_ = (blur_map >= lo) & (blur_map < hi)
        t   = ((blur_map - lo) / (hi - lo + 1e-9))[in_]
        out[in_] = arrs[i][in_] * (1 - t[:, None]) + arrs[i + 1][in_] * t[:, None]

    out[blur_map >= (N - 1) / N] = arrs[N][blur_map >= (N - 1) / N]
    return Image.fromarray(out.clip(0, 255).astype(np.uint8), image.mode)


def _t1_apply_gradient(canvas: Image.Image, accent: "tuple[int, int, int]") -> Image.Image:
    w, h = canvas.size
    ar, ag, ab = accent

    def _grad_left(gw: int, gh: int) -> Image.Image:
        img = Image.new("RGBA", (gw, gh), (0, 0, 0, 0))
        px  = img.load()
        for x in range(int(gw * 0.65)):
            t = 1.0 - x / (gw * 0.65)
            a = int(240 * (t ** 1.4))
            if a:
                for y in range(gh):
                    px[x, y] = (6, 8, 12, a)
        return img

    def _grad_bottom(gw: int, gh: int) -> Image.Image:
        img = Image.new("RGBA", (gw, gh), (0, 0, 0, 0))
        px  = img.load()
        for y in range(gh):
            t = max(0.0, (y - gh * 0.55) / (gh * 0.45))
            a = int(215 * (t ** 1.3))
            if a:
                for x in range(gw):
                    px[x, y] = (6, 8, 12, a)
        return img

    def _accent_glow(gw: int, gh: int) -> Image.Image:
        img = Image.new("RGBA", (gw, gh), (0, 0, 0, 0))
        dw  = ImageDraw.Draw(img)
        for i in range(18):
            t  = i / 18
            rr = int(math.hypot(gw, gh) * (0.05 + 0.38 * t))
            aa = int(14 * (1 - t) ** 2.2)
            if aa:
                dw.ellipse([gw - rr, -rr, gw + rr, rr], fill=(ar, ag, ab, aa))
        return img

    left_side   = _grad_left(w // 4, h // 4).resize((w, h), Image.BILINEAR)
    bottom_side = _grad_bottom(w // 4, h // 4).resize((w, h), Image.BILINEAR)
    result      = Image.alpha_composite(canvas, left_side)
    result      = Image.alpha_composite(result, bottom_side)
    result      = Image.alpha_composite(result, _accent_glow(w, h))
    return result


def render_t1_backdrop(
    images:  "list[Image.Image]",
    slug:    str,
    variant: str = "tilt",
    logos:   "list[Image.Image | None] | None" = None,
) -> Image.Image:
    cfg    = _T1_TILT_CFG if variant == "tilt" else _T1_FLAT_CFG
    accent = default_accent_for_label(slug)

    n           = len(images)
    eff_logos   = list(logos) if logos else []
    eff_logos  += [None] * max(0, n - len(eff_logos))
    pairs       = list(zip(images, eff_logos[:n]))

    minimum = 4
    if 0 < len(pairs) < minimum:
        for img, logo in itertools.cycle(pairs):
            if len(pairs) >= minimum:
                break
            pairs.append((img.copy(), logo))

    over, ox, oy = _t1_build_layout(pairs, CANVAS_W, CANVAS_H, scale=1.0, cfg=cfg)
    warped       = _t1_perspective_warp(over, ox, oy, CANVAS_W, CANVAS_H, cfg)
    dof          = _t1_apply_dof(warped, scale=1.0, cfg=cfg)
    return _t1_apply_gradient(dof, accent)

# --- T2 Backdrop Engine ------------------------------------------------------

class _T2Cfg:
    __slots__ = (
        "tilt_deg", "offset_x", "offset_y",
        "landscape_w", "portrait_w", "gap", "card_radius",
        "col_pattern", "col_stagger", "random_aspect_chance",
        "fade_left", "fade_right",
        "pov_x", "pov_y", "warp_strength",
        "dof_blur_max", "dof_focus_x", "dof_focus_y", "dof_falloff",
        "focus_x", "focus_y", "focus_radius",
    )

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


_T2_TILT_CFG = _T2Cfg(
    tilt_deg=-10,     offset_x=335,         offset_y=100,
    landscape_w=300,  portrait_w=200,       gap=8,            card_radius=8,
    col_pattern=["L", "P", "L", "P", "L", "P", "L", "P", "L"],
    col_stagger=0.35, random_aspect_chance=0.35,
    fade_left=0.30,   fade_right=1.00,
    pov_x=1.0,        pov_y=-1.0,           warp_strength=0.37,
    dof_blur_max=10.0, dof_focus_x=0.75,   dof_focus_y=0.25, dof_falloff=1.5,
    focus_x=0.75,     focus_y=0.25,         focus_radius=0.30,
)

_T2_FLAT_CFG = _T2Cfg(
    tilt_deg=-10,     offset_x=335,         offset_y=100,
    landscape_w=300,  portrait_w=200,       gap=8,            card_radius=8,
    col_pattern=["L", "P", "L", "P", "L", "P", "L", "P", "L"],
    col_stagger=0.35, random_aspect_chance=0.35,
    fade_left=0.30,   fade_right=1.00,
    pov_x=0.0,        pov_y=0.0,            warp_strength=0.0,
    dof_blur_max=10.0, dof_focus_x=0.75,   dof_focus_y=0.25, dof_falloff=1.5,
    focus_x=0.50,     focus_y=0.0,          focus_radius=0.30,
)


def _t2_pick_next(items: "list[dict]", placed_ids: "set[int]", repeat_state: "dict") -> "dict":
    for item in items:
        if item["id"] not in placed_ids:
            placed_ids.add(item["id"])
            return item
    idx    = repeat_state.get("idx", 0)
    chosen = items[idx % len(items)]
    repeat_state["idx"] = idx + 1
    return chosen


def _t2_build_layout(
    items: "list[dict]",
    canvas_w: int,
    canvas_h: int,
    scale: float,
    cfg: "_T2Cfg",
) -> "tuple[Image.Image, int, int]":
    gap  = int(cfg.gap * scale)
    lw   = int(cfg.landscape_w * scale)
    pw   = int(cfg.portrait_w  * scale)

    max_th     = int(pw * 3 / 2)
    stagger_px = int(canvas_h * cfg.col_stagger)
    bleed_x    = (max(lw, pw) + gap) * 3
    bleed_y    = max_th * 2 + stagger_px + gap * 6

    over_w = canvas_w + bleed_x * 2
    over_h = canvas_h + bleed_y * 2
    ox     = bleed_x
    oy     = bleed_y
    canvas_img = Image.new("RGBA", (over_w, over_h), (10, 12, 16, 255))

    pattern_len = len(cfg.col_pattern)
    pattern_idx = 0
    cur_x       = -bleed_x
    columns: list[dict] = []
    while cur_x < canvas_w + bleed_x:
        col_type = cfg.col_pattern[pattern_idx % pattern_len]
        base_w   = lw if col_type == "L" else pw
        if cfg.pov_x != 0.0:
            norm_x    = (cur_x + base_w * 0.5) / canvas_w
            norm_dist = max(0.0, min(1.0, norm_x))
            sf        = max(0.5, 1.0 - abs(cfg.pov_x) * norm_dist * 0.15)
        else:
            sf = 1.0
        col_w = max(50, int(base_w * sf))
        columns.append({"x": cur_x + ox, "w": col_w, "type": col_type, "stagger": pattern_idx % 2 == 1})
        cur_x       += col_w + gap
        pattern_idx += 1

    placed_ids: set[int]  = set()
    rng                   = random.Random(42)
    items_shuf            = list(items)
    rng.shuffle(items_shuf)
    repeat_state: dict = {"idx": 0}

    for col in columns:
        col_x    = col["x"]
        col_w    = col["w"]
        col_type = col["type"]
        start_y  = (-bleed_y + (stagger_px if col["stagger"] else 0)) + oy
        y        = start_y

        while y < over_h:
            use_landscape = (col_type == "L")
            if rng.random() < cfg.random_aspect_chance:
                use_landscape = not use_landscape
            th = max(4, int(col_w * 9 / 16) if use_landscape else int(col_w * 3 / 2))

            screen_x = col_x - ox + col_w * 0.5
            norm_x   = screen_x / canvas_w
            depth    = max(0.0, min(1.0, norm_x))
            opacity  = cfg.fade_left + (cfg.fade_right - cfg.fade_left) * depth

            item = _t2_pick_next(items_shuf, placed_ids, repeat_state)
            tile = _t1_make_tile(item["img"], col_w, th, opacity, cfg, logo=item.get("logo"))
            canvas_img.paste(tile, (int(col_x), int(y)), tile)
            y += th + gap

    return canvas_img, ox, oy


def render_t2_backdrop(
    images:  "list[Image.Image]",
    slug:    str,
    variant: str = "tilt",
    logos:   "list[Image.Image | None] | None" = None,
) -> Image.Image:
    cfg    = _T2_TILT_CFG if variant == "tilt" else _T2_FLAT_CFG
    accent = default_accent_for_label(slug)

    n          = len(images)
    eff_logos  = list(logos) if logos else []
    eff_logos += [None] * max(0, n - len(eff_logos))
    pairs      = list(zip(images, eff_logos[:n]))

    minimum = 4
    if 0 < len(pairs) < minimum:
        for img, logo in itertools.cycle(pairs):
            if len(pairs) >= minimum:
                break
            pairs.append((img.copy(), logo))

    items = [{"id": i, "img": img, "logo": logo} for i, (img, logo) in enumerate(pairs)]

    over, ox, oy = _t2_build_layout(items, CANVAS_W, CANVAS_H, scale=1.0, cfg=cfg)
    warped       = _t1_perspective_warp(over, ox, oy, CANVAS_W, CANVAS_H, cfg)
    dof          = _t1_apply_dof(warped, scale=1.0, cfg=cfg)
    return _t1_apply_gradient(dof, accent)

# --- Cover Card Helpers ------------------------------------------------------

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
    log.warning("Sans-serif font not found — using Pillow built-in.")
    return ImageFont.load_default()


def _text_bbox(text: str, font) -> tuple[int, int]:
    dummy = Image.new("RGB", (1, 1))
    draw  = ImageDraw.Draw(dummy)
    bb    = draw.textbbox((0, 0), text, font=font)
    return bb[2] - bb[0], bb[3] - bb[1]


def _fit_font(text: str, max_w: int, max_h: int, font_path: str | None):
    lo, hi = 18, 300
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


def extract_dominant_color(
    img: Image.Image, sample_height_pct: float = 0.40
) -> tuple[int, int, int]:
    """
    Sample the average color from the bottom 40% of the image.
    Resizes the region to 1x1 via LANCZOS for a perceptually weighted average.
    This color is used for both the grade overlay and the gradient endpoint,
    so each card's fade inherits its own image palette rather than plain black.
    """
    w, h     = img.size
    sample_y = int(h * (1.0 - sample_height_pct))
    region   = img.crop((0, sample_y, w, h)).convert("RGB")
    tiny     = region.resize((1, 1), Image.LANCZOS)
    return tiny.getpixel((0, 0))[:3]


def darken_color(rgb: tuple[int, int, int], factor: float = 0.10) -> tuple[int, int, int]:
    """
    Return a heavily darkened version of rgb.
    factor=0.10 gives 10% brightness — very dark but still tinted,
    suitable as a gradient endpoint so the fade feels native to the image.
    """
    r, g, b = rgb
    return (int(r * factor), int(g * factor), int(b * factor))


def _apply_color_grade(img: Image.Image, dominant_rgb: tuple[int, int, int]) -> Image.Image:
    """
    Subtle color grade using the image's own dominant color.
    The tint radiates from the top-left corner (strongest) and fades
    toward the bottom-right so the subject area stays natural.
    Uses numpy — no per-pixel Python loops.
    """
    w, h    = img.size
    r, g, b = dominant_rgb

    xs = np.linspace(0.0, 1.0, w, dtype=np.float32)
    ys = np.linspace(0.0, 1.0, h, dtype=np.float32)
    xg, yg   = np.meshgrid(xs, ys)
    dist_map = np.sqrt(xg ** 2 + yg ** 2) / math.sqrt(2)
    t_map    = np.clip(1.0 - dist_map / 0.75, 0.0, 1.0) ** 1.6
    alpha_map = (t_map * COLOR_INTENSITY * 255).astype(np.uint8)

    overlay_arr          = np.zeros((h, w, 4), dtype=np.uint8)
    overlay_arr[:, :, 0] = r
    overlay_arr[:, :, 1] = g
    overlay_arr[:, :, 2] = b
    overlay_arr[:, :, 3] = alpha_map

    overlay = Image.fromarray(overlay_arr, "RGBA")
    base    = img.convert("RGBA")
    result  = Image.alpha_composite(base, overlay)
    return result.convert("RGB")


def _render_bottom_gradient(
    img: Image.Image,
    label: str,
    dominant_rgb: tuple[int, int, int],
) -> Image.Image:
    """
    Composite a bottom gradient that fades from transparent at GRADIENT_START
    to a darkened version of the image's dominant color at the very bottom.
    This matches the style seen in Nuvio's title cards where each card's fade
    inherits its own palette rather than fading to generic black.
    Then renders the catalog label centred in the gradient area.
    """
    w, h         = img.size
    grad_start_y = int(h * GRADIENT_START)
    grad_height  = h - grad_start_y
    er, eg, eb   = darken_color(dominant_rgb, factor=0.10)

    if grad_height > 0:
        t_arr     = np.linspace(0.0, 1.0, grad_height, dtype=np.float32)
        alpha_arr = np.clip(255 * GRADIENT_DARKNESS * (t_arr ** 1.3), 0, 255).astype(np.uint8)

        grad_arr             = np.zeros((h, w, 4), dtype=np.uint8)
        grad_arr[grad_start_y:, :, 0] = er
        grad_arr[grad_start_y:, :, 1] = eg
        grad_arr[grad_start_y:, :, 2] = eb
        for i, alpha in enumerate(alpha_arr):
            grad_arr[grad_start_y + i, :, 3] = alpha

        grad_layer = Image.fromarray(grad_arr, "RGBA")
    else:
        grad_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))

    result = Image.alpha_composite(img.convert("RGBA"), grad_layer)

    font_path = _find_font_path()
    panel_h   = max(1, h - grad_start_y)
    max_tw    = int(w * 0.82)
    max_th    = int(panel_h * 0.52)
    font      = _fit_font(label, max_tw, max_th, font_path)
    tw, th    = _text_bbox(label, font)
    tx        = (w - tw) // 2
    ty        = grad_start_y + (panel_h - th) // 2

    text_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(text_layer).text((tx, ty), label, font=font, fill=(255, 255, 255, 255))
    result = Image.alpha_composite(result, text_layer)
    return result.convert("RGB")


def render_cover_card(
    backdrop:    Image.Image,
    label:       str,
    orientation: str  = "landscape",
    focused:     bool = False,
) -> Image.Image:
    """
    Render a single cover card from a backdrop image.

    orientation : 'landscape' -> 1920x1080
                  'portrait'  -> 680x1000
    focused     : True  -> dim the image (focus state)
                  False -> full brightness

    Both states receive a color grade + adaptive bottom gradient + label.
    """
    if orientation == "portrait":
        w, h = PORTRAIT_W, PORTRAIT_H
    else:
        w, h = CANVAS_W, CANVAS_H

    img      = _crop_to_ratio(backdrop.convert("RGB"), w, h).resize((w, h), Image.LANCZOS)
    dominant = extract_dominant_color(img)
    img      = _apply_color_grade(img, dominant)

    if focused:
        black = Image.new("RGB", (w, h), (0, 0, 0))
        img   = Image.blend(img, black, alpha=FOCUSED_DIM)

    return _render_bottom_gradient(img, label, dominant)

# --- I/O Helpers -------------------------------------------------------------

def save_dual(img: Image.Image, base_path: Path) -> None:
    rgb = img.convert("RGB")
    rgb.save(base_path.with_suffix(".jpg"),  "JPEG", quality=92, optimize=True)
    rgb.save(base_path.with_suffix(".webp"), "WEBP", quality=85, method=6)


def assets_exist(folder: str, slug: str, mode: str = "all") -> bool:
    base: Path         = COLLECTIONS_DIR / folder
    checks: list[Path] = []

    if mode in ("all", "backdrop"):
        for ext in (".jpg", ".webp"):
            checks.append(base / "backdrop" / f"{slug}{ext}")
            checks.append(base / "backdrop" / f"{slug}_t1_tilt{ext}")
            checks.append(base / "backdrop" / f"{slug}_t1_flat{ext}")
            checks.append(base / "backdrop" / f"{slug}_t2_tilt{ext}")
            checks.append(base / "backdrop" / f"{slug}_t2_flat{ext}")

    if mode in ("all", "covers"):
        for t in ("focused", "cover"):
            for suffix in ("_landscape", "_portrait"):
                for ext in (".jpg", ".webp"):
                    checks.append(base / t / f"{slug}{suffix}{ext}")

    return all(p.exists() for p in checks)

# --- Per-catalog Orchestration -----------------------------------------------

def process_catalog(catalog: dict, folder: str, slug: str, force: bool, mode: str = "all") -> None:
    name = catalog.get("name") or catalog.get("title") or slug
    base = COLLECTIONS_DIR / folder

    do_backdrop = mode in ("all", "backdrop")
    do_covers   = mode in ("all", "covers")

    log.info("")
    log.info("=" * 62)
    log.info("Catalog  : %s  [%s/%s]  mode=%s", name, folder, slug, mode)
    log.info("=" * 62)

    for asset_type in ("backdrop", "cover", "focused", "title"):
        (base / asset_type).mkdir(parents=True, exist_ok=True)

    if not force and assets_exist(folder, slug, mode):
        log.info("  Assets already exist for mode=%s — skipping (use --force to regenerate).", mode)
        return

    top_backdrop: "Image.Image | None" = None

    if do_backdrop:
        log.info("  Fetching backdrop artwork ...")
        backdrops, logos, top_backdrop = fetch_all_backdrops(catalog)
        if not backdrops:
            log.warning("  No backdrop images fetched — skipping render.")
            return
        log.info("  Fetched %d backdrop image(s).", len(backdrops))

        log.info("  Rendering Prism backdrop ...")
        prism = render_prism_backdrop(backdrops, slug, logos=logos)
        save_dual(prism, base / "backdrop" / slug)
        log.info("  + backdrop/%s.jpg + .webp", slug)

        log.info("  Rendering T1 tilt backdrop ...")
        t1_tilt = render_t1_backdrop(backdrops, slug, "tilt", logos=logos)
        save_dual(t1_tilt, base / "backdrop" / f"{slug}_t1_tilt")
        log.info("  + backdrop/%s_t1_tilt.jpg + .webp", slug)

        log.info("  Rendering T1 flat backdrop ...")
        t1_flat = render_t1_backdrop(backdrops, slug, "flat", logos=logos)
        save_dual(t1_flat, base / "backdrop" / f"{slug}_t1_flat")
        log.info("  + backdrop/%s_t1_flat.jpg + .webp", slug)

        log.info("  Rendering T2 tilt backdrop ...")
        t2_tilt = render_t2_backdrop(backdrops, slug, "tilt", logos=logos)
        save_dual(t2_tilt, base / "backdrop" / f"{slug}_t2_tilt")
        log.info("  + backdrop/%s_t2_tilt.jpg + .webp", slug)

        log.info("  Rendering T2 flat backdrop ...")
        t2_flat = render_t2_backdrop(backdrops, slug, "flat", logos=logos)
        save_dual(t2_flat, base / "backdrop" / f"{slug}_t2_flat")
        log.info("  + backdrop/%s_t2_flat.jpg + .webp", slug)

    if do_covers:
        if top_backdrop is None:
            log.info("  Fetching backdrop for cover cards ...")
            _, _logos, top_backdrop = fetch_all_backdrops(catalog, limit=1)

        if top_backdrop is None:
            log.warning("  No image available for cover cards — skipping covers.")
            return

        raw_title = (catalog.get("title") or slug).strip()
        label     = strip_emoji(raw_title)
        log.info("  Cover label: %s", label)

        log.info("  Rendering landscape cover ...")
        cover_land = render_cover_card(top_backdrop, label, orientation="landscape", focused=False)
        save_dual(cover_land, base / "cover" / f"{slug}_landscape")
        log.info("    + cover/%s_landscape.jpg + .webp", slug)

        log.info("  Rendering landscape focused ...")
        foc_land = render_cover_card(top_backdrop, label, orientation="landscape", focused=True)
        save_dual(foc_land, base / "focused" / f"{slug}_landscape")
        log.info("    + focused/%s_landscape.jpg + .webp", slug)

        log.info("  Rendering portrait cover ...")
        cover_port = render_cover_card(top_backdrop, label, orientation="portrait", focused=False)
        save_dual(cover_port, base / "cover" / f"{slug}_portrait")
        log.info("    + cover/%s_portrait.jpg + .webp", slug)

        log.info("  Rendering portrait focused ...")
        foc_port = render_cover_card(top_backdrop, label, orientation="portrait", focused=True)
        save_dual(foc_port, base / "focused" / f"{slug}_portrait")
        log.info("    + focused/%s_portrait.jpg + .webp", slug)

    log.info("  title/ initialized (manual assets preserved).")

# --- CLI & Entry Point -------------------------------------------------------

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
    parser.add_argument("--target",  default="all",   metavar="TARGET", help="Folder name, catalog slug, or 'all' (default: all)")
    parser.add_argument("--json",    default=str(SOURCE_JSON), metavar="PATH", help="Path to nuvio-collections.json")
    parser.add_argument("--force",   action="store_true", help="Regenerate assets even if output files already exist")
    parser.add_argument("--mode",    default="all",   choices=["all", "backdrop", "covers"], help="all | backdrop | covers (default: all)")
    args = parser.parse_args()

    log.info("Nuvio TV - Catalog Asset Generator  mode=%s", args.mode)

    validate_env()

    manifest_catalog_ids: set[str] = set()
    if AIOMETADATA_URL:
        manifest_catalog_ids = fetch_manifest_catalog_ids(AIOMETADATA_URL)

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
        log.warning("No matching collections.* catalogs found for --target '%s'.", target)
        sys.exit(0)

    log.info("Processing %d catalog(s) for --target='%s' --mode='%s' (force=%s).", len(matched), target, args.mode, args.force)

    errors = 0
    if manifest_catalog_ids:
        for catalog, folder, slug in matched:
            for src in get_catalog_sources(catalog):
                cid = src.get("id") or src.get("catalogId", "")
                if cid and cid not in manifest_catalog_ids:
                    log.warning("Catalog ID '%s' (in %s/%s) was NOT found in your AIOMetadata manifest.", cid, folder, slug)

    for catalog, folder, slug in matched:
        try:
            process_catalog(catalog, folder, slug, force=args.force, mode=args.mode)
        except Exception as exc:
            log.error("Fatal error in '%s/%s': %s", folder, slug, exc, exc_info=True)
            errors += 1

    log.info("")
    if errors:
        log.info("Done with %d error(s). Check logs above.", errors)
        sys.exit(1)
    else:
        log.info("All done — no errors.")


if __name__ == "__main__":
    main()
