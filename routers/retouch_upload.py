from typing import Optional
import os
import secrets
from datetime import datetime
from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse

from core.config import s3, R2_BUCKET
from utils.storage import read_json_key, write_json_key
from core.auth import get_uid_from_request

router = APIRouter(prefix="/api/vaults/retouch", tags=["retouch"])


def _read_retouch_queue(uid: str) -> list:
    """Read retouch queue for a user."""
    try:
        data = read_json_key(f"users/{uid}/retouch_queue.json")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _write_retouch_queue(uid: str, queue: list):
    """Write retouch queue for a user."""
    write_json_key(f"users/{uid}/retouch_queue.json", queue)


def _get_url_for_key(key: str) -> str:
    from core.config import R2_CUSTOM_DOMAIN, s3_presign_client
    if R2_CUSTOM_DOMAIN and s3_presign_client:
        return s3_presign_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET, "Key": key},
            ExpiresIn=3600,
        )
    if s3:
        return s3.meta.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": R2_BUCKET, "Key": key},
            ExpiresIn=3600,
        )
    return ""


@router.post("/upload-result")
async def upload_retouch_result(
    request: Request,
    file: UploadFile = File(...),
    retouch_id: str = Form(...)
):
    """Upload final retouched image for a retouch request."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not retouch_id:
        return JSONResponse({"error": "retouch_id required"}, status_code=400)
    
    try:
        # Find the retouch request
        items = _read_retouch_queue(uid)
        found_item = None
        for it in items:
            if it.get("id") == retouch_id:
                found_item = it
                break
        
        if not found_item:
            return JSONResponse({"error": "Retouch request not found"}, status_code=404)
        
        # Read file
        raw = await file.read()
        if not raw:
            return JSONResponse({"error": "Empty file"}, status_code=400)
        
        # Determine file extension
        orig_filename = file.filename or 'result.jpg'
        ext = os.path.splitext(orig_filename)[1].lower()
        if not ext or ext not in ['.jpg', '.jpeg', '.png', '.webp', '.heic', '.tif', '.tiff']:
            ext = '.jpg'
        
        # Generate unique key for result image
        vault = found_item.get("vault", "")
        ts = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        random_suffix = secrets.token_hex(4)
        key = f"users/{uid}/vaults/{vault}/retouch_results/{ts}_{random_suffix}_result{ext}"
        
        # Upload to R2
        s3.put_object(
            Bucket=R2_BUCKET,
            Key=key,
            Body=raw,
            ContentType=file.content_type or 'image/jpeg',
            ACL='private'
        )
        
        # Update retouch request with result URL
        result_url = _get_url_for_key(key)
        for it in items:
            if it.get("id") == retouch_id:
                it["result_photo_url"] = result_url
                it["result_photo_key"] = key
                it["updated_at"] = datetime.utcnow().isoformat()
                break
        
        _write_retouch_queue(uid, items)
        
        return {
            "ok": True,
            "result_url": result_url,
            "result_key": key
        }
    
    except Exception as ex:
        return JSONResponse({"error": str(ex)}, status_code=500)
