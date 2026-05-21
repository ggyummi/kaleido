# tiny-deluxe

Automated pipeline that generates cinematic catalog assets for [Nuvio TV](https://github.com/luckynumb3rs/stremio-perfect-setup), served as a free static CDN via [jsDelivr](https://www.jsdelivr.com/). Assets are regenerated on demand via GitHub Actions — zero manual maintenance once set up.

---

## What this repo produces

For each catalog defined in `nuvio-collections.json`, the pipeline generates five asset types:

```
collections/
└── {folder}/
    ├── backdrop/
    │   ├── {catalog}.jpg           ← Prism 3D tilted-grid collage (1920×1080)
    │   ├── {catalog}.webp
    │   ├── {catalog}_t1_tilt.jpg   ← T1 perspective-warp + −10° rotation
    │   ├── {catalog}_t1_tilt.webp
    │   ├── {catalog}_t1_flat.jpg   ← T1 tilt-only, no perspective warp
    │   ├── {catalog}_t1_flat.webp
    │   ├── {catalog}_t2_tilt.jpg   ← T2 mixed P+L columns, perspective-warp + −10° rotation
    │   ├── {catalog}_t2_tilt.webp
    │   ├── {catalog}_t2_flat.jpg   ← T2 mixed P+L columns, tilt-only, no perspective warp
    │   └── {catalog}_t2_flat.webp
    ├── focused/
    │   ├── {catalog}_landscape.jpg   ← frosted glass panel, dimmed — selected/hover (1920×1080)
    │   ├── {catalog}_landscape.webp
    │   ├── {catalog}_portrait.jpg    ← same, portrait orientation (680×1000)
    │   └── {catalog}_portrait.webp
    ├── cover/
    │   ├── {catalog}_landscape.jpg   ← frosted glass panel, full brightness — idle (1920×1080)
    │   ├── {catalog}_landscape.webp
    │   ├── {catalog}_portrait.jpg    ← same, portrait orientation (680×1000)
    │   └── {catalog}_portrait.webp
    └── title/                     ← manual-only; automation never writes here
```

The visual difference between `focused/` and `cover/` creates the pop effect when a user scrolls through a row in Nuvio TV.

---

## Asset rendering

### Backdrop — five renders per catalog

All five backdrop variants reuse the same pool of images fetched from `catalogSources` — no additional HTTP requests are made.

#### Prism tilted-grid (`{catalog}.jpg`)

Adapted from [luckynumb3rs/stremio-perfect-setup](https://github.com/luckynumb3rs/stremio-perfect-setup):

- A 10° clockwise-tilted staggered grid of rounded-corner image tiles
- Up to 40 backdrop images fetched from all `catalogSources` (movies + series mixed into one pool, deduplicated)
- Best images placed nearest the focal point (centre-screen, slightly below midline)
- Four-pass gradient overlay: dark left edge, dark bottom vignette, dark bottom-left corner, accent-coloured top-right glow
- Accent colour is deterministic per catalog name (HSV, seed derived from label characters)

#### T1 perspective-warp (`{catalog}_t1_tilt.jpg`)

Ported from [bramst0ne/prism-wallpapers](https://github.com/bramst0ne/prism-wallpapers) `backdrop_T1.py`:

- Row-staggered landscape tile grid (400 px wide tiles at 1080p, 8 px gap)
- Full 3D perspective warp: `POV_X=1.0, POV_Y=-1.0, WARP_STRENGTH=0.37`
- −10° canvas rotation anchored to the pan-shifted focal centre
- Depth-of-field blur keyed to `(0.75, 0.25)` focal point
- Left-fade opacity gradient + dark bottom vignette + accent glow

#### T1 flat-tilt (`{catalog}_t1_flat.jpg`)

Ported from `backdrop_T1_flat.py` — identical to the tilt variant but with perspective warp disabled (`POV_X=0, POV_Y=0, WARP_STRENGTH=0`) and focal centre shifted to `(0.75, 0.50)`. Produces a cleaner, flatter look suitable for UI contexts where strong depth is distracting.

#### T2 perspective-warp (`{catalog}_t2_tilt.jpg`)

Ported from `backdrop_T2.py`:

- Mixed portrait (2:3) and landscape (16:9) column grid following `COL_PATTERN = [L, P, L, P, L, P, L, P, L]`
- `RANDOM_ASPECT_CHANCE=0.35` randomly flips individual tile aspect ratios for visual variety
- `COL_STAGGER=0.35` vertically offsets alternating columns by 35 % of canvas height
- Column widths scale slightly with perspective distance (`POV_X` factor)
- Full 3D perspective warp: `POV_X=1.0, POV_Y=-1.0, WARP_STRENGTH=0.37`
- −10° canvas rotation, depth-of-field blur, and accent glow (shared with T1)

#### T2 flat-tilt (`{catalog}_t2_flat.jpg`)

Ported from `backdrop_T2_flat.py` — identical to the T2 tilt variant but with perspective warp disabled (`POV_X=0, POV_Y=0, WARP_STRENGTH=0`) and focal centre shifted to `(0.50, 0.0)`.

### Focused / Cover — Apple TV+ style cover cards

Each catalog generates four cover variants: landscape (1920×1080) and portrait (680×1000), each in both focused and cover states.

- Top backdrop image from the catalog, cropped and scaled to the output dimensions
- **Frosted glass panel** occupying the bottom 28% of the image:
  - Gaussian-blurred backdrop region beneath the panel
  - Dark semi-transparent overlay (alpha 150) over the blur
  - Full catalog title (emoji stripped, original casing) centred horizontally and vertically within the panel
- `focused`: backdrop dimmed to 50% brightness before the panel is applied — the darkened state shown when a card is selected or hovered
- `cover`: full-brightness backdrop with the frosted glass panel — the idle/unfocused state

---

## How the CDN works

Every file committed to this repo is instantly accessible at a permanent jsDelivr URL:

```
https://cdn.jsdelivr.net/gh/ggyummi/tiny-deluxe@main/{path/to/file}
```

The URL never changes. When the pipeline regenerates a file and pushes it, jsDelivr serves the updated image after the cache is purged (step 8 of the workflow). Paste the URL into Nuvio once — it stays fresh automatically.

### Example URLs

```
# backdrop collage
https://cdn.jsdelivr.net/gh/ggyummi/tiny-deluxe@main/collections/streaming/backdrop/netflix.webp

# focused banner — landscape (selected state, 1920×1080)
https://cdn.jsdelivr.net/gh/ggyummi/tiny-deluxe@main/collections/streaming/focused/netflix_landscape.webp

# focused banner — portrait (selected state, 680×1000)
https://cdn.jsdelivr.net/gh/ggyummi/tiny-deluxe@main/collections/streaming/focused/netflix_portrait.webp

# cover banner — landscape (idle state, 1920×1080)
https://cdn.jsdelivr.net/gh/ggyummi/tiny-deluxe@main/collections/streaming/cover/netflix_landscape.webp

# cover banner — portrait (idle state, 680×1000)
https://cdn.jsdelivr.net/gh/ggyummi/tiny-deluxe@main/collections/streaming/cover/netflix_portrait.webp
```

The `{folder}` and `{catalog}` path segments come directly from the catalog's `id` field in `nuvio-collections.json` — `collections.{folder}.{catalog}`.

---

## Setup

### 1. Fork or clone this repo

Rename it to whatever you like.

### 2. Configure nuvio-collections.json

The file already exists in the repo root. Each entry follows this schema:

```json
[
  {
    "id": "collections.{folder}.{catalog}",
    "name": "Display Name",
    "enabled": true,
    "catalogSources": [
      { "type": "movie",  "id": "your.stremio.addon.catalog.id" },
      { "type": "series", "id": "your.stremio.addon.catalog.id" }
    ]
  }
]
```

- `id` must be exactly three dot-separated segments starting with `collections.`
- `{folder}` groups catalogs into subdirectories (e.g. `streaming`, `genres`, `discover`)
- `{catalog}` becomes the filename (e.g. `netflix`, `action`, `trending`)
- `catalogSources` lists one or more Stremio addon catalog endpoints to pull artwork from; movies and series are mixed into one backdrop pool

The included `nuvio-collections.json` has entries across `discover/`, `streaming/`, `genres/`, `themes/`, `studios/`, `decades/`, `runtime/`, and `simkl/`.

### 3. Add GitHub Secrets

Go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret | Where to get it | Required? |
|---|---|---|
| `AIOMETADATA_URL` | Base URL of your AIOMetadata Stremio addon instance | Recommended |
| `TMDB_API_KEY` | [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api) | Optional fallback |

`AIOMETADATA_URL` is the preferred source. The script calls `{AIOMETADATA_URL}/catalog/{type}/{id}.json` — a plain unauthenticated GET; no OAuth tokens are needed in the request. When absent, the script falls back to the public Cinemeta addon (generic popular content only — provider-specific catalogs like Netflix or Disney+ will not be accurate without it).

`TMDB_API_KEY` is used as a secondary fallback for entries that lack a `catalogSources` field.

### 4. Run the workflow

Go to **Actions → 🎨 Nuvio · Catalog Asset Generator → Run workflow**.

Set the **Target** field and click the green button:

| Target value | Effect |
|---|---|
| `all` | Every enabled catalog in `nuvio-collections.json` |
| `streaming` | Every catalog inside the `streaming` folder |
| `netflix` | Just the `netflix` catalog (any folder) |

Leave **Force Regenerate** unchecked for faster incremental runs — already-generated assets are skipped automatically.

---

## Workflow steps

Two separate workflows are provided, each triggerable from **Actions → Run workflow**:

### `generate-backdrops.yml` — 🖼 Nuvio · Backdrop Generator

Runs automatically every Monday and on pushes to `main` that touch key files.

1. **Checkout** — full history clone
2. **Python 3.11** — set up with pip cache
3. **Dependencies** — `pip install -r requirements.txt` (requests, Pillow, numpy)
4. **Validate** — confirms `nuvio-collections.json` exists and has valid `collections.*` entries
5. **Generate** — runs `generate_catalog_assets.py --target <input> --mode backdrop [--force]`
6. **Commit & push** — stages only `backdrop/` assets; commits with a summary line
7. **Purge CDN** — runs `collections/scripts/purge.py` to flush jsDelivr cache immediately

### `generate-covers.yml` — 🎨 Nuvio · Cover Generator

Manual dispatch only.

1. **Checkout** — full history clone
2. **Python 3.11** — set up with pip cache
3. **Font install** — NimbusSans Bold (Helvetica Neue equivalent) + Liberation Sans fallback
4. **Dependencies** — `pip install -r requirements.txt` (requests, Pillow, numpy)
5. **Validate** — confirms `nuvio-collections.json` exists and has valid `collections.*` entries
6. **Generate** — runs `generate_catalog_assets.py --target <input> --mode covers [--force]`
7. **Commit & push** — stages `focused/` and `cover/` assets; commits with a summary line
8. **Purge CDN** — runs `collections/scripts/purge.py` to flush jsDelivr cache immediately

---

## Running locally

```bash
git clone https://github.com/ggyummi/tiny-deluxe.git
cd tiny-deluxe
pip install -r requirements.txt

export AIOMETADATA_URL=https://your-aiometadata-instance.example.com
export TMDB_API_KEY=your_key_here   # optional

# Process everything
python generate_catalog_assets.py --target all

# Process one folder
python generate_catalog_assets.py --target streaming

# Process one catalog
python generate_catalog_assets.py --target netflix

# Force-regenerate even if files exist
python generate_catalog_assets.py --target all --force
```

Dependencies: `requests`, `Pillow`, `numpy` — see `requirements.txt`. No authentication tokens are sent in HTTP requests.

---

## CDN cache purging

After new assets are committed, the workflow automatically purges the jsDelivr cache for every updated file via `purge.jsdelivr.net`. Nuvio picks up fresh images within minutes rather than waiting up to 7 days for the cache to expire.

The purge script can also be run locally:

```bash
# Dry-run — print URLs, make no requests
python collections/scripts/purge.py --dry-run

# Live purge
python collections/scripts/purge.py

# Override repo slug (useful for forks)
REPO_SLUG=yourname/yourrepo python collections/scripts/purge.py
```

---

## Repository structure

```
tiny-deluxe/
├── .github/
│   └── workflows/
│       ├── generate-backdrops.yml  ← backdrop generation workflow (auto + manual)
│       └── generate-covers.yml     ← cover/focused generation workflow (manual)
├── collections/
│   ├── scripts/
│   │   └── purge.py                ← jsDelivr CDN cache purge script
│   ├── discover/                   ← generated per folder (auto-created)
│   │   ├── backdrop/
│   │   ├── focused/
│   │   ├── cover/
│   │   └── title/
│   ├── streaming/
│   ├── genres/
│   ├── themes/
│   ├── studios/
│   ├── decades/
│   ├── runtime/
│   └── simkl/
├── generate_catalog_assets.py      ← main asset generator
├── nuvio-collections.json          ← catalog config (edit to add/remove catalogs)
├── requirements.txt
└── README.md
```

---

## Credits

- [luckynumb3rs/stremio-perfect-setup](https://github.com/luckynumb3rs/stremio-perfect-setup) — Prism tilted-grid backdrop engine and CDN approach
- [TMDb](https://www.themoviedb.org/) — backdrop and poster images
- [jsDelivr](https://www.jsdelivr.com/) — free CDN for GitHub repos
