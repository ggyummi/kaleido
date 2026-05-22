import requests
from PIL import Image, ImageFilter, ImageDraw, ImageFont
from io import BytesIO

# --- Configuration ---
TMDB_POSTER_URL = "https://image.tmdb.org/t/p/original/jRf89HVEtBZiSnOXXWDhZOfuTwW.jpg"
GENRE_TEXT = "Thriller"
RATING_TEXT = "★ 7.2"
OUTPUT_FILE = "custom_poster.jpg"

def create_custom_poster():
    # 1. Fetch the image from TMDB
    response = requests.get(TMDB_POSTER_URL)
    img = Image.open(BytesIO(response.content)).convert("RGBA")
    width, height = img.size

    # 2. Define the bottom blur region (e.g., bottom 20% of the poster)
    blur_height = int(height * 0.20)
    bottom_box = (0, height - blur_height, width, height)
    
    # 3. Crop, blur, and paste back
    bottom_region = img.crop(bottom_box)
    # Radius controls the intensity of the blur
    blurred_bottom = bottom_region.filter(ImageFilter.GaussianBlur(radius=25)) 
    img.paste(blurred_bottom, bottom_box)

    # 4. Add a dark gradient over the blurred area to ensure text readability
    overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    
    for y in range(height - blur_height, height):
        # Calculate alpha for gradient (0 at top of blur, darker at bottom)
        alpha = int(200 * ((y - (height - blur_height)) / blur_height))
        draw.line([(0, y), (width, y)], fill=(0, 0, 0, alpha))
        
    img = Image.alpha_composite(img, overlay)

    # 5. Add Text Overlay
    # You will need to provide a path to a .ttf file in your repository
    try:
        font = ImageFont.truetype("arial.ttf", 60) 
    except IOError:
        font = ImageFont.load_default()

    draw = ImageDraw.Draw(img)
    text = f"{GENRE_TEXT} • {RATING_TEXT}"
    
    # Center text in the blurred region
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    
    x = (width - text_width) / 2
    y = height - (blur_height / 2) - (text_height / 2)
    
    # Draw text with a slight shadow for extra pop
    draw.text((x+2, y+2), text, font=font, fill=(0, 0, 0, 180))
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255))

    # 6. Save the final image
    final_img = img.convert("RGB")
    final_img.save(OUTPUT_FILE, quality=95)
    print(f"Saved custom poster to {OUTPUT_FILE}")

if __name__ == "__main__":
    create_custom_poster()
