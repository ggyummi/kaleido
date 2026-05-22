import os
import requests
from PIL import Image, ImageFilter, ImageDraw, ImageFont
from io import BytesIO

# --- Configuration ---
TMDB_POSTER_URL = "https://image.tmdb.org/t/p/original/jRf89HVEtBZiSnOXXWDhZOfuTwW.jpg" 
GENRE_TEXT = "Thriller"
RATING_TEXT = "★ 7.2"
OUTPUT_FILE = "custom_poster_radial.jpg"
TARGET_WIDTH = 800 # Downscales the image so effects are visible

def download_font():
    """Fetches a clean UI font so GitHub Actions never drops to the 10px default."""
    font_path = "Inter.ttf"
    if not os.path.exists(font_path):
        print("Downloading font...")
        url = "https://github.com/google/fonts/raw/main/ofl/inter/Inter%5Bslnt%2Cwght%5D.ttf"
        response = requests.get(url)
        with open(font_path, "wb") as f:
            f.write(response.content)
    return font_path

def create_custom_poster():
    # 1. Fetch the image from TMDB
    response = requests.get(TMDB_POSTER_URL)
    img = Image.open(BytesIO(response.content)).convert("RGBA")
    
    # 2. Downscale the image to make effects visible
    ratio = TARGET_WIDTH / img.width
    new_height = int(img.height * ratio)
    img = img.resize((TARGET_WIDTH, new_height), Image.Resampling.LANCZOS)
    width, height = img.size

    # 3. Define the bottom region (bottom 25%)
    blur_height = int(height * 0.25) 
    bottom_box = (0, height - blur_height, width, height)
    bottom_region = img.crop(bottom_box)
    
    # 4. Create the fully blurred version of the bottom
    blurred_bottom = bottom_region.filter(ImageFilter.GaussianBlur(radius=25)) 
    
    # 5. Create the Radial Mask
    mask = Image.new('L', bottom_region.size, 255) 
    draw_mask = ImageDraw.Draw(mask)
    
    ellipse_bbox = (-width // 2, -blur_height // 2, width + (width // 2), blur_height * 1.5)
    draw_mask.ellipse(ellipse_bbox, fill=0)
    
    mask = mask.filter(ImageFilter.GaussianBlur(radius=40))
    
    # 6. Blend the two images using the mask
    radial_blended_bottom = Image.composite(blurred_bottom, bottom_region, mask)

    # 7. Paste the final blended segment back onto the main poster
    img.paste(radial_blended_bottom, bottom_box)

    # 8. Add a subtle dark gradient to ensure white text is readable
    overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    
    for y in range(height - blur_height, height):
        alpha = int(180 * ((y - (height - blur_height)) / blur_height))
        draw.line([(0, y), (width, y)], fill=(0, 0, 0, alpha))
        
    img = Image.alpha_composite(img, overlay)

    # 9. Load the downloaded Font & Draw Text
    font_file = download_font()
    font = ImageFont.truetype(font_file, 40) # 40px looks great on an 800px image

    draw = ImageDraw.Draw(img)
    text = f"{GENRE_TEXT}   •   {RATING_TEXT}"
    
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    
    x = (width - text_width) / 2
    y = height - (blur_height / 1.8) - (text_height / 2) 
    
    draw.text((x+3, y+3), text, font=font, fill=(0, 0, 0, 200)) # Drop shadow
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255)) # Main text

    # 10. Save the final image
    final_img = img.convert("RGB")
    final_img.save(OUTPUT_FILE, quality=95)
    print(f"Saved custom poster to {OUTPUT_FILE}")

if __name__ == "__main__":
    create_custom_poster()
