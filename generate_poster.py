import os
import requests
from PIL import Image, ImageFilter, ImageDraw, ImageFont
from io import BytesIO

# --- Configuration ---
TMDB_POSTER_URL = "https://image.tmdb.org/t/p/original/jRf89HVEtBZiSnOXXWDhZOfuTwW.jpg" 
GENRE_TEXT = "Thriller"
RATING_TEXT = "★ 7.2"
OUTPUT_FILE = "custom_poster_final.jpg"
TARGET_WIDTH = 800

def download_font():
    """Fetches a clean, static UI font using an unbreakable direct link."""
    font_path = "DejaVuSans.ttf"
    if not os.path.exists(font_path):
        print("Downloading font...")
        url = "https://raw.githubusercontent.com/matplotlib/matplotlib/v3.8.0/lib/matplotlib/mpl-data/fonts/ttf/DejaVuSans.ttf"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=10)
        
        if response.status_code == 200:
            with open(font_path, "wb") as f:
                f.write(response.content)
            print("Font downloaded successfully.")
        else:
            raise Exception(f"Font download failed with status code: {response.status_code}")
            
    return font_path

def create_custom_poster():
    # 1. Fetch and scale the image
    print("Fetching poster from TMDB...")
    headers = {'User-Agent': 'Mozilla/5.0'}
    response = requests.get(TMDB_POSTER_URL, headers=headers)
    if response.status_code != 200:
        raise Exception(f"Failed to download image. TMDB returned: {response.status_code}")
        
    img = Image.open(BytesIO(response.content)).convert("RGBA")
    ratio = TARGET_WIDTH / img.width
    new_height = int(img.height * ratio)
    img = img.resize((TARGET_WIDTH, new_height), Image.Resampling.LANCZOS)
    width, height = img.size

    # 2. Define the bottom region (taller, bottom 30%)
    blur_height = int(height * 0.30) 
    bottom_box = (0, height - blur_height, width, height)
    bottom_region = img.crop(bottom_box)
    
    # 3. Apply a heavy, straight uniform blur (No radial masks)
    blurred_bottom = bottom_region.filter(ImageFilter.GaussianBlur(radius=30)) 
    img.paste(blurred_bottom, bottom_box)

    # 4. Add a strong dark gradient overlay
    overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    
    for y in range(height - blur_height, height):
        # Steeper alpha curve (goes up to 240 out of 255) for a much darker bottom
        alpha = int(240 * ((y - (height - blur_height)) / blur_height))
        draw.line([(0, y), (width, y)], fill=(0, 0, 0, alpha))
        
    img = Image.alpha_composite(img, overlay)

    # 5. Load Font & Draw Text
    font_file = download_font()
    font = ImageFont.truetype(font_file, 45) # Slightly larger font

    draw = ImageDraw.Draw(img)
    text = f"{GENRE_TEXT}   •   {RATING_TEXT}"
    
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    
    # Position text perfectly in the center of the dark gradient
    x = (width - text_width) / 2
    y = height - (blur_height / 2) - (text_height / 2) + 20 
    
    # Render Text
    draw.text((x+2, y+2), text, font=font, fill=(0, 0, 0, 255)) # Sharp drop shadow
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255)) # Main white text

    # 6. Save the final image
    final_img = img.convert("RGB")
    final_img.save(OUTPUT_FILE, quality=95)
    print(f"Success! Saved custom poster to {OUTPUT_FILE}")

if __name__ == "__main__":
    create_custom_poster()
