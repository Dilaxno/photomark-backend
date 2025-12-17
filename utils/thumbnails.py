"""
Thumbnail generation utilities for optimized image loading.
Generates small thumbnails (600px) for grid views and medium thumbnails (1200px) for previews.
Higher resolution for retina/HiDPI displays.
"""
import io
from PIL import Image
from typing import Optional, Tuple
from core.config import logger

# Thumbnail sizes - doubled for retina/HiDPI displays
THUMB_SMALL = 600   # For grid views (2x for 300px display)
THUMB_MEDIUM = 1200  # For previews/lightbox (2x for 600px display)

def generate_thumbnail(image_data: bytes, max_size: int = THUMB_SMALL, quality: int = 80) -> Optional[bytes]:
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
        
        # Save as optimized JPEG
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=quality, optimize=True, progressive=True)
        buf.seek(0)
        return buf.getvalue()
    except Exception as ex:
        logger.warning(f"Thumbnail generation failed: {ex}")
        return None


def generate_thumbnails(image_data: bytes) -> Tuple[Optional[bytes], Optional[bytes]]:
    """
    Generate both small and medium thumbnails with high quality.
    
    Args:
        image_data: Original image bytes
    
    Returns:
        Tuple of (small_thumb_bytes, medium_thumb_bytes)
    """
    small = generate_thumbnail(image_data, THUMB_SMALL, quality=90)
    medium = generate_thumbnail(image_data, THUMB_MEDIUM, quality=92)
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
