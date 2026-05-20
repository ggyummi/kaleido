#!/usr/bin/env python3
"""
collections/scripts/purge.py
───────────────────────────────────────────────────────────────────────────
tiny-deluxe · jsDelivr CDN cache purge script

Walks collections/*/backdrop/* and collections/*/cards/* and hits
purge.jsdelivr.net for every file so updated images are served immediately
after a pipeline run instead of waiting up to 7 days for cache expiry.

Usage
───────────────────────────────────────────────────────────────────────────
  # Normal run (from repo root via GitHub Actions):
  python collections/scripts/purge.py

  # Dry-run (print URLs, make no requests):
  python collections/scripts/purge.py --dry-run

  # Override repo slug (useful for testing forks):
  REPO_SLUG=yourname/yourrepo python collections/scripts/purge.py

Environment variables
───────────────────────────────────────────────────────────────────────────
  REPO_SLUG   GitHub <owner>/<repo> string.
              Defaults to "ggyummi/tiny-deluxe".
              GitHub Actions sets this automatically via the workflow env block.
"""

import argparse
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

# ── Configuration ────────────────────────────────────────────────────────────────────

SCRIPT_DIR  = Path(__file__).resolve().parent
REPO_ROOT   = SCRIPT_DIR.parent.parent          # collections/scripts/ -> repo root
COLLECTIONS = REPO_ROOT / "collections"

# Repo slug: prefer the env var injected by the workflow, fall back to default.
REPO_SLUG   = os.environ.get("REPO_SLUG", "ggyummi/tiny-deluxe")

PURGE_BASE  = "https://purge.jsdelivr.net/gh"
CDN_REF     = "@main"                           # always purge the @main ref
TIMEOUT     = 20                                # seconds per request


# ── Asset discovery ────────────────────────────────────────────────────────────────────

def iter_asset_paths(root: Path):
    """
    Yield repo-relative POSIX paths for every generated image file under:

      Legacy pipeline layout (single-level):
        collections/*/backdrop/*
        collections/*/cards/*

      Catalog asset generator layout (two-level: {folder}/{catalog}/...):
        collections/*/*/backdrops/*
        collections/*/*/focused/*
        collections/*/*/cover/*

    Both .jpg and .webp files are included.
    """
    legacy_patterns = ["*/backdrop/*", "*/cards/*"]
    catalog_patterns = ["*/*/backdrops/*", "*/*/focused/*", "*/*/cover/*"]
    for pattern in legacy_patterns + catalog_patterns:
        for path in sorted(root.glob(pattern)):
            if path.is_file() and path.suffix in {".jpg", ".webp"}:
                yield path.relative_to(REPO_ROOT).as_posix()


def build_purge_urls(repo_slug: str) -> list[str]:
    paths = list(iter_asset_paths(COLLECTIONS))
    return [f"{PURGE_BASE}/{repo_slug}{CDN_REF}/{p}" for p in paths]


# ── Purge ──────────────────────────────────────────────────────────────────────────────

def purge_url(url: str, timeout: int) -> int:
    with urlopen(url, timeout=timeout) as resp:
        return resp.status


# ── Entry point ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Purge jsDelivr CDN cache for tiny-deluxe collection assets."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print purge URLs without making requests.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=TIMEOUT,
        help=f"Per-request timeout in seconds (default: {TIMEOUT}).",
    )
    args = parser.parse_args()

    urls = build_purge_urls(REPO_SLUG)

    if not urls:
        print("No collection assets found to purge.")
        print(f"Looked in: {COLLECTIONS}")
        return 0

    print(f"Purging {len(urls)} CDN URL(s) for repo '{REPO_SLUG}' ...")
    if args.dry_run:
        print("(dry-run — no requests will be made)")

    failed = 0
    for url in urls:
        print(f"  {url}")
        if args.dry_run:
            continue
        try:
            status = purge_url(url, timeout=args.timeout)
            print(f"    → status {status}")
        except (HTTPError, URLError) as exc:
            print(f"    → FAILED: {exc}", file=sys.stderr)
            failed += 1

    if failed:
        print(f"\n⚠  {failed} purge request(s) failed.", file=sys.stderr)
        return 1

    print(f"\n✅  Done — {len(urls)} URL(s) purged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
