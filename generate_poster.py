import requests
from PIL import Image, ImageFilter, ImageDraw, ImageFont
from io import BytesIO

# --- Configuration ---
TMDB_POSTER_URL = "https://image.tmdb.org/t/p/original/jRf89HVEtBZiSnOXXWDhZOfuTwW.jpg" # Example TMDB poster
GENRE_TEXT = "Thriller"
RATING_TEXT = "★ 7.2"
OUTPUT_FILE = "custom_poster_radial.jpg"

def create_custom_poster():
    # 1. Fetch the image from TMDB
    response = requests.get(TMDB_POSTER_URL)
    img = Image.open(BytesIO(response.content)).convert("RGBA")
    width, height = img.size

    # 2. Define the bottom region (made slightly taller to show off the radial effect)
    blur_height = int(height * 0.25) 
    bottom_box = (0, height - blur_height, width, height)
    bottom_region = img.crop(bottom_box)
    
    # 3. Create the fully blurred version of the bottom
    blurred_bottom = bottom_region.filter(ImageFilter.GaussianBlur(radius=25)) 
    
    # 4. Create the Radial Mask
    # 'L' mode creates an 8-bit grayscale image. 
    # White (255) means "show blur", Black (0) means "show clear".
    mask = Image.new('L', bottom_region.size, 255) 
    draw_mask = ImageDraw.Draw(mask)
    
    # Draw a black ellipse in the upper-center of the bottom region.
    # By making the width wider than the actual image, we get a nice, sweeping arc.
    ellipse_bbox = (-width // 2, -blur_height // 2, width + (width // 2), blur_height * 1.5)
    draw_mask.ellipse(ellipse_bbox, fill=0)
    
    # Blur the mask heavily to create that smooth, gradual fading transition
    mask = mask.filter(ImageFilter.GaussianBlur(radius=50))
    
    # 5. Blend the two images using the mask
    radial_blended_bottom = Image.composite(blurred_bottom, bottom_region, mask)

    # 6. Paste the final blended segment back onto the main poster
    img.paste(radial_blended_bottom, bottom_box)

    # 7. Add a subtle dark gradient to ensure white text is readable
    overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    
    for y in range(height - blur_height, height):
        # Alpha controls darkness. Adjust the '150' multiplier to make it darker/lighter
        alpha = int(150 * ((y - (height - blur_height)) / blur_height))
        draw.line([(0, y), (width, y)], fill=(0, 0, 0, alpha))
        
    img = Image.alpha_composite(img, overlay)

    # 8. Add Text Overlay
    try:
        font = ImageFont.truetype("arial.ttf", 60) 
    except IOError:
        font = ImageFont.load_default()

    draw = ImageDraw.Draw(img)
    text = f"{GENRE_TEXT} • {RATING_TEXT}"
    
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]
    
    x = (width - text_width) / 2
    # Move text slightly higher so it sits naturally in the focal point of the radial blur
    y = height - (blur_height / 1.8) - (text_height / 2) 
    
    draw.text((x+2, y+2), text, font=font, fill=(0, 0, 0, 180)) # Drop shadow
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255)) # Main text

    # 9. Save the final image
    final_img = img.convert("RGB")
    final_img.save(OUTPUT_FILE, quality=95)
    print(f"Saved custom poster to {OUTPUT_FILE}")

if __name__ == "__main__":
    create_custom_poster()
