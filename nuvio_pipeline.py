import os
import re
import sys
import requests
import subprocess
from PIL import Image

# ---------------------------------------------------------
# Configuration & Secrets
# ---------------------------------------------------------
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")
FANART_API_KEY = os.environ.get("FANART_API_KEY")
AIOMETADATA_URL = os.environ.get("AIOMETADATA_URL")

HEADERS = {
    "accept": "application/json",
    "Authorization": f"Bearer {TMDB_API_KEY}"
}

# Safety limit for all web requests (in seconds)
TIMEOUT_LIMIT = 15

def log(message):
    """Forces logs to print instantly to the GitHub Action terminal."""
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
            log(f"   [WARNING] TMDb lookup timed out for {name}")
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
        log(f"   [WARNING] Text search timed out for {name}")
        
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
        log(f"   [WARNING] Failed downloading poster for TMDb ID {tmdb_id}")
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
        log(f"   [WARNING] Failed downloading logo from Fanart.tv for TMDb ID {tmdb_id}")
        if os.path.exists(poster_path): os.remove(poster_path)
        return None, None
        
    return poster_path, logo_path

# ---------------------------------------------------------
# Main Execution Routine
# ---------------------------------------------------------
def main():
    if not all([TMDB_API_KEY, FANART_API_KEY, AIOMETADATA_URL]):
        log("[CRITICAL] Missing API Keys. Verification failed.")
        return

    log("Connecting to Nuvio manifest endpoint...")
    try:
        manifest = requests.get(AIOMETADATA_URL, timeout=TIMEOUT_LIMIT).json()
    except requests.exceptions.RequestException as e:
        log(f"[CRITICAL] Could not connect to AIOMETADATA_URL: {e}")
        return

    catalogs = manifest.get("catalogs", [])
    log(f"Found {len(catalogs)} catalogs to process.")

    for catalog in catalogs:
        raw_name = catalog.get("name", "Unknown_Catalog")
        catalog_name = sanitize_filename(raw_name)
        
        dir_logo_cards = os.path.join("collections", catalog_name, "logo_cards")
        dir_dynamic = os.path.join("collections", catalog_name, "dynamic_grids")
        os.makedirs(dir_logo_cards, exist_ok=True)
        os.makedirs(dir_dynamic, exist_ok=True)
        
        catalog_url = AIOMETADATA_URL.replace("manifest.json", f"catalog/{catalog['type']}/{catalog['id']}.json")
        log(f"\nProcessing Catalog: {raw_name}")
        
        try:
            items = requests.get(catalog_url, timeout=TIMEOUT_LIMIT).json().get("metas", [])
            log(f" -> Catalog contains {len(items)} items.")
        except requests.exceptions.RequestException:
            log(f" [WARNING] Could not fetch catalog items for {raw_name}. Skipping.")
            continue

        for item in items:
            title = item.get("name", "Unknown")
            clean_title = sanitize_filename(title)
            log(f" * Working on: {title}")
            
            tmdb_id = resolve_to_tmdb_id(item)
            if not tmdb_id:
                log(f"   -> Skip: Unable to resolve universal ID.")
                continue
                
            out_logo_jpg = os.path.join(dir_logo_cards, f"{clean_title}.jpg")
            out_logo_webp = os.path.join(dir_logo_cards, f"{clean_title}.webp")
            out_dynamic_jpg = os.path.join(dir_dynamic, f"{clean_title}.jpg")
            out_dynamic_webp = os.path.join(dir_dynamic, f"{clean_title}.webp")
            
            if os.path.exists(out_logo_jpg) and os.path.exists(out_dynamic_jpg):
                log(f"   -> Skip: All backdrops already exist in CDN cache.")
                continue
                
            poster_file, logo_file = fetch_assets(tmdb_id, item.get("type", "movie"))
            if not poster_file or not logo_file:
                log(f"   -> Skip: Missing structural assets on TMDb/Fanart.")
                continue
                
            # STYLE 1: Logo Cards
            if not os.path.exists(out_logo_jpg):
                log(f"   -> Launching logo_cards.py engine...")
                try:
                    subprocess.run(
                        ["python", "logo_cards.py", "--poster", poster_file, "--logo", logo_file, "--output", out_logo_jpg, "--skip-logos"],
                        check=True, timeout=60
                    )
                    if os.path.exists(out_logo_jpg):
                        convert_to_webp(out_logo_jpg, out_logo_webp)
                except subprocess.TimeoutExpired:
                    log(f"   [WARNING] logo_cards.py froze for 60 seconds on {title}. Terminated.")
                except subprocess.CalledProcessError:
                    log(f"   [WARNING] logo_cards.py internal error for {title}.")

            # STYLE 2: Dynamic Grids
            if not os.path.exists(out_dynamic_jpg):
                log(f"   -> Launching backdrop_T2.py engine...")
                try:
                    subprocess.run(
                        ["python", "backdrop_T2.py", "--poster", poster_file, "--logo", logo_file, "--output", out_dynamic_jpg, "--skip-logos"],
                        check=True, timeout=60
                    )
                    if os.path.exists(out_dynamic_jpg):
                        convert_to_webp(out_dynamic_jpg, out_dynamic_webp)
                except subprocess.TimeoutExpired:
                    log(f"   [WARNING] backdrop_T2.py froze for 60 seconds on {title}. Terminated.")
                except subprocess.CalledProcessError:
                    log(f"   [WARNING] backdrop_T2.py internal error for {title}.")

            # Cleanup temp files
            if os.path.exists(poster_file): os.remove(poster_file)
            if os.path.exists(logo_file): os.remove(logo_file)

    log("\nPipeline processing cycle has completely finished!")

if __name__ == "__main__":
    main()
