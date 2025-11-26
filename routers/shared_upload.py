import os
import secrets
from datetime import datetime
from fastapi import APIRouter, UploadFile, File, Form
from fastapi.responses import JSONResponse

from core.config import s3, R2_BUCKET, R2_PUBLIC_BASE_URL, R2_CUSTOM_DOMAIN, s3_presign_client
from utils.storage import read_json_key

router = APIRouter(prefix="/api/vaults/shared", tags=["shared"])


def _share_key(token: str) -> str:
    """Key for share record."""
    return f"shares/{token}.json"


def _get_url_for_key(key: str, expires_in: int = 3600) -> str:
    if R2_CUSTOM_DOMAIN and s3_presign_client:
        return s3_presign_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET, "Key": key},
            ExpiresIn=expires_in,
        )
    if s3:
        return s3.meta.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET, "Key": key},
            ExpiresIn=expires_in,
        )
    return ""


@router.post("/upload-marked-photo")
async def upload_marked_photo(
    file: UploadFile = File(...),
    token: str = Form(...),
    photo_key: str = Form(...)
):
    """Upload a marked-up photo from client (for retouch requests)."""
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
        # Read file
        raw = await file.read()
        if not raw:
            return JSONResponse({"error": "Empty file"}, status_code=400)
        
        # Determine file extension
        orig_filename = file.filename or 'marked.png'
        ext = os.path.splitext(orig_filename)[1].lower()
        if not ext or ext not in ['.jpg', '.jpeg', '.png', '.webp']:
            ext = '.png'
        
        # Generate unique key for marked photo
        ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        random_suffix = secrets.token_hex(4)
        # Extract original filename without extension
        original_name = os.path.splitext(os.path.basename(photo_key))[0]
        safe_name = "".join(c for c in original_name if c.isalnum() or c in ('-', '_'))[:30]
        key = f"users/{uid}/vaults/{vault}/marked/{ts}_{random_suffix}_{safe_name}_marked{ext}"
        
        # Upload to R2
        s3.put_object(
            Bucket=R2_BUCKET,
            Key=key,
            Body=raw,
            ContentType=file.content_type or 'image/png',
            ACL='private'
        )
        
        # Generate URL
        url = _get_url_for_key(key)
        
        return {
            "ok": True,
            "marked_photo_url": url,
            "marked_photo_key": key
        }
    
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=500)
