"""
IPTC/EXIF Metadata Router

Endpoints for managing and embedding copyright/contact metadata in photos.
"""

from fastapi import APIRouter, Request, Body, UploadFile, File, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from typing import Optional
import io
from PIL import Image

from core.auth import get_uid_from_request, resolve_workspace_uid, has_role_access
from core.config import logger
from utils.storage import read_json_key, write_json_key, read_bytes_key, upload_bytes
from utils.metadata import MetadataSettings, embed_metadata, embed_metadata_to_bytes, read_metadata

router = APIRouter(prefix="/api/metadata", tags=["metadata"])


def _settings_key(uid: str) -> str:
    """Storage key for user's metadata settings."""
    return f"users/{uid}/settings/metadata.json"


@router.get("/settings")
async def get_metadata_settings(request: Request):
    """
    Get the user's saved metadata settings (photographer name, copyright, contact info).
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        data = read_json_key(_settings_key(eff_uid)) or {}
        return {
            "settings": {
                "photographer_name": data.get("photographer_name", ""),
                "copyright_notice": data.get("copyright_notice", ""),
                "contact_email": data.get("contact_email", ""),
                "contact_phone": data.get("contact_phone", ""),
                "contact_website": data.get("contact_website", ""),
                "business_name": data.get("business_name", ""),
                "address": data.get("address", ""),
                "city": data.get("city", ""),
                "country": data.get("country", ""),
                "auto_embed": data.get("auto_embed", False),
            }
        }
    except Exception as e:
        logger.warning(f"Failed to get metadata settings for {eff_uid}: {e}")
        return {"settings": {}}


@router.post("/settings")
async def save_metadata_settings(request: Request, payload: dict = Body(...)):
    """
    Save the user's metadata settings.
    
    Body: {
        photographer_name?: string,
        copyright_notice?: string,
        contact_email?: string,
        contact_phone?: string,
        contact_website?: string,
        business_name?: string,
        address?: string,
        city?: string,
        country?: string,
        auto_embed?: boolean
    }
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        settings = {
            "photographer_name": str(payload.get("photographer_name") or "").strip(),
            "copyright_notice": str(payload.get("copyright_notice") or "").strip(),
            "contact_email": str(payload.get("contact_email") or "").strip(),
            "contact_phone": str(payload.get("contact_phone") or "").strip(),
            "contact_website": str(payload.get("contact_website") or "").strip(),
            "business_name": str(payload.get("business_name") or "").strip(),
            "address": str(payload.get("address") or "").strip(),
            "city": str(payload.get("city") or "").strip(),
            "country": str(payload.get("country") or "").strip(),
            "auto_embed": bool(payload.get("auto_embed", False)),
        }
        
        write_json_key(_settings_key(eff_uid), settings)
        return {"ok": True, "settings": settings}
    
    except Exception as e:
        logger.exception(f"Failed to save metadata settings for {eff_uid}: {e}")
        return JSONResponse({"error": "Failed to save settings"}, status_code=500)


@router.post("/embed")
async def embed_metadata_endpoint(
    request: Request,
    file: UploadFile = File(...),
):
    """
    Embed metadata into a single image and return the result.
    Uses the user's saved metadata settings.
    
    Returns: JPEG image with embedded metadata
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    
    try:
        # Load user's metadata settings
        data = read_json_key(_settings_key(eff_uid)) or {}
        settings = MetadataSettings.from_dict(data)
        
        # Read and process image
        raw = await file.read()
        if not raw:
            return JSONResponse({"error": "Empty file"}, status_code=400)
        
        result_bytes = embed_metadata_to_bytes(raw, settings)
        
        # Return processed image
        filename = file.filename or "image.jpg"
        if not filename.lower().endswith('.jpg'):
            filename = filename.rsplit('.', 1)[0] + '.jpg'
        
        return StreamingResponse(
            io.BytesIO(result_bytes),
            media_type="image/jpeg",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )
    
    except Exception as e:
        logger.exception(f"Failed to embed metadata: {e}")
        return JSONResponse({"error": "Failed to process image"}, status_code=500)


@router.post("/embed/custom")
async def embed_metadata_custom(
    request: Request,
    file: UploadFile = File(...),
    photographer_name: Optional[str] = None,
    copyright_notice: Optional[str] = None,
    contact_email: Optional[str] = None,
    contact_website: Optional[str] = None,
):
    """
    Embed custom metadata into a single image (override saved settings).
    
    Returns: JPEG image with embedded metadata
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    
    try:
        # Build custom settings
        settings = MetadataSettings(
            photographer_name=photographer_name,
            copyright_notice=copyright_notice,
            contact_email=contact_email,
            contact_website=contact_website,
        )
        
        # Read and process image
        raw = await file.read()
        if not raw:
            return JSONResponse({"error": "Empty file"}, status_code=400)
        
        result_bytes = embed_metadata_to_bytes(raw, settings)
        
        # Return processed image
        filename = file.filename or "image.jpg"
        if not filename.lower().endswith('.jpg'):
            filename = filename.rsplit('.', 1)[0] + '.jpg'
        
        return StreamingResponse(
            io.BytesIO(result_bytes),
            media_type="image/jpeg",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'}
        )
    
    except Exception as e:
        logger.exception(f"Failed to embed custom metadata: {e}")
        return JSONResponse({"error": "Failed to process image"}, status_code=500)


@router.post("/read")
async def read_metadata_endpoint(
    request: Request,
    file: UploadFile = File(...),
):
    """
    Read EXIF metadata from an image.
    
    Returns: { metadata: { artist, copyright, description, software } }
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        raw = await file.read()
        if not raw:
            return JSONResponse({"error": "Empty file"}, status_code=400)
        
        metadata = read_metadata(raw)
        return {"metadata": metadata}
    
    except Exception as e:
        logger.exception(f"Failed to read metadata: {e}")
        return JSONResponse({"error": "Failed to read metadata"}, status_code=500)


@router.post("/embed/gallery/{photo_key:path}")
async def embed_metadata_gallery_photo(
    request: Request,
    photo_key: str,
):
    """
    Embed metadata into an existing gallery photo (in-place update).
    
    Path param: photo_key - the storage key of the photo
    Returns: { ok: true, key: string }
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    
    # Validate key belongs to user
    allowed_prefixes = (
        f"users/{eff_uid}/watermarked/",
        f"users/{eff_uid}/external/",
        f"users/{eff_uid}/originals/",
    )
    if not photo_key.startswith(allowed_prefixes):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    
    try:
        # Load user's metadata settings
        data = read_json_key(_settings_key(eff_uid)) or {}
        settings = MetadataSettings.from_dict(data)
        
        # Read existing photo
        raw = read_bytes_key(photo_key)
        if not raw:
            return JSONResponse({"error": "Photo not found"}, status_code=404)
        
        # Embed metadata
        result_bytes = embed_metadata_to_bytes(raw, settings)
        
        # Upload back to same key
        upload_bytes(photo_key, result_bytes, content_type="image/jpeg")
        
        return {"ok": True, "key": photo_key}
    
    except Exception as e:
        logger.exception(f"Failed to embed metadata in gallery photo: {e}")
        return JSONResponse({"error": "Failed to process photo"}, status_code=500)
