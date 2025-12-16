"""
Thumbnail generation API endpoints.
Handles on-demand thumbnail generation for existing images.
"""
from fastapi import APIRouter, Request, BackgroundTasks
from fastapi.responses import JSONResponse
from typing import Optional
import os
from datetime import datetime

from core.config import s3, R2_BUCKET, s3_backup, BACKUP_BUCKET, logger
from core.auth import resolve_workspace_uid, has_role_access
from utils.thumbnails import generate_thumbnail, get_thumbnail_key, THUMB_SMALL
from utils.storage import read_bytes_key, backup_read_bytes_key, get_presigned_url

router = APIRouter(prefix="/api", tags=["thumbnails"])

# Image extensions to process
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.heic', '.tif', '.tiff'}

# Prefixes to scan for images
SCAN_PREFIXES = [
    "watermarked/",
    "external/",
    "vaults/",
    "portfolio/",
    "gallery/",
    "photos/",
    "partners/",  # Team chat / collaboration photos
]


def _is_image_key(key: str) -> bool:
    """Check if a key is an image file."""
    lower = key.lower()
    return any(lower.endswith(ext) for ext in IMAGE_EXTENSIONS)


def _generate_thumbnail_for_key(key: str, bucket, read_func) -> bool:
    """Generate and upload a thumbnail for a single key."""
    try:
        thumb_key = get_thumbnail_key(key, 'small')
        
        # Check if thumbnail already exists
        try:
            bucket.Object(thumb_key).load()
            return False  # Already exists
        except Exception:
            pass  # Doesn't exist, continue
        
        # Read original image
        data = read_func(key)
        if not data:
            return False
        
        # Generate thumbnail
        thumb_data = generate_thumbnail(data, THUMB_SMALL, quality=75)
        if not thumb_data:
            return False
        
        # Upload thumbnail
        bucket.put_object(
            Key=thumb_key,
            Body=thumb_data,
            ContentType='image/jpeg',
            ACL='private',
            CacheControl='public, max-age=31536000'
        )
        logger.info(f"Generated thumbnail: {thumb_key}")
        return True
    except Exception as ex:
        logger.warning(f"Thumbnail generation failed for {key}: {ex}")
        return False


def _generate_thumbnails_for_user(uid: str, bucket, read_func, limit: int = 50) -> dict:
    """Generate missing thumbnails for a user's images."""
    generated = 0
    skipped = 0
    errors = 0
    
    for prefix_suffix in SCAN_PREFIXES:
        if generated >= limit:
            break
            
        prefix = f"users/{uid}/{prefix_suffix}"
        try:
            for obj in bucket.objects.filter(Prefix=prefix):
                if generated >= limit:
                    break
                    
                key = obj.key
                
                # Skip non-images and existing thumbnails
                if not _is_image_key(key) or '_thumb_' in key:
                    continue
                
                # Try to generate thumbnail
                if _generate_thumbnail_for_key(key, bucket, read_func):
                    generated += 1
                else:
                    skipped += 1
        except Exception as ex:
            logger.warning(f"Error scanning {prefix}: {ex}")
            errors += 1
    
    return {"generated": generated, "skipped": skipped, "errors": errors}


def _background_generate_thumbnails(uid: str, source: str = "r2"):
    """Background task to generate thumbnails."""
    try:
        if source == "backup" and s3_backup and BACKUP_BUCKET:
            bucket = s3_backup.Bucket(BACKUP_BUCKET)
            read_func = backup_read_bytes_key
        elif s3 and R2_BUCKET:
            bucket = s3.Bucket(R2_BUCKET)
            read_func = read_bytes_key
        else:
            return
        
        result = _generate_thumbnails_for_user(uid, bucket, read_func, limit=100)
        logger.info(f"Background thumbnail generation for {uid}: {result}")
    except Exception as ex:
        logger.error(f"Background thumbnail generation failed for {uid}: {ex}")


