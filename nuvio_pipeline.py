import os
import re
import time
import json
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

# ---------------------------------------------------------
# Utility Functions
# ---------------------------------------------------------
def sanitize_filename(name):
    """Removes invalid characters for safe directory and file names."""
    return re.sub(r'[\\/*?:"<>|]', "", name).replace(" ", "_")

def convert_to_webp(jpg_path, webp_path):
    """Converts a standard JPG to an optimized WEBP format."""
    try:
        with Image.open(jpg_path) as img:
            img.save(webp_path, "WEBP", quality=90)
        print(f"Optimized to WEBP: {webp_path}")
    except Exception as e:
        print(f"Error converting to WEBP: {e}")

# ---------------------------------------------------------
# Omni-Identifier Fallback Engine
# ---------------------------------------------------------
def resolve_to_tmdb_id(meta_item):
    """Maps varying external IDs (Trakt, IMDb, TVDB, etc.) to a numeric TMDb ID."""
    item_id = str(meta_item.get("id", ""))
    name = meta_item.get("name", "")
    type_str = meta_item.get("type", "movie")
    
    # Tier 1: Direct TMDb ID
    if item_id.startswith("tmdb:"):
        return item_id.split(":")[1]
        
    # Tier 2: IMDb ID lookup via TMDb API
    if item_id.startswith("tt"):
        print(f"Resolving IMDb ID {item_id} for '{name}'...")
        url = f"https://api.themoviedb.org/3/find/{item_id}?external_source=imdb_id"
        response = requests.get(url, headers=HEADERS).json()
        results = response.get("movie_results", []) + response.get("tv_results", [])
        if results:
            return str(results[0]["id"])
            
    # Tier 3: Text-based search fallback (Title + Year)
    year = meta_item.get("releaseInfo", "")
    print(f"Falling back to text search for '{name}' ({year})...")
    search_type = "movie" if type_str == "movie" else "tv"
    search_url = f"https://api.themoviedb.org/3/search/{search_type}?query={name}&year={year}"
    
    search_response = requests.get(search_url, headers=HEADERS).json()
    results = search_response.get("results", [])
    
    if results:
        return str(results[0]["id"])
        
    print(f"Failed to resolve TMDb ID for {name}")
    return None

# ---------------------------------------------------------
# Asset Fetching
# ---------------------------------------------------------
def fetch_assets(tmdb_id, type_str):
    """Fetches the poster from TMDb and the HD logo from Fanart.tv."""
    poster_path = f"temp_poster_{tmdb_id}.jpg"
    logo_path = f"temp_logo_{tmdb_id}.png"
    
    # Get Poster
    tmdb_url = f"https://api.themoviedb.org/3/{type_str}/{tmdb_id}/images"
    img_data = requests.get(tmdb_url, headers=HEADERS).json()
    posters = img_data.get("posters", [])
    if not posters:
        return None, None
        
    poster_url = f"https://image.tmdb.org/t/p/original{posters[0]['file_path']}"
    with open(poster_path, 'wb') as f:
        f.write(requests.get(poster_url).content)
        
    # Get Logo
    fanart_url = f"https://webservice.fanart.tv/v3/{'movies' if type_str == 'movie' else 'tv'}/{tmdb_id}?api_key={FANART_API_KEY}"
    fanart_data = requests.get(fanart_url).json()
    logos = fanart_data.get("hdmovielogo", []) or fanart_data.get("hdtvlogo", [])
    
    if not logos:
        if os.path.exists(poster_path): os.remove(poster_path)
        return None, None
        
    logo_url = logos[0]["url"]
    with open(logo_path, 'wb') as f:
        f.write(requests.get(logo_url).content)
        
    return poster_path, logo_path

# ---------------------------------------------------------
# Main Execution Routine
# ---------------------------------------------------------
def main():
    if not all([TMDB_API_KEY, FANART_API_KEY, AIOMETADATA_URL]):
        print("Missing API Keys. Please check environment variables.")
        return

    print("Fetching Nuvio manifest...")
    manifest = requests.get(AIOMETADATA_URL).json()
    catalogs = manifest.get("catalogs", [])

    for catalog in catalogs:
        raw_name = catalog.get("name", "Unknown_Catalog")
        catalog_name = sanitize_filename(raw_name)
        target_dir = os.path.join("collections", catalog_name, "backdrop")
        os.makedirs(target_dir, exist_ok=True)
        
        print(f"\nProcessing Catalog: {raw_name}")
        
        # Parse catalog items (Assuming standard Stremio catalog structure)
        catalog_url = AIOMETADATA_URL.replace("manifest.json", f"catalog/{catalog['type']}/{catalog['id']}.json")
        try:
            items = requests.get(catalog_url).json().get("metas", [])
        except:
            print(f"Could not fetch items for {raw_name}. Skipping.")
            continue

        for item in items:
            title = sanitize_filename(item.get("name", "Unknown"))
            tmdb_id = resolve_to_tmdb_id(item)
            
            if not tmdb_id:
                continue
                
            out_jpg = os.path.join(target_dir, f"{title}.jpg")
            out_webp = os.path.join(target_dir, f"{title}.webp")
            
            # Caching / Skip Check
            if os.path.exists(out_jpg) and os.path.exists(out_webp):
                print(f"Skipping {title}: Assets already exist.")
                continue
                
            print(f"Rendering assets for {title}...")
            poster_file, logo_file = fetch_assets(tmdb_id, item.get("type", "movie"))
            
            if not poster_file or not logo_file:
                print(f"Missing source assets for {title}. Skipping.")
                continue
                
            # Handoff to rendering engine
            try:
                # Assuming bramst0ne's script is named 'render.py' or similar in the repo root
                subprocess.run(
                    ["python", "render.py", "--poster", poster_file, "--logo", logo_file, "--output", out_jpg],
                    check=True
                )
                
                if os.path.exists(out_jpg):
                    convert_to_webp(out_jpg, out_webp)
                    
            except subprocess.CalledProcessError as e:
                print(f"Rendering engine failed for {title}: {e}")
            finally:
                # Cleanup temp files
                if os.path.exists(poster_file): os.remove(poster_file)
                if os.path.exists(logo_file): os.remove(logo_file)
                
    print("\nPipeline execution complete!")

if __name__ == "__main__":
    main()
