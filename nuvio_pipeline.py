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
    "accept": "application/json",
    "Authorization": f"Bearer {TMDB_API_KEY}"
}

TIMEOUT_LIMIT = 15

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

def resolve_to_tmdb_id(meta_item):
    item_id = str(meta_item.get("id", ""))
    name = meta_item.get("name", "")
    type_str = meta_item.get("type", "movie")
    
    if item_id.startswith("tmdb:"):
        return item_id.split(":")[1]
        
    if item_id.startswith("tt"):
        log(f"   -> Resolving IMDb ID {item_id} via TMDb...")
        url = f"https://api.themoviedb.org/3/find/{item_id}?external_source=imdb_id"
        try:
            response = requests.get(url, headers=HEADERS, timeout=TIMEOUT_LIMIT).json()
            results = response.get("movie_results", []) + response.get("tv_results", [])
            if results:
                return str(results[0]["id"])
        except requests.exceptions.RequestException:
            return None
            
    year = meta_item.get("releaseInfo", "")
    search_type = "movie" if type_str == "movie" else "tv"
    search_url = f"https://api.themoviedb.org/3/search/{search_type}?query={name}&year={year}"
    
    try:
        search_response = requests.get(search_url, headers=HEADERS, timeout=TIMEOUT_LIMIT).json()
        results = search_response.get("results", [])
        if results:
            return str(results[0]["id"])
    except requests.exceptions.RequestException:
        pass
        
    return None

def fetch_assets(tmdb_id, type_str):
    poster_path = f"temp_poster_{tmdb_id}.jpg"
    logo_path = f"temp_logo_{tmdb_id}.png"
    
    # Fetch Poster
    tmdb_url = f"https://api.themoviedb.org/3/{type_str}/{tmdb_id}/images"
    try:
        img_data = requests.get(tmdb_url, headers=HEADERS, timeout=TIMEOUT_LIMIT).json()
        posters = img_data.get("posters", [])
        if not posters:
            return None, None
        
        poster_url = f"https://image.tmdb.org/t/p/original{posters[0]['file_path']}"
        with open(poster_path, 'wb') as f:
            f.write(requests.get(poster_url, timeout=TIMEOUT_LIMIT).content)
    except requests.exceptions.RequestException:
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
    except requests.exceptions.RequestException:
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

    # 1. Check multiple locations for the file
    target_file = None
    if os.path.exists("AIOMetadata.json"):
        target_file = "AIOMetadata.json"
    elif os.path.exists("templates/AIOMetadata.json"):
        target_file = "templates/AIOMetadata.json"
        
    if not target_file:
        log("[CRITICAL] Could not find AIOMetadata.json in the root OR in the templates folder. Please upload it.")
        return

    log(f"Reading local {target_file}...")
    with open(target_file, 'r', encoding='utf-8') as f:
        manifest = json.load(f)

   # 2. Handle both exported raw lists and Stremio dictionary formats
    if isinstance(manifest, list):
        catalogs = manifest
    else:
        # Check if catalogs are at the root level OR hidden inside a 'config' section
        if "config" in manifest and "catalogs" in manifest["config"]:
            catalogs = manifest["config"]["catalogs"]
        else:
            catalogs = manifest.get("catalogs", [])
        
    log(f"Found {len(catalogs)} catalogs in your file.")

    if len(catalogs) == 0:
        log("[WARNING] The file was read successfully, but no catalogs were found inside it.")
        return

    for catalog in catalogs:
        raw_name = catalog.get("name", "Unknown_Catalog")
        catalog_name = sanitize_filename(raw_name)
        
        dir_logo_cards = os.path.join("collections", catalog_name, "logo_cards")
        dir_dynamic = os.path.join("collections", catalog_name, "dynamic_grids")
        os.makedirs(dir_logo_cards, exist_ok=True)
        os.makedirs(dir_dynamic, exist_ok=True)
        
        # Build the Stremio public API endpoint for this specific catalog ID
        catalog_url = f"https://aiometadata.strem.fun/catalog/{catalog['type']}/{catalog['id']}.json"
        log(f"\nProcessing Catalog: {raw_name}")
        
        try:
            items = requests.get(catalog_url, timeout=TIMEOUT_LIMIT).json().get("metas", [])
            log(f" -> Catalog contains {len(items)} items.")
        except requests.exceptions.RequestException:
            log(f" [WARNING] Could not fetch movies for {raw_name}. Skipping.")
            continue

        for item in items:
            title = item.get("name", "Unknown")
            clean_title = sanitize_filename(title)
            log(f" * Working on: {title}")
            
            tmdb_id = resolve_to_tmdb_id(item)
            if not tmdb_id:
                continue
                
            out_logo_jpg = os.path.join(dir_logo_cards, f"{clean_title}.jpg")
            out_logo_webp = os.path.join(dir_logo_cards, f"{clean_title}.webp")
            out_dynamic_jpg = os.path.join(dir_dynamic, f"{clean_title}.jpg")
            out_dynamic_webp = os.path.join(dir_dynamic, f"{clean_title}.webp")
            
            if os.path.exists(out_logo_jpg) and os.path.exists(out_dynamic_jpg):
                log(f"   -> Skip: All backdrops exist.")
                continue
                
            poster_file, logo_file = fetch_assets(tmdb_id, item.get("type", "movie"))
            if not poster_file or not logo_file:
                log(f"   -> Skip: Missing assets.")
                continue
                
            if not os.path.exists(out_logo_jpg):
                log(f"   -> Launching logo_cards.py...")
                try:
                    subprocess.run(["python", "logo_cards.py", "--poster", poster_file, "--logo", logo_file, "--output", out_logo_jpg, "--skip-logos"], check=True, timeout=60)
                    if os.path.exists(out_logo_jpg): convert_to_webp(out_logo_jpg, out_logo_webp)
                except: pass

            if not os.path.exists(out_dynamic_jpg):
                log(f"   -> Launching backdrop_T2.py...")
                try:
                    subprocess.run(["python", "backdrop_T2.py", "--poster", poster_file, "--logo", logo_file, "--output", out_dynamic_jpg, "--skip-logos"], check=True, timeout=60)
                    if os.path.exists(out_dynamic_jpg): convert_to_webp(out_dynamic_jpg, out_dynamic_webp)
                except: pass

            if os.path.exists(poster_file): os.remove(poster_file)
            if os.path.exists(logo_file): os.remove(logo_file)

    log("\nPipeline processing cycle has completely finished!")

if __name__ == "__main__":
    main()
