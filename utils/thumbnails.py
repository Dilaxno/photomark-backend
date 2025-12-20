"""
Thumbnail generation utilities for optimized image loading.
Generates thumbnails matching Cloudinary optimization standards.
Thumbnails are 1200px (600px base × 2.0 DPR) for retina/HiDPI displays.
"""
import io
from PIL import Image
from typing import Optional, Tuple
from core.config import logger

# Thumbnail sizes - matching Cloudinary standards (w_600,dpr_2.0 = 1200px actual)
THUMB_SMALL = 1200   # For grid/gallery views (600px base × 2.0 DPR, matches Cloudinary)
THUMB_MEDIUM = 1600  # For lightbox/full views (800px base × 2.0 DPR)

def generate_thumbnail(image_data: bytes, max_size: int = THUMB_SMALL, quality: int = 95) -> Optional[bytes]:
    """
    Generate a thumbnail from image data.
    
    Args:
        image_data: Original image bytes
        max_size: Maximum dimension (width or height)
        quality: JPEG quality (1-100)
    
    Returns:
        Thumbnail bytes or None if failed
    """
    try:
        img = Image.open(io.BytesIO(image_data))
        
        # Convert to RGB if necessary (handles RGBA, P mode, etc.)
        if img.mode in ('RGBA', 'P', 'LA'):
            # Create white background for transparency
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            background.paste(img, mask=img.split()[-1] if img.mode == 'RGBA' else None)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        
        # Calculate new dimensions maintaining aspect ratio
        width, height = img.size
        if width <= max_size and height <= max_size:
            # Image is already small enough, just optimize it
            pass
        elif width > height:
            new_width = max_size
            new_height = int(height * (max_size / width))
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        else:
            new_height = max_size
            new_width = int(width * (max_size / height))
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        
        # Apply subtle sharpening for better quality (only if resized)
        if width > max_size or height > max_size:
            from PIL import ImageFilter
            img = img.filter(ImageFilter.UnsharpMask(radius=0.5, percent=50, threshold=2))
        
        # Save as optimized JPEG with highest quality settings (matching Cloudinary q_auto:best)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=quality, optimize=True, progressive=True, 
                 subsampling=0, qtables='web_high')
        buf.seek(0)
        return buf.getvalue()
    except Exception as ex:
        logger.warning(f"Thumbnail generation failed: {ex}")
        return None


def generate_thumbnails(image_data: bytes) -> Tuple[Optional[bytes], Optional[bytes]]:
    """
    Generate both small and medium thumbnails with highest quality.
    Matches Cloudinary q_auto:best quality standards.
    
    Args:
        image_data: Original image bytes
    
    Returns:
        Tuple of (small_thumb_bytes, medium_thumb_bytes)
    """
    small = generate_thumbnail(image_data, THUMB_SMALL, quality=98)
    medium = generate_thumbnail(image_data, THUMB_MEDIUM, quality=98)
    return small, medium


def get_thumbnail_key(original_key: str, size: str = 'small') -> str:
    """
    Generate the storage key for a thumbnail based on the original key.
    
    Args:
        original_key: Original image storage key
        size: 'small' or 'medium'
    
    Returns:
        Thumbnail storage key
    """
    # Insert _thumb_small or _thumb_medium before the extension
    import os
    base, ext = os.path.splitext(original_key)
    return f"{base}_thumb_{size}.jpg"
