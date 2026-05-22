import os
import requests
from PIL import Image, ImageFilter, ImageDraw, ImageFont
from io import BytesIO

# --- Configuration ---
TMDB_POSTER_URL = "https://image.tmdb.org/t/p/original/jRf89HVEtBZiSnOXXWDhZOfuTwW.jpg" 
GENRE_TEXT = "Thriller"
RATING_TEXT = "★ 7.2"
OUTPUT_FILE = "custom_poster_perfect.jpg"
TARGET_WIDTH = 800

def download_font():
    font_path = "DejaVuSans-Bold.ttf"
    if not os.path.exists(font_path):
        print("Downloading bold font...")
        url = "https://raw.githubusercontent.com/matplotlib/matplotlib/v3.8.0/lib/matplotlib/mpl-data/fonts/ttf/DejaVuSans-Bold.ttf"
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
    print("Fetching poster from TMDB...")
    headers = {'User-Agent': 'Mozilla/5.0'}
    response = requests.get(TMDB_POSTER_URL, headers=headers)
    if response.status_code != 200:
        raise Exception(f"Failed to download image. TMDB returned: {response.status_code}")
        
    img = Image.open(BytesIO(response.content)).convert("RGBA")
    ratio = TARGET_WIDTH / img.width
    height = int(img.height * ratio)
    img = img.resize((TARGET_WIDTH, height), Image.Resampling.LANCZOS)
    width = img.width

    # 1. Intense Blur: Cranked up to 45 to totally smudge the bottom characters
    blurred_img = img.filter(ImageFilter.GaussianBlur(radius=45))

    # 2. Smooth Vertical Mask (Bottom 25%)
    mask = Image.new('L', img.size, 0)
    draw_mask = ImageDraw.Draw(mask)
    blur_zone_height = int(height * 0.25)
    blur_start_y = height - blur_zone_height
    
    for y in range(blur_start_y, height):
        progress = (y - blur_start_y) / blur_zone_height
        alpha = int(255 * progress) 
        draw_mask.line([(0, y), (width, y)], fill=alpha)

    img = Image.composite(blurred_img, img, mask)

    # 3. Deep Gradient Overlay: Restoring the darkness for high text contrast
    overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
    draw_overlay = ImageDraw.Draw(overlay)
    
    dark_start_y = blur_start_y + int(blur_zone_height * 0.1)
    dark_zone_height = height - dark_start_y
    
    for y in range(dark_start_y, height):
        progress = (y - dark_start_y) / dark_zone_height
        # Ramps up to a very dark 220 (out of 255) at the bottom edge
        alpha = int(220 * (progress ** 1.1))
        draw_overlay.line([(0, y), (width, y)], fill=(0, 0, 0, alpha))
        
    img = Image.alpha_composite(img, overlay)

    # 4. Load Font & Draw Text
    font_file = download_font()
    font = ImageFont.truetype(font_file, 42)

    draw = ImageDraw.Draw(img)
    text = f"{GENRE_TEXT}   •   {RATING_TEXT}"
    
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    
    # 5. Position text in the lower, darkest section of the blur
    x = (width - text_width) / 2
    y = height - (blur_zone_height * 0.5) - (text_height / 2) + 15
    
    draw.text((x+3, y+3), text, font=font, fill=(0, 0, 0, 255)) 
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255)) 

    # 6. Save the final image
    final_img = img.convert("RGB")
    final_img.save(OUTPUT_FILE, quality=95)
    print(f"Success! Saved perfect custom poster to {OUTPUT_FILE}")

if __name__ == "__main__":
    create_custom_poster()
