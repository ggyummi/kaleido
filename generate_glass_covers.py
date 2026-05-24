#!/usr/bin/env python3
"""
generate_glass_covers.py — Nuvio TV Glassmorphism Cover Generator
=================================================================
Generates landscape-only cover and focused cards with a glassmorphism
bottom panel.

The bottom 35% of each image receives:
  • A heavy Gaussian blur (radius 45) blended via a power-curve gradient:
    0% at 65% image height, ~62% at the midpoint, 100% at the bottom edge.
    This produces the thick, saturated frosted-glass look seen in cinematic
    UI references — the blur builds quickly and is fully saturated at bottom.
  • The catalog title rendered in Inter Medium (or fallback), lower-left,
    on top of the glass zone.

Output:
  main/test/{folder}/focused/{catalog}_landscape.jpg(.webp)   — dimmed + glass
  main/test/{folder}/cover/{catalog}_landscape.jpg(.webp)     — full-bright + glass

Shares ALL data-fetching and utility code with generate_catalog_assets.py via
a read-only module import — no existing files are modified.

Environment variables (same as generate_catalog_assets.py):
  AIOMETADATA_URL   Base URL of AIOMetadata/Stremio addon (preferred)
  TMDB_API_KEY      TMDb API key (optional fallback)
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# ── Read-only import of shared utilities ────────────────────────────────────────────
# generate_catalog_assets.py is imported as a module; nothing in it is changed.
import generate_catalog_assets as _gca

log = logging.getLogger("nuvio.glass")

# ── Canvas / style constants (re-exported from parent) ───────────────────────────
CANVAS_W        = _gca.CANVAS_W         # 1920
CANVAS_H        = _gca.CANVAS_H         # 1080
FOCUSED_DIM     = _gca.FOCUSED_DIM      # 0.50
COVER_FONT_SIZE = _gca.COVER_FONT_SIZE  # 80

# ── Output root — completely separate from collections/ ─────────────────────────
OUTPUT_DIR = Path("main/test")

# ── Glass zone parameters ─────────────────────────────────────────────────────────
GLASS_FRACTION = 0.35   # bottom 35% is the glass panel
BLUR_RADIUS    = 45     # Gaussian blur radius (px); heavy = saturated frosted look

# ── Font candidates — Inter Medium preferred, regular-weight fallbacks ────────────
_GLASS_FONT_CANDIDATES = [
    # Inter Medium (downloaded by workflow)
    "/usr/local/share/fonts/Inter-Medium.ttf",
    "/usr/share/fonts/truetype/inter/Inter-Medium.ttf",
    # Open Sans Regular (medium-weight feel, installed by workflow)
    "/usr/share/fonts/truetype/open-sans/OpenSans-Regular.ttf",
    "/usr/share/fonts/open-sans/OpenSans-Regular.ttf",
    # Liberation Sans Regular
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
    # DejaVu Sans
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _find_glass_font_path() -> str | None:
    for p in _GLASS_FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


# ─── Glassmorphism Renderer ────────────────────────────────────────────────────────

def render_glass_landscape(
    backdrop: Image.Image,
    label: str,
    focused: bool,
) -> Image.Image:
    """
    Render a 1920x1080 landscape card with a glassmorphism bottom zone.

    Steps:
      1. Crop / resize backdrop to 1920x1080.
      2. Optionally dim to 50% brightness (focused variant).
      3. Apply a strongly blurred copy of the image to the bottom 35% via a
         power-curve gradient mask (0 at top of zone, fully blurred at bottom).
      4. Render the catalog title in Inter Medium (or fallback) over the glass
         zone, lower-left, in white.

    Returns an RGB Image.
    """
    w, h = CANVAS_W, CANVAS_H
    glass_start = int(h * (1.0 - GLASS_FRACTION))  # row where glass begins
    zone_h      = h - glass_start                   # height of glass zone

    # 1. Prepare background
    bg = _gca._crop_to_ratio(backdrop.convert("RGBA"), w, h).resize(
        (w, h), Image.LANCZOS
    )
    if focused:
        black = Image.new("RGBA", (w, h), (0, 0, 0, 255))
        bg    = Image.blend(bg, black, 1.0 - FOCUSED_DIM)

    # 2. Blurred copy of the full image (consistent blur strength everywhere;
    #    the gradient mask controls *how much* blur shows in the glass zone).
    blurred = bg.copy().filter(ImageFilter.GaussianBlur(radius=BLUR_RADIUS))

    # 3. Power-curve gradient mask: 0 at glass_start -> 255 at bottom edge.
    #    t**0.7 makes the blur build quickly — already ~62% by the midpoint —
    #    producing the heavy, saturated look at the bottom seen in the reference.
    mask_arr = np.zeros((h, w), dtype=np.uint8)
    if zone_h > 0:
        t = np.linspace(0.0, 1.0, zone_h, dtype=np.float32)
        alpha = np.power(t, 0.7)
        mask_arr[glass_start:] = np.clip(alpha * 255, 0, 255).astype(np.uint8)[:, np.newaxis]
    blur_mask = Image.fromarray(mask_arr, "L")

    # 4. Paste blurred over sharp using the power-curve gradient mask
    result = bg.copy()
    result.paste(blurred, (0, 0), blur_mask)

    # 5. Catalog title — Inter Medium weight, white, lower-left, word-wrapped
    font_path  = _find_glass_font_path()
    font       = _gca._load_font(COVER_FONT_SIZE, font_path)
    max_tw     = int(w * 0.55)
    lines      = _gca._wrap_text(label, font, max_tw)
    _, lh      = _gca._text_bbox("Ag", font)
    line_step  = int(lh * 1.15)
    total_h    = line_step * len(lines)
    bottom_pad = int(h * 0.08)
    tx = int(w * 0.08)
    ty = h - bottom_pad - total_h
    draw = ImageDraw.Draw(result)

    # Soft drop shadow so the title reads on both light and dark glass zones
    for i, line in enumerate(lines):
        draw.text(
            (tx + 2, ty + i * line_step + 2),
            line, font=font, fill=(0, 0, 0, 160),
        )
    # White title
    for i, line in enumerate(lines):
        draw.text(
            (tx, ty + i * line_step),
            line, font=font, fill=(255, 255, 255, 255),
        )

    return result.convert("RGB")


# ─── Output Helpers ───────────────────────────────────────────────────────────

def assets_exist_glass(folder: str, slug: str) -> bool:
    """Return True when both landscape variants already exist on disk."""
    base = OUTPUT_DIR / folder
    for variant in ("focused", "cover"):
        for ext in (".jpg", ".webp"):
            if not (base / variant / f"{slug}_landscape{ext}").exists():
                return False
    return True


# ─── Per-catalog Orchestration ────────────────────────────────────────────────

def process_catalog_glass(
    catalog: dict,
    folder: str,
    slug: str,
    force: bool,
    used_cover_hashes: "set[str] | None" = None,
) -> None:
    name = catalog.get("name") or catalog.get("title") or slug
    base = OUTPUT_DIR / folder

    log.info("")
    log.info("━" * 62)
    log.info("Catalog  : %s  [%s/%s]", name, folder, slug)
    log.info("━" * 62)

    for variant in ("focused", "cover"):
        (base / variant).mkdir(parents=True, exist_ok=True)

    if not force and assets_exist_glass(folder, slug):
        log.info("  Glass assets already exist — skipping (use --force to regenerate).")
        return

    log.info("  Fetching backdrop pool …")
    cover_pool, _, _ = _gca.fetch_all_backdrops(catalog, limit=15)

    if not cover_pool:
        log.warning("  No images fetched — skipping catalog.")
        return

    _hashes  = used_cover_hashes if used_cover_hashes is not None else set()
    backdrop = cover_pool[0]
    for candidate in cover_pool:
        h = _gca._quick_image_hash(candidate)
        if h not in _hashes:
            backdrop = candidate
            _hashes.add(h)
            log.info("  Selected: first unused backdrop from pool.")
            break
    else:
        log.info("  Pool exhausted — reusing top backdrop.")
        _hashes.add(_gca._quick_image_hash(cover_pool[0]))

    label = _gca.strip_emoji(
        catalog.get("name") or catalog.get("title") or slug
    ).strip() or slug

    log.info("  Label: %s", label)

    for focused_flag in (True, False):
        variant = "focused" if focused_flag else "cover"
        log.info("  Rendering %s landscape (glass) …", variant)
        card     = render_glass_landscape(backdrop, label, focused=focused_flag)
        out_path = base / variant / f"{slug}_landscape"
        _gca.save_dual(card, out_path)
        log.info("    ✓ %s/%s_landscape.jpg + .webp", variant, slug)


# ─── CLI & Entry Point ────────────────────────────────────────────────────────

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    parser = argparse.ArgumentParser(
        description=(
            "Generate glassmorphism landscape cover+focused cards. "
            "Output: main/test/{folder}/{variant}/{slug}_landscape.jpg(.webp)"
        ),
    )
    parser.add_argument(
        "--target", default="all", metavar="TARGET",
        help="Folder name, catalog slug, or 'all' (default: all)",
    )
    parser.add_argument(
        "--json", default="nuvio-collections.json", metavar="PATH",
        help="Path to nuvio-collections.json",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Regenerate assets even if output files already exist",
    )
    args = parser.parse_args()

    log.info("╔" + "═" * 57 + "╗")
    log.info("║      Nuvio TV · Glassmorphism Cover Generator            ║")
    log.info("║      output : main/test/                                 ║")
    log.info("║      effect : Gaussian blur r=45, power curve, bottom 35%% ║")
    log.info("╚" + "═" * 57 + "╝")

    _gca.validate_env()

    manifest_catalog_ids: set[str] = set()
    if _gca.AIOMETADATA_URL:
        manifest_catalog_ids = _gca.fetch_manifest_catalog_ids(_gca.AIOMETADATA_URL)

    json_path = Path(args.json)
    if not json_path.exists():
        log.error("Config file not found: %s", json_path)
        sys.exit(1)

    catalogs = _gca.load_catalogs(json_path)
    log.info("Loaded %d catalog(s) from %s.", len(catalogs), json_path)

    target  = args.target.strip().lower()
    matched: list[tuple[dict, str, str]] = []

    for catalog in catalogs:
        if not catalog.get("enabled", True):
            continue
        cat_id = catalog.get("id", "")
        parsed = _gca.parse_collection_id(cat_id)
        if parsed is None:
            continue
        folder, slug = parsed
        if target == "all" or target == folder or target == slug:
            matched.append((catalog, folder, slug))

    if not matched:
        log.warning("No matching collections.* catalogs for --target '%s'.", target)
        sys.exit(0)

    log.info(
        "Processing %d catalog(s) for --target='%s' (force=%s).",
        len(matched), target, args.force,
    )

    if manifest_catalog_ids:
        for catalog, folder, slug in matched:
            for src in _gca.get_catalog_sources(catalog):
                cid = src.get("id") or src.get("catalogId", "")
                if cid and cid not in manifest_catalog_ids:
                    log.warning(
                        "Catalog ID '%s' (%s/%s) not in AIOMetadata manifest — "
                        "this source will return no images.",
                        cid, folder, slug,
                    )

    used_cover_hashes: set[str] = set()
    errors = 0

    for catalog, folder, slug in matched:
        try:
            process_catalog_glass(
                catalog, folder, slug,
                force=args.force,
                used_cover_hashes=used_cover_hashes,
            )
        except Exception as exc:
            log.error("Error in '%s/%s': %s", folder, slug, exc, exc_info=True)
            errors += 1

    log.info("")
    if errors:
        log.info("╔" + "═" * 57 + "╗")
        log.info("║  Done with %d error(s). Check logs above.               ║", errors)
        log.info("╚" + "═" * 57 + "╝")
        sys.exit(1)
    else:
        log.info("╔" + "═" * 57 + "╗")
        log.info("║              All done — no errors.                       ║")
        log.info("╚" + "═" * 57 + "╝")


if __name__ == "__main__":
    main()
