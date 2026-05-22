import os
import requests
from PIL import Image, ImageFilter, ImageDraw, ImageFont, ImageEnhance
from io import BytesIO

# --- Configuration ---
TMDB_POSTER_URL = "https://image.tmdb.org/t/p/original/jRf89HVEtBZiSnOXXWDhZOfuTwW.jpg" 
GENRE_TEXT = "Thriller"
RATING_TEXT = "★ 7.2"
OUTPUT_FILE = "custom_poster_glass.jpg"
TARGET_WIDTH = 800

def download_font():
    font_path = "DejaVuSans-Bold.ttf"
    if not os.path.exists(font_path):
        print("Downloading font...")
        url = "https://raw.githubusercontent.com/matplotlib/matplotlib/v3.8.0/lib/matplotlib/mpl-data/fonts/ttf/DejaVuSans-Bold.ttf"
        headers = {'User-Agent': 'Mozilla/5.0'}
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            with open(font_path, "wb") as f:
                f.write(response.content)
        else:
            raise Exception("Font download failed.")
    return font_path

def create_custom_poster():
    print("Fetching poster...")
    response = requests.get(TMDB_POSTER_URL, headers={'User-Agent': 'Mozilla/5.0'})
    img = Image.open(BytesIO(response.content)).convert("RGBA")
    
    # Scale image
    ratio = TARGET_WIDTH / img.width
    height = int(img.height * ratio)
    img = img.resize((TARGET_WIDTH, height), Image.Resampling.LANCZOS)
    width = img.width

    # 1. Isolate the exact UI Bar region (Bottom 20%)
    bar_height = int(height * 0.20)
    box = (0, height - bar_height, width, height)
    bottom_region = img.crop(box)

    # 2. Emulate Native UI Glassmorphism
    # Boost saturation aggressively so colors survive the heavy blur
    enhancer = ImageEnhance.Color(bottom_region)
    bottom_region = enhancer.enhance(2.0) 
    
    # Apply a massive blur to destroy the details
    blurred_bar = bottom_region.filter(ImageFilter.GaussianBlur(radius=40))
    
    # Apply a flat, dark UI tint (approx 60% opacity black)
    tint = Image.new('RGBA', blurred_bar.size, (0, 0, 0, 150))
    blurred_bar = Image.alpha_composite(blurred_bar, tint)

    # 3. Feather the top edge of the blurred bar so it's not a harsh pixel cut
    mask = Image.new('L', blurred_bar.size, 255)
    draw_mask = ImageDraw.Draw(mask)
    feather_amount = 35 # Soften the top 35 pixels
    
    for y in range(feather_amount):
        alpha = int(255 * (y / feather_amount))
        draw_mask.line([(0, y), (width, y)], fill=alpha)

    # 4. Paste the final UI bar back onto the main image
    img.paste(blurred_bar, box, mask)

    # 5. Add Text centered perfectly inside the UI bar
    font_file = download_font()
    font = ImageFont.truetype(font_file, 45)
    draw = ImageDraw.Draw(img)
    text = f"{GENRE_TEXT}   •   {RATING_TEXT}"
    
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    
    x = (width - text_width) / 2
    y = height - (bar_height / 2) - (text_height / 2)
    
    # Hard drop shadow for maximum crispness
    draw.text((x+2, y+2), text, font=font, fill=(0, 0, 0, 255)) 
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255)) 

    # 6. Save
    final_img = img.convert("RGB")
    final_img.save(OUTPUT_FILE, quality=95)
    print(f"Done! Saved to {OUTPUT_FILE}")

if __name__ == "__main__":
    create_custom_poster()
