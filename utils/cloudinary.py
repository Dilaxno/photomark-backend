"""
Cloudinary URL optimization utilities
Following the project's Cloudinary optimization standards
"""
from typing import Optional


def get_cloudinary_thumbnail_url(url: str) -> Optional[str]:
    """
    Generate Cloudinary thumbnail URL for photos following optimization standards.
    
    For photo thumbnails: f_auto,q_auto:best,w_600,dpr_2.0
    - f_auto → WebP/AVIF (30-50% smaller files)
    - q_auto:best → High quality with smart compression
    - w_600 → Thumbnail width for gallery/vault previews
    - dpr_2.0 → 2x for retina displays
    """
    try:
        if not url or 'cloudinary.com' not in url:
            return None
        
        # For photo thumbnails: f_auto,q_auto:best,w_600,dpr_2.0
        return url.replace('/upload/', '/upload/f_auto,q_auto:best,w_600,dpr_2.0/')
    except Exception:
        return None


def get_cloudinary_optimized_url(url: str, width: int = 1200) -> str:
    """
    Generate optimized Cloudinary URL for full photos.
    
    For full photos: f_auto,q_auto:best,w_[WIDTH],dpr_2.0
    - f_auto → WebP/AVIF (30-50% smaller files)
    - q_auto:best → High quality with smart compression
    - w_[WIDTH] → Exact width needed
    - dpr_2.0 → 2x for retina displays
    """
    try:
        if not url or 'cloudinary.com' not in url:
            return url
        
        # For full photos: f_auto,q_auto:best,w_[WIDTH],dpr_2.0
        return url.replace('/upload/', f'/upload/f_auto,q_auto:best,w_{width},dpr_2.0/')
    except Exception:
        return url


def get_cloudinary_screenshot_url(url: str) -> str:
    """
    Generate Cloudinary URL for screenshots with maximum quality.
    
    For screenshots/UI: f_png,q_100
    - f_png → Keeps lossless PNG format (no compression artifacts)
    - q_100 → Maximum quality, no lossy compression
    """
    try:
        if not url or 'cloudinary.com' not in url:
            return url
        
        # For screenshots: f_png,q_100
        return url.replace('/upload/', '/upload/f_png,q_100/')
    except Exception:
        return url