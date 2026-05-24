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
import time
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
RATE_DELAY  = 0.5                               # seconds between requests to avoid rate limiting


# ── Asset discovery ────────────────────────────────────────────────────────────────────

def iter_asset_paths(root: Path):
    """
    Yield repo-relative POSIX paths for every generated image file under:

      Flat layout: collections/{folder}/{asset_type}/{catalog}.jpg
        collections/*/backdrop/*
        collections/*/cards/*
        collections/*/focused/*
        collections/*/cover/*

    Both .jpg and .webp files are included.
    """
    # flat layout: collections/{folder}/{asset_type}/{catalog}.jpg
    # legacy layout: collections/{slug}/backdrop/* and collections/{slug}/cards/*
    patterns = ["*/backdrop/*", "*/cards/*", "*/focused/*", "*/cover/*"]
    for pattern in patterns:
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
    parser.add_argument(
        "--delay",
        type=float,
        default=RATE_DELAY,
        help=f"Delay between requests in seconds to avoid rate limiting (default: {RATE_DELAY}).",
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
    for i, url in enumerate(urls):
        print(f"  {url}")
        if args.dry_run:
            continue
        try:
            status = purge_url(url, timeout=args.timeout)
            print(f"    → status {status}")
        except (HTTPError, URLError) as exc:
            print(f"    → FAILED: {exc}", file=sys.stderr)
            failed += 1
        if i < len(urls) - 1:
            time.sleep(args.delay)

    if failed:
        print(f"\n⚠  {failed} purge request(s) failed.", file=sys.stderr)
        return 1

    print(f"\n✅  Done — {len(urls)} URL(s) purged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
