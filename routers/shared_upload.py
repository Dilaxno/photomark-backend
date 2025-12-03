import os
import io
import secrets
from datetime import datetime
from fastapi import APIRouter, UploadFile, File, Form, Depends
from fastapi.responses import JSONResponse
from PIL import Image

from core.config import s3, R2_BUCKET, R2_PUBLIC_BASE_URL, R2_CUSTOM_DOMAIN, s3_presign_client
from utils.storage import presign_custom_domain_bucket
from utils.storage import read_json_key, get_presigned_url
from sqlalchemy.orm import Session
from core.database import get_db
from models.gallery import GalleryAsset

router = APIRouter(prefix="/api/vaults/shared", tags=["shared"])


def _share_key(token: str) -> str:
    """Key for share record."""
    return f"shares/{token}.json"


def _get_url_for_key(key: str, expires_in: int = 3600) -> str:
    return get_presigned_url(key, expires_in=expires_in) or ""


@router.post("/upload-marked-photo")
async def upload_marked_photo(
    file: UploadFile = File(...),
    token: str = Form(...),
    photo_key: str = Form(...),
    db: Session = Depends(get_db)
):
    """Upload a marked-up photo from client (for retouch requests).
    
    The uploaded file can be either:
    1. A transparent PNG overlay with just the annotations (will be composited with original)
    2. A full marked-up image (will be stored as-is)
    """
    token = token.strip()
    photo_key = photo_key.strip()
    
    if not token or not photo_key:
        return JSONResponse({"error": "token and photo_key required"}, status_code=400)
    
    # Validate token
    rec = read_json_key(_share_key(token))
    if not rec:
        return JSONResponse({"error": "invalid token"}, status_code=400)
    
    uid = rec.get('uid') or ''
    vault = rec.get('vault') or ''
    
    if not uid or not vault:
        return JSONResponse({"error": "invalid share"}, status_code=400)
    
    try:
        # Read uploaded file (annotation overlay)
        overlay_raw = await file.read()
        if not overlay_raw:
            return JSONResponse({"error": "Empty file"}, status_code=400)
        
        # Try to composite with original image
        final_image_bytes = overlay_raw
        content_type = 'image/png'
        
        try:
            # Load the overlay image
            overlay_img = Image.open(io.BytesIO(overlay_raw)).convert('RGBA')
            print(f"[markup] Overlay image loaded: {overlay_img.size}, mode={overlay_img.mode}")
            
            # Try to fetch the original image from R2
            try:
                print(f"[markup] Fetching original image from R2: {photo_key}")
                original_obj = s3.get_object(Bucket=R2_BUCKET, Key=photo_key)
                original_raw = original_obj['Body'].read()
                print(f"[markup] Original image fetched: {len(original_raw)} bytes")
                original_img = Image.open(io.BytesIO(original_raw)).convert('RGBA')
                print(f"[markup] Original image loaded: {original_img.size}, mode={original_img.mode}")
                
                # Resize overlay to match original if needed
                if overlay_img.size != original_img.size:
                    print(f"[markup] Resizing overlay from {overlay_img.size} to {original_img.size}")
                    overlay_img = overlay_img.resize(original_img.size, Image.Resampling.LANCZOS)
                
                # Composite: original image + annotation overlay
                composite = Image.alpha_composite(original_img, overlay_img)
                print(f"[markup] Composite created: {composite.size}")
                
                # Convert to RGB for JPEG output (smaller file size)
                composite_rgb = composite.convert('RGB')
                
                # Save to bytes
                output = io.BytesIO()
                composite_rgb.save(output, format='JPEG', quality=90)
                final_image_bytes = output.getvalue()
                content_type = 'image/jpeg'
                print(f"[markup] Final composited image: {len(final_image_bytes)} bytes")
                
            except Exception as e:
                # If we can't fetch original, just save the overlay as-is
                print(f"[markup] ERROR: Could not fetch original image for compositing: {e}")
                import traceback
                traceback.print_exc()
                # Keep overlay_raw as final_image_bytes
                
        except Exception as e:
            # If overlay processing fails, save raw upload
            print(f"[markup] ERROR: Could not process overlay image: {e}")
            import traceback
            traceback.print_exc()
        
        # Generate unique key for marked photo
        ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        random_suffix = secrets.token_hex(4)
        original_name = os.path.splitext(os.path.basename(photo_key))[0]
        safe_name = "".join(c for c in original_name if c.isalnum() or c in ('-', '_'))[:30]
        ext = '.jpg' if content_type == 'image/jpeg' else '.png'
        key = f"users/{uid}/vaults/{vault}/marked/{ts}_{random_suffix}_{safe_name}_marked{ext}"
        
        # Upload to R2
        s3.put_object(
            Bucket=R2_BUCKET,
            Key=key,
            Body=final_image_bytes,
            ContentType=content_type,
            ACL='private',
            CacheControl='public, max-age=604800'
        )
        
        try:
            existing = db.query(GalleryAsset).filter(GalleryAsset.key == key).first()
            if existing:
                existing.user_uid = uid
                existing.vault = vault
                existing.size_bytes = len(final_image_bytes)
            else:
                asset_rec = GalleryAsset(user_uid=uid, vault=vault, key=key, size_bytes=len(final_image_bytes))
                db.add(asset_rec)
            db.commit()
        except Exception:
            try:
                db.rollback()
            except Exception:
                pass
        
        # Generate URL
        url = _get_url_for_key(key)
        
        return {
            "ok": True,
            "marked_photo_url": url,
            "marked_photo_key": key
        }
    
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=500)
