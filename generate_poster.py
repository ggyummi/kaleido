import os
import requests
from PIL import Image, ImageFilter, ImageDraw, ImageFont
from io import BytesIO

# --- Configuration ---
TMDB_POSTER_URL = "https://image.tmdb.org/t/p/original/jRf89HVEtBZiSnOXXWDhZOfuTwW.jpg" 
GENRE_TEXT = "Thriller"
RATING_TEXT = "★ 7.2"
OUTPUT_FILE = "custom_poster_seamless.jpg"
TARGET_WIDTH = 800

def download_font():
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
    print("Fetching poster from TMDB...")
    headers = {'User-Agent': 'Mozilla/5.0'}
    response = requests.get(TMDB_POSTER_URL, headers=headers)
    if response.status_code != 200:
        raise Exception(f"Failed to download image. TMDB returned: {response.status_code}")
        
    # 1. Open and resize the base image
    img = Image.open(BytesIO(response.content)).convert("RGBA")
    ratio = TARGET_WIDTH / img.width
    height = int(img.height * ratio)
    img = img.resize((TARGET_WIDTH, height), Image.Resampling.LANCZOS)
    width = img.width

    # 2. Create a fully blurred copy of the entire image
    blurred_img = img.filter(ImageFilter.GaussianBlur(radius=20))

    # 3. Create a Vertical Gradient Mask for a seamless blur transition
    # 'L' mode creates an 8-bit grayscale image. 0 = Keep Sharp, 255 = Show Blur.
    mask = Image.new('L', img.size, 0)
    draw_mask = ImageDraw.Draw(mask)
    
    # Start the blur transition at the bottom 40% of the poster
    blur_start_y = int(height * 0.60)
    
    for y in range(blur_start_y, height):
        # Calculate progress from 0.0 to 1.0
        progress = (y - blur_start_y) / (height - blur_start_y)
        # Apply a curve so the blur starts softly and ramps up
        alpha = int(255 * (progress ** 1.5)) 
        draw_mask.line([(0, y), (width, y)], fill=alpha)

    # 4. Seamlessly merge the sharp image and blurred image using the mask
    img = Image.composite(blurred_img, img, mask)

    # 5. Add the Dark Gradient Overlay (Black fading smoothly up)
    overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
    draw_overlay = ImageDraw.Draw(overlay)
    
    # Start the dark shadow lower down (bottom 25%) so it doesn't cover too much
    dark_start_y = int(height * 0.75)
    
    for y in range(dark_start_y, height):
        progress = (y - dark_start_y) / (height - dark_start_y)
        # Curve the darkness so it blends beautifully into the background
        alpha = int(230 * (progress ** 1.2))
        draw_overlay.line([(0, y), (width, y)], fill=(0, 0, 0, alpha))
        
    img = Image.alpha_composite(img, overlay)

    # 6. Load Font & Draw Text
    font_file = download_font()
    font = ImageFont.truetype(font_file, 45)

    draw = ImageDraw.Draw(img)
    text = f"{GENRE_TEXT}   •   {RATING_TEXT}"
    
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    
    # Position text perfectly
    x = (width - text_width) / 2
    # Drop the text a bit lower to match your reference image
    y = height - text_height - 60 
    
    draw.text((x+3, y+3), text, font=font, fill=(0, 0, 0, 220)) # Drop shadow
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255)) # Main white text

    # 7. Save the final image
    final_img = img.convert("RGB")
    final_img.save(OUTPUT_FILE, quality=95)
    print(f"Success! Saved seamless custom poster to {OUTPUT_FILE}")

if __name__ == "__main__":
    create_custom_poster()
