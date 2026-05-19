import os
import re
import sys
import json
import requests
import subprocess
from PIL import Image

# ---------------------------------------------------------
# Configuration & Secrets
# ---------------------------------------------------------
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
FANART_API_KEY = os.environ.get("FANART_API_KEY")

HEADERS = {
    "accept": "application/json"
}

TIMEOUT_LIMIT = 20

def log(message):
    print(message, flush=True)

def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "", name).replace(" ", "_")

def convert_to_webp(jpg_path, webp_path):
    try:
        with Image.open(jpg_path) as img:
            img.save(webp_path, "WEBP", quality=90)
        log(f"   -> Successfully optimized to WEBP.")
    except Exception as e:
        log(f"   [WARNING] Error converting to WEBP: {e}")

def fetch_assets(tmdb_id, type_str):
    poster_path = f"temp_poster_{tmdb_id}.jpg"
    logo_path = f"temp_logo_{tmdb_id}.png"
    
    # Fetch Poster
    tmdb_url = f"https://api.themoviedb.org/3/{type_str}/{tmdb_id}/images"
    try:
        img_data = requests.get(tmdb_url, headers=HEADERS, params={"api_key": TMDB_API_KEY}, timeout=TIMEOUT_LIMIT).json()
        posters = img_data.get("posters", [])
        if not posters:
            return None, None
        
        poster_url = f"https://image.tmdb.org/t/p/original{posters[0]['file_path']}"
        with open(poster_path, 'wb') as f:
            f.write(requests.get(poster_url, timeout=TIMEOUT_LIMIT).content)
    except Exception:
        return None, None
        
    # Fetch Logo
    fanart_url = f"https://webservice.fanart.tv/v3/{'movies' if type_str == 'movie' else 'tv'}/{tmdb_id}?api_key={FANART_API_KEY}"
    try:
        fanart_data = requests.get(fanart_url, timeout=TIMEOUT_LIMIT).json()
        logos = fanart_data.get("hdmovielogo", []) or fanart_data.get("hdtvlogo", [])
        
        if not logos:
            if os.path.exists(poster_path): os.remove(poster_path)
            return None, None
            
        logo_url = logos[0]["url"]
        with open(logo_path, 'wb') as f:
            f.write(requests.get(logo_url, timeout=TIMEOUT_LIMIT).content)
    except Exception:
        if os.path.exists(poster_path): os.remove(poster_path)
        return None, None
        
    return poster_path, logo_path

