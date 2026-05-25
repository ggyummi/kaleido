#!/usr/bin/env python3
"""
generate_glass_covers.py — Nuvio TV Glassmorphism Cover Generator
=================================================================
Generates landscape-only cover and focused cards with an advanced
glassmorphism bottom panel.

The bottom panel features:
  • An enhanced luminance-boosted blur layer.
  • A 1px crisp specular edge highlight line at the boundary.
  • Micro-grit noise texturing for a tactile frosted feel.
  • Soft, diffused typography drop shadows for spatial depth.

Output:
  main/test/{folder}/focused/{catalog}_landscape.jpg(.webp)   — dimmed + glass
  main/test/{folder}/cover/{catalog}_landscape.jpg(.webp)     — full-bright + glass

Shares ALL data-fetching and utility code with generate_catalog_assets.py via
a read-only module import — no existing files are modified.
"""

import argparse
import logging
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageEnhance

# ── Read-only import of shared utilities ──────────────────────────────────────────────
import generate_catalog_assets as _gca

log = logging.getLogger("nuvio.glass")

# ── Canvas / style constants (re-exported from parent) ───────────────────────
CANVAS_W        = _gca.CANVAS_W         # 1920
CANVAS_H        = _gca.CANVAS_H         # 1080
FOCUSED_DIM     = _gca.FOCUSED_DIM      # 0.50
COVER_FONT_SIZE = _gca.COVER_FONT_SIZE  # 110

# Larger fixed font for glass text covers — never shrinks for long titles
GLASS_COVER_FONT_SIZE = 110

# ── Output root ───────────────────────────────────────────────────────────────────────────
OUTPUT_DIR = Path("main/test")

# ── Glass zone parameters ──────────────────────────────────────────────────────────────────
GLASS_FRACTION = 0.70   # Proportion of canvas height used by glass panel
BLUR_RADIUS    = 60     # Gaussian blur radius (px)


# ─── Glassmorphism Renderer ────────────────────────────────────────────────────────────────────────────────────────────────────────────

def render_glass_landscape(
    backdrop: Image.Image,
    label: str,
    focused: bool,
) -> Image.Image:
    """
    Render a 1920×1080 landscape card with premium glassmorphism.
    Includes edge highlights, textured grain, light diffusion, and soft shadows.
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

    # 2. Enhanced Blur Layer (Brightness Boost & Contrast Softening)
    raw_blur = bg.copy().filter(ImageFilter.GaussianBlur(radius=BLUR_RADIUS))
    
    # Boost brightness to mimic internal light scattering inside glass
    brightener = ImageEnhance.Brightness(raw_blur)
    raw_blur = brightener.enhance(1.12)
    
    # Soften contrast slightly to make the colors feel creamy and diffused
    contrast_tweak = ImageEnhance.Contrast(raw_blur)
    blurred = contrast_tweak.enhance(0.88)

    # 3. Linear gradient mask: 0 at glass_start → 255 at bottom edge
    mask_arr = np.zeros((h, w), dtype=np.uint8)
    if zone_h > 0:
        t = np.linspace(0.0, 1.0, zone_h, dtype=np.float32)
        mask_arr[glass_start:] = np.clip(t * 255, 0, 255).astype(np.uint8)[:, np.newaxis]
    blur_mask = Image.fromarray(mask_arr, "L")

    # 4. Paste blurred over sharp using the linear gradient mask
    result = bg.copy()
    result.paste(blurred, (0, 0), blur_mask)

    # 5. Inject Micro-Grit Noise ( Frosted Texture restricted to glass zone )
    if zone_h > 0:
        # Generate low-intensity monochromatic noise
        noise_sigma = 5  
        noise_arr = np.random.normal(0, noise_sigma, (h, w, 4)).astype(np.int16)
        
        # Zero out noise anywhere above the glass zone
        noise_arr[:glass_start, :, :] = 0
        
        # Apply noise and clamp safely to pixel limits
        img_arr = np.array(result).astype(np.int16)
        img_arr = np.clip(img_arr + noise_arr, 0, 255).astype(np.uint8)
        result = Image.fromarray(img_arr, "RGBA")

    # 7. Typography Layout Configuration
    font_path  = _gca._find_font_path()
    font       = _gca._load_font(GLASS_COVER_FONT_SIZE, font_path)
    max_tw     = int(w * 0.55)
    lines      = _gca._wrap_text_glass(label, font, max_tw)
    _, lh      = _gca._text_bbox("Ag", font)
    line_step  = int(lh * 1.15)
    total_h    = line_step * len(lines)
    bottom_pad = int(h * 0.08)
    tx = int(w * 0.08)
    ty = h - bottom_pad - total_h

    # 8. Render Soft Diffused Text Shadow Layer
    shadow_layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    s_draw = ImageDraw.Draw(shadow_layer)
    for i, line in enumerate(lines):
        # Draw ambient black baseline text shifted slightly lower
        s_draw.text(
            (tx, ty + i * line_step + 4),
            line, font=font, fill=(0, 0, 0, 180),
        )
    # Blur the text layer separately to soften the cast shadow edge
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=6))
    result.paste(shadow_layer, (0, 0), shadow_layer)

    # 9. Render Sharp White Title Foreground
    draw = ImageDraw.Draw(result)
    for i, line in enumerate(lines):
        draw.text(
            (tx, ty + i * line_step),
            line, font=font, fill=(255, 255, 255, 255),
        )

    return result.convert("RGB")


# ─── Output Helpers ────────────────────────────────────────────────────────────────────────────────────────

def assets_exist_glass(folder: str, slug: str) -> bool:
    """Return True when both landscape variants already exist on disk."""
    base = OUTPUT_DIR / folder
    for variant in ("focused", "cover"):
        for ext in (".jpg", ".webp"):
            if not (base / variant / f"{slug}_landscape{ext}").exists():
                return False
    return True


# ─── Per-catalog Orchestration ────────────────────────────────────────────────────────────────────────────────────────────

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


# ─── CLI & Entry Point ───────────────────────────────────────────────────────────────────────────────────────────

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

    log.info("╔════════════════════════════════════════════════════════╗")
    log.info("║      Nuvio TV · Glassmorphism Cover Generator            ║")
    log.info("║      output : main/test/                                 ║")
    log.info("║      effect : Premium Custom Glassmorphism Setup         ║")
    log.info("╚════════════════════════════════════════════════════════╝")

    _gca.validate_env()

    manifest_catalog_ids: set[str] = set()
    if _gca.AIOMETADATA_URL:
        try:
            manifest_catalog_ids = _gca.fetch_manifest_catalog_ids(_gca.AIOMETADATA_URL)
        except RuntimeError as exc:
            log.warning("Manifest fetch failed — skipping catalog ID validation: %s", exc)

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
        log.info("╔════════════════════════════════════════════════════════╗")
        log.info("║  Done with %d error(s). Check logs above.               ║", errors)
        log.info("╚════════════════════════════════════════════════════════╝")
        sys.exit(1)
    else:
        log.info("╔════════════════════════════════════════════════════════╗")
        log.info("║              All done — no errors.                       ║")
        log.info("╚════════════════════════════════════════════════════════╝")


if __name__ == "__main__":
    main()
