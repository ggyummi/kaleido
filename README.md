# tiny-deluxe

Automated pipeline that generates cinematic backdrops and catalog cover cards for [Nuvio Collections](https://github.com/luckynumb3rs/stremio-perfect-setup), served as a free static CDN via [jsDelivr](https://www.jsdelivr.com/). Images refresh automatically every week via GitHub Actions — zero manual maintenance once set up.

Inspired by [luckynumb3rs/stremio-perfect-setup](https://github.com/luckynumb3rs/stremio-perfect-setup), [bramst0ne/prism-wallpapers](https://github.com/bramst0ne/prism-wallpapers), and [betterer-covers](https://betterer-covers.itsrenoria.workers.dev/).

---

## What this repo produces

For each catalog defined in your `AIOMetadata.json` export, the pipeline generates:

```
collections/
└── {Catalog_Name}_{type}/
    ├── backdrop/
    │   ├── {Movie_Title}.jpg    ← cinematic 1920×1080 per-item backdrop
    │   └── {Movie_Title}.webp   ← WebP twin (smaller file size)
    └── cards/
        ├── {Catalog_Name}.jpg   ← catalog cover card  ← use this URL in Nuvio
        └── {Catalog_Name}.webp  ← WebP twin           ← use this URL in Nuvio
```

The **catalog cover card** (`cards/`) is what you paste into your Nuvio Collections config. It uses the top-ranked item's TMDb backdrop as the background with your catalog name overlaid in bold text.

---

## How the CDN works

Files committed to this repo are instantly available at a permanent jsDelivr URL:

```
https://cdn.jsdelivr.net/gh/ggyummi/tiny-deluxe@main/{path/to/file}
```

The URL **never changes** — when the pipeline regenerates a file and pushes the new version, jsDelivr serves the updated image after the cache is purged. This means you paste the URL into Nuvio once and it stays fresh forever.

### Example URLs

```
# Catalog cover card (WebP — recommended for Nuvio)
https://cdn.jsdelivr.net/gh/ggyummi/tiny-deluxe@main/collections/Netflix_movie/cards/Netflix_movie.webp

# Catalog cover card (JPEG fallback)
https://cdn.jsdelivr.net/gh/ggyummi/tiny-deluxe@main/collections/Netflix_movie/cards/Netflix_movie.jpg
```

After your first pipeline run, browse your repo's `collections/` folder to find the exact paths for each catalog. The folder name is derived from the catalog's display name and type (e.g. `🎬 Netflix` + `movie` → `Netflix_movie`).

---

## Setup

### 1. Fork or clone this repo

Rename it to whatever you like (this one is `tiny-deluxe`).

### 2. Export your AIOMetadata config

Open your AIOMetadata app → export → download `AIOMetadata.json` → upload it to the root of this repo. The pipeline reads this file to know which catalogs to generate images for.

### 3. Add GitHub Secrets

Go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret name | Where to get it | Required? |
|---|---|---|
| `TMDB_API_KEY` | [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api) | ✅ Yes |
| `FANART_API_KEY` | [fanart.tv/get-an-api-key](https://fanart.tv/get-an-api-key/) | ✅ Yes |
| `AIOMETADATA_URL` | Your AIOMetadata instance manifest URL | Optional |

`AIOMETADATA_URL` enables live catalog data for Trakt, Simkl, MAL, Kitsu, and PublicMetaDB catalogs. Without it, the pipeline reads `AIOMetadata.json` and uses TMDb-native endpoints only (works for Netflix, Disney+, Prime, etc.).

### 4. Run the pipeline

Go to **Actions → 🎬 Nuvio Backdrop & Card CDN Render → Run workflow**.

The first run will take ~30–60 minutes depending on how many catalogs you have. Subsequent runs are faster because already-generated images are skipped (cache-aware).

---

## Triggers

The pipeline runs automatically in three ways:

| Trigger | When |
|---|---|
| Manual | Actions tab → Run workflow button |
| On file push | Whenever `AIOMetadata.json` is updated in the repo |
| Scheduled | Every Monday at 03:00 UTC |

The weekly schedule keeps your backdrops fresh — new popular titles rotate in as your catalogs change.

---

## Using the images in Nuvio Collections

After your pipeline has run at least once, add a `nuvio-collections.json` file to the root of this repo. This file defines your Nuvio Collections entries with their CDN backdrop and card URLs.

### nuvio-collections.json structure

```json
[
  {
    "id": "netflix-movies",
    "name": "🎬 Netflix",
    "type": "movie",
    "backdropUrl": "https://cdn.jsdelivr.net/gh/ggyummi/tiny-deluxe@main/collections/Netflix_movie/cards/Netflix_movie.webp",
    "cardUrl": "https://cdn.jsdelivr.net/gh/ggyummi/tiny-deluxe@main/collections/Netflix_movie/cards/Netflix_movie.webp"
  },
  {
    "id": "netflix-series",
    "name": "🎬 Netflix",
    "type": "series",
    "backdropUrl": "https://cdn.jsdelivr.net/gh/ggyummi/tiny-deluxe@main/collections/Netflix_series/cards/Netflix_series.webp",
    "cardUrl": "https://cdn.jsdelivr.net/gh/ggyummi/tiny-deluxe@main/collections/Netflix_series/cards/Netflix_series.webp"
  }
]
```

To find the exact folder names for your catalogs, browse `collections/` after a successful pipeline run — each subfolder name is the slug used in the URL.

---

## CDN cache purging

After the pipeline commits new images, the workflow automatically purges the jsDelivr cache for every updated file. This means Nuvio picks up fresh images within minutes instead of waiting up to 7 days for the CDN cache to expire on its own.

The purge script is at `collections/scripts/purge.py` and can also be run locally:

```bash
# Dry-run (see what would be purged, no requests made)
python collections/scripts/purge.py --dry-run

# Live purge
python collections/scripts/purge.py
```

---

## Repository structure

```
tiny-deluxe/
├── .github/
│   └── workflows/
│       └── nuvio_render.yml    ← GitHub Actions workflow
├── collections/
│   ├── scripts/
│   │   └── purge.py            ← jsDelivr cache purge script
│   └── {Catalog_slug}/         ← generated per catalog (auto-created)
│       ├── backdrop/            ← per-item cinematic backdrops
│       └── cards/               ← catalog cover cards (use these in Nuvio)
├── AIOMetadata.json             ← your AIOMetadata export (update to refresh)
├── nuvio-collections.json       ← your Nuvio Collections config (maintain manually)
├── nuvio_pipeline.py            ← main pipeline script
├── requirements.txt             ← Python dependencies
└── README.md
```

---

## Updating your catalogs

To add, remove, or change catalogs:

1. Re-export `AIOMetadata.json` from your AIOMetadata app.
2. Upload it to the repo root (drag-and-drop in the GitHub UI works).
3. The push will automatically trigger the pipeline.
4. New catalogs get generated; removed catalogs keep their old images (you can delete them manually from the `collections/` folder).
5. Update `nuvio-collections.json` with the new URLs.

---

## Running locally

```bash
git clone https://github.com/ggyummi/tiny-deluxe.git
cd tiny-deluxe
pip install -r requirements.txt

export TMDB_API_KEY=your_key_here
export FANART_API_KEY=your_key_here

python nuvio_pipeline.py
```

---

## Credits

- [luckynumb3rs/stremio-perfect-setup](https://github.com/luckynumb3rs/stremio-perfect-setup) — inspiration and CDN approach
- [bramst0ne/prism-wallpapers](https://github.com/bramst0ne/prism-wallpapers) — visual style inspiration
- [betterer-covers](https://betterer-covers.itsrenoria.workers.dev/) — layout inspiration
- [TMDb](https://www.themoviedb.org/) — poster and backdrop images
- [Fanart.tv](https://fanart.tv/) — HD logo assets
- [jsDelivr](https://www.jsdelivr.com/) — free CDN for GitHub repos