# ---------------------------------------------------------
# Main Execution Routine
# ---------------------------------------------------------
def main():
    if not all([TMDB_API_KEY, FANART_API_KEY]):
        log("[CRITICAL] Missing API Keys. Verification failed.")
        return

    target_file = None
    if os.path.exists("AIOMetadata.json"):
        target_file = "AIOMetadata.json"
    elif os.path.exists("templates/AIOMetadata.json"):
        target_file = "templates/AIOMetadata.json"
        
    if not target_file:
        log("[CRITICAL] Could not find AIOMetadata.json. Please upload it.")
        return

    log(f"Reading local {target_file}...")
    with open(target_file, 'r', encoding='utf-8') as f:
        manifest = json.load(f)

    if isinstance(manifest, list):
        catalogs = manifest
    else:
        if "config" in manifest and "catalogs" in manifest["config"]:
            catalogs = manifest["config"]["catalogs"]
        else:
            catalogs = manifest.get("catalogs", [])
        
    log(f"Found {len(catalogs)} catalogs in your file.")

    for catalog in catalogs:
        raw_name = catalog.get("name", "Unknown_Catalog")
        catalog_name = sanitize_filename(raw_name)
        catalog_id = catalog.get("id", "").lower()
        cat_type = "movie" if catalog.get("type") == "movie" else "tv"
        
        dir_logo_cards = os.path.join("collections", catalog_name, "logo_cards")
        dir_dynamic = os.path.join("collections", catalog_name, "dynamic_grids")
        os.makedirs(dir_logo_cards, exist_ok=True)
        os.makedirs(dir_dynamic, exist_ok=True)
        
        log(f"\nProcessing Catalog: {raw_name}")
        
        tmdb_url = None
        metadata = catalog.get("metadata", {})
        discover = metadata.get("discover", {})
        
        # --- THE NEW OVERRIDE ENGINE ---
        if "anime" in catalog_id or "mal" in catalog_id or "kitsu" in catalog_id or "anilist" in catalog_id:
            # Fake it with popular Anime from TMDB
            log(" -> Detected Anime/External list. Using representative Anime pool.")
            if cat_type == "movie":
                tmdb_url = "https://api.themoviedb.org/3/discover/movie?sort_by=popularity.desc&include_adult=false&with_genres=16&with_original_language=ja&vote_count.gte=20&with_release_type=4|5|6"
            else:
                tmdb_url = "https://api.themoviedb.org/3/discover/tv?sort_by=popularity.desc&include_adult=false&with_genres=16&with_original_language=ja&vote_count.gte=10&with_status=0|3|4|5"
                
        elif "simkl" in catalog_id or "trakt" in catalog_id or "pmdb" in catalog_id or "publicmetadb" in catalog_id:
            # Fake it with generic popular movies/shows from TMDB
            log(" -> Detected Private Tracking list. Using representative popular pool.")
            tmdb_url = f"https://api.themoviedb.org/3/{cat_type}/popular?language=en-US"
            
        elif discover and "params" in discover:
            # Standard TMDB dynamic filter
            media_type = "movie" if discover.get("mediaType") == "movie" else "tv"
            params = discover.get("params", {})
            query_parts = []
            for k, v in params.items():
                if v is True: query_parts.append(f"{k}=true")
                elif v is False: query_parts.append(f"{k}=false")
                elif v is not None: query_parts.append(f"{k}={v}")
            query_string = "&".join(query_parts)
            tmdb_url = f"https://api.themoviedb.org/3/discover/{media_type}?{query_string}"
            
        elif "trending" in catalog_id:
            tmdb_url = f"https://api.themoviedb.org/3/trending/{cat_type}/week?language=en-US"
        elif "top_rated" in catalog_id:
            tmdb_url = f"https://api.themoviedb.org/3/{cat_type}/top_rated?language=en-US"
        else:
            tmdb_url = f"https://api.themoviedb.org/3/{cat_type}/popular?language=en-US"
            
        # --- END OVERRIDE ENGINE ---
            
        try:
            response = requests.get(tmdb_url, headers=HEADERS, params={"api_key": TMDB_API_KEY}, timeout=TIMEOUT_LIMIT)
            response.raise_for_status() 
            items = response.json().get("results", [])
            log(f" -> Fetched {len(items)} items directly from TMDB.")
        except Exception as e:
            log(f"   [WARNING] Could not fetch movies for {raw_name}. Error: {e}")
            continue

        for item in items:
            title = item.get("title") or item.get("name", "Unknown")
            clean_title = sanitize_filename(title)
            tmdb_id = str(item.get("id"))
            
            log(f" * Working on: {title}")
                
            out_logo_jpg = os.path.join(dir_logo_cards, f"{clean_title}.jpg")
            out_logo_webp = os.path.join(dir_logo_cards, f"{clean_title}.webp")
            out_dynamic_jpg = os.path.join(dir_dynamic, f"{clean_title}.jpg")
            out_dynamic_webp = os.path.join(dir_dynamic, f"{clean_title}.webp")
            
            if os.path.exists(out_logo_jpg) and os.path.exists(out_dynamic_jpg):
                log(f"   -> Skip: All backdrops exist.")
                continue
                
            poster_file, logo_file = fetch_assets(tmdb_id, cat_type)
            if not poster_file or not logo_file:
                log(f"   -> Skip: Missing assets.")
                continue
                
            if not os.path.exists(out_logo_jpg):
                try:
                    subprocess.run(["python", "logo_cards.py", "--poster", poster_file, "--logo", logo_file, "--output", out_logo_jpg, "--skip-logos"], check=True, timeout=60)
                    if os.path.exists(out_logo_jpg): convert_to_webp(out_logo_jpg, out_logo_webp)
                except: pass

            if not os.path.exists(out_dynamic_jpg):
                try:
                    subprocess.run(["python", "backdrop_T2.py", "--poster", poster_file, "--logo", logo_file, "--output", out_dynamic_jpg, "--skip-logos"], check=True, timeout=60)
                    if os.path.exists(out_dynamic_jpg): convert_to_webp(out_dynamic_jpg, out_dynamic_webp)
                except: pass

            if os.path.exists(poster_file): os.remove(poster_file)
            if os.path.exists(logo_file): os.remove(logo_file)

    log("\nPipeline processing cycle has completely finished!")

if __name__ == "__main__":
    main()
