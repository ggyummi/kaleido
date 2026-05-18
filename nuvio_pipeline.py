import os
import re
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

def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "", name).replace(" ", "_")

def convert_to_webp(jpg_path, webp_path):
    try:
        with Image.open(jpg_path) as img:
            img.save(webp_path, "WEBP", quality=90)
    except Exception as e:
        print(f"Error converting to WEBP: {e}")

def resolve_to_tmdb_id(meta_item):
    item_id = str(meta_item.get("id", ""))
    name = meta_item.get("name", "")
    type_str = meta_item.get("type", "movie")
    
    if item_id.startswith("tmdb:"):
        return item_id.split(":")[1]
        
    if item_id.startswith("tt"):
        url = f"https://api.themoviedb.org/3/find/{item_id}?external_source=imdb_id"
        response = requests.get(url, headers=HEADERS).json()
        results = response.get("movie_results", []) + response.get("tv_results", [])
        if results:
            return str(results[0]["id"])
            
    year = meta_item.get("releaseInfo", "")
    search_type = "movie" if type_str == "movie" else "tv"
    search_url = f"https://api.themoviedb.org/3/search/{search_type}?query={name}&year={year}"
    
    search_response = requests.get(search_url, headers=HEADERS).json()
    results = search_response.get("results", [])
    if results:
        return str(results[0]["id"])
        
    return None

def fetch_assets(tmdb_id, type_str):
    poster_path = f"temp_poster_{tmdb_id}.jpg"
    logo_path = f"temp_logo_{tmdb_id}.png"
    
    tmdb_url = f"https://api.themoviedb.org/3/{type_str}/{tmdb_id}/images"
    img_data = requests.get(tmdb_url, headers=HEADERS).json()
    posters = img_data.get("posters", [])
    if not posters:
        return None, None
        
    poster_url = f"https://image.tmdb.org/t/p/original{posters[0]['file_path']}"
    with open(poster_path, 'wb') as f:
        f.write(requests.get(poster_url).content)
        
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
        print("Missing API Keys.")
        return

    manifest = requests.get(AIOMETADATA_URL).json()
    catalogs = manifest.get("catalogs", [])

    for catalog in catalogs:
        raw_name = catalog.get("name", "Unknown_Catalog")
        catalog_name = sanitize_filename(raw_name)
        
        # Create separate directories for different styles
        dir_logo_cards = os.path.join("collections", catalog_name, "logo_cards")
        dir_dynamic = os.path.join("collections", catalog_name, "dynamic_grids")
        os.makedirs(dir_logo_cards, exist_ok=True)
        os.makedirs(dir_dynamic, exist_ok=True)
        
        catalog_url = AIOMETADATA_URL.replace("manifest.json", f"catalog/{catalog['type']}/{catalog['id']}.json")
        try:
            items = requests.get(catalog_url).json().get("metas", [])
        except:
            continue

        for item in items:
            title = sanitize_filename(item.get("name", "Unknown"))
            tmdb_id = resolve_to_tmdb_id(item)
            if not tmdb_id:
                continue
                
            # Define output paths for both styles
            out_logo_jpg = os.path.join(dir_logo_cards, f"{title}.jpg")
            out_logo_webp = os.path.join(dir_logo_cards, f"{title}.webp")
            out_dynamic_jpg = os.path.join(dir_dynamic, f"{title}.jpg")
            out_dynamic_webp = os.path.join(dir_dynamic, f"{title}.webp")
            
            # Skip check: Only skip if ALL styles are already generated
            if os.path.exists(out_logo_jpg) and os.path.exists(out_dynamic_jpg):
                print(f"Skipping {title}: All assets already exist.")
                continue
                
            poster_file, logo_file = fetch_assets(tmdb_id, item.get("type", "movie"))
            if not poster_file or not logo_file:
                continue
                
            # STYLE 1: Logo Cards
            if not os.path.exists(out_logo_jpg):
                print(f"Generating Logo Card for {title}...")
                try:
                    subprocess.run(
                        ["python", "logo_cards.py", "--poster", poster_file, "--logo", logo_file, "--output", out_logo_jpg, "--skip-logos"],
                        check=True
                    )
                    if os.path.exists(out_logo_jpg):
                        convert_to_webp(out_logo_jpg, out_logo_webp)
                except subprocess.CalledProcessError:
                    print(f"Logo card generation failed for {title}")

            # STYLE 2: Dynamic Grids (Backdrop T2)
            if not os.path.exists(out_dynamic_jpg):
                print(f"Generating Dynamic Grid for {title}...")
                try:
                    subprocess.run(
                        ["python", "backdrop_T2.py", "--poster", poster_file, "--logo", logo_file, "--output", out_dynamic_jpg, "--skip-logos"],
                        check=True
                    )
                    if os.path.exists(out_dynamic_jpg):
                        convert_to_webp(out_dynamic_jpg, out_dynamic_webp)
                except subprocess.CalledProcessError:
                    print(f"Dynamic grid generation failed for {title}")

            # Cleanup temp files
            if os.path.exists(poster_file): os.remove(poster_file)
            if os.path.exists(logo_file): os.remove(logo_file)

if __name__ == "__main__":
    main()