@router.post("/thumbnails/generate")
async def generate_thumbnails(
    request: Request,
    background_tasks: BackgroundTasks,
    limit: int = 50,
    source: str = "r2"
):
    """
    Generate missing thumbnails for the current user's images.
    This runs in the background and returns immediately.
    
    Args:
        limit: Maximum thumbnails to generate per request (default 50)
        source: "r2" for primary storage, "backup" for B2 backup
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    
    uid = eff_uid
    
    # Add background task
    background_tasks.add_task(_background_generate_thumbnails, uid, source)
    
    return {"ok": True, "message": "Thumbnail generation started in background"}


@router.get("/thumbnails/status")
async def get_thumbnail_status(request: Request, source: str = "r2"):
    """
    Get thumbnail generation status for the current user.
    Returns count of images with and without thumbnails.
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    
    uid = eff_uid
    
    # Select bucket
    if source == "backup" and s3_backup and BACKUP_BUCKET:
        bucket = s3_backup.Bucket(BACKUP_BUCKET)
    elif s3 and R2_BUCKET:
        bucket = s3.Bucket(R2_BUCKET)
    else:
        return JSONResponse({"error": "Storage unavailable"}, status_code=503)
    
    total_images = 0
    with_thumbnails = 0
    without_thumbnails = 0
    
    try:
        for prefix_suffix in SCAN_PREFIXES:
            prefix = f"users/{uid}/{prefix_suffix}"
            for obj in bucket.objects.filter(Prefix=prefix):
                key = obj.key
                
                if not _is_image_key(key) or '_thumb_' in key:
                    continue
                
                total_images += 1
                
                # Check if thumbnail exists
                thumb_key = get_thumbnail_key(key, 'small')
                try:
                    bucket.Object(thumb_key).load()
                    with_thumbnails += 1
                except Exception:
                    without_thumbnails += 1
                
                # Limit scan to avoid timeout
                if total_images >= 500:
                    break
            
            if total_images >= 500:
                break
    except Exception as ex:
        logger.warning(f"Error checking thumbnail status: {ex}")
    
    return {
        "total_images": total_images,
        "with_thumbnails": with_thumbnails,
        "without_thumbnails": without_thumbnails,
        "coverage_percent": round((with_thumbnails / total_images * 100) if total_images > 0 else 100, 1)
    }


@router.get("/thumbnail/{key:path}")
async def get_or_generate_thumbnail(request: Request, key: str, background_tasks: BackgroundTasks):
    """
    Get thumbnail URL for an image, generating it on-demand if it doesn't exist.
    This is the key endpoint for automatic thumbnail generation.
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    uid = eff_uid
    
    # Validate key belongs to user
    if not key.startswith(f"users/{uid}/"):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    
    if not s3 or not R2_BUCKET:
        return JSONResponse({"error": "Storage unavailable"}, status_code=503)
    
    bucket = s3.Bucket(R2_BUCKET)
    thumb_key = get_thumbnail_key(key, 'small')
    
    # Check if thumbnail exists
    try:
        bucket.Object(thumb_key).load()
        # Thumbnail exists, return URL
        url = get_presigned_url(thumb_key, expires_in=3600)
        return {"thumb_url": url, "generated": False}
    except Exception:
        pass  # Doesn't exist
    
    # Generate thumbnail on-demand
    try:
        data = read_bytes_key(key)
        if not data:
            return JSONResponse({"error": "Image not found"}, status_code=404)
        
        thumb_data = generate_thumbnail(data, THUMB_SMALL, quality=75)
        if not thumb_data:
            return JSONResponse({"error": "Could not generate thumbnail"}, status_code=500)
        
        # Upload thumbnail
        bucket.put_object(
            Key=thumb_key,
            Body=thumb_data,
            ContentType='image/jpeg',
            ACL='private',
            CacheControl='public, max-age=31536000'
        )
        
        url = get_presigned_url(thumb_key, expires_in=3600)
        return {"thumb_url": url, "generated": True}
    except Exception as ex:
        logger.error(f"On-demand thumbnail generation failed for {key}: {ex}")
        return JSONResponse({"error": "Thumbnail generation failed"}, status_code=500)
