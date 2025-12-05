"""
Imgur Export Router
Allows users to export photos to Imgur (free, no OAuth required for anonymous uploads).
Uses Imgur API v3.
"""
from typing import List, Optional
import os
import httpx
import base64
from datetime import datetime

from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.config import logger
from core.auth import get_uid_from_request
from utils.storage import read_json_key, write_json_key, read_bytes_key, get_presigned_url

router = APIRouter(prefix="/api/imgur", tags=["imgur"])

# Imgur API configuration
IMGUR_CLIENT_ID = os.getenv("IMGUR_CLIENT_ID", "")
IMGUR_API_BASE = "https://api.imgur.com/3"


class ImgurUploadPayload(BaseModel):
    """Payload for uploading to Imgur."""
    keys: List[str]
    album_title: Optional[str] = None
    titles: Optional[List[str]] = None
    descriptions: Optional[List[str]] = None


def _imgur_history_key(uid: str) -> str:
    """Storage key for user's Imgur upload history."""
    return f"users/{uid}/integrations/imgur_history.json"


@router.get("/status")
async def imgur_status(request: Request):
    """Check if Imgur integration is configured."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    configured = bool(IMGUR_CLIENT_ID)
    
    # Get upload history count
    history_count = 0
    try:
        history = read_json_key(_imgur_history_key(uid)) or []
        history_count = len(history) if isinstance(history, list) else 0
    except:
        pass
    
    return {
        "configured": configured,
        "uploads_count": history_count
    }


@router.post("/upload")
async def imgur_upload(request: Request, payload: ImgurUploadPayload):
    """Upload photos to Imgur (anonymous upload, no account needed)."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not IMGUR_CLIENT_ID:
        return JSONResponse({"error": "Imgur integration not configured"}, status_code=500)
    
    if not payload.keys:
        return JSONResponse({"error": "No photos selected"}, status_code=400)
    
    if len(payload.keys) > 50:
        return JSONResponse({"error": "Maximum 50 photos per upload"}, status_code=400)
    
    # Validate keys belong to user
    valid_keys = [k for k in payload.keys if k.startswith(f"users/{uid}/")]
    if not valid_keys:
        return JSONResponse({"error": "No valid photos found"}, status_code=400)
    
    results = []
    errors = []
    album_id = None
    album_deletehash = None
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        # Create album if multiple images and title provided
        if len(valid_keys) > 1 and payload.album_title:
            try:
                album_resp = await client.post(
                    f"{IMGUR_API_BASE}/album",
                    headers={"Authorization": f"Client-ID {IMGUR_CLIENT_ID}"},
                    data={"title": payload.album_title}
                )
                if album_resp.status_code == 200:
                    album_data = album_resp.json().get("data", {})
                    album_id = album_data.get("id")
                    album_deletehash = album_data.get("deletehash")
            except Exception as ex:
                logger.warning(f"Failed to create Imgur album: {ex}")
        
        for i, key in enumerate(valid_keys):
            try:
                # Read image bytes
                image_bytes = read_bytes_key(key)
                if not image_bytes:
                    errors.append({"key": key, "error": "Could not read image"})
                    continue
                
                # Encode as base64
                image_b64 = base64.b64encode(image_bytes).decode('utf-8')
                
                # Get title and description
                filename = key.split("/")[-1]
                name_without_ext = filename.rsplit(".", 1)[0] if "." in filename else filename
                title = payload.titles[i] if payload.titles and i < len(payload.titles) else name_without_ext
                description = payload.descriptions[i] if payload.descriptions and i < len(payload.descriptions) else None
                
                # Upload to Imgur
                upload_data = {
                    "image": image_b64,
                    "type": "base64",
                    "title": title[:128] if title else None,
                }
                if description:
                    upload_data["description"] = description[:1024]
                if album_deletehash:
                    upload_data["album"] = album_deletehash
                
                resp = await client.post(
                    f"{IMGUR_API_BASE}/image",
                    headers={"Authorization": f"Client-ID {IMGUR_CLIENT_ID}"},
                    data={k: v for k, v in upload_data.items() if v is not None}
                )
                
                if resp.status_code == 200:
                    data = resp.json().get("data", {})
                    results.append({
                        "key": key,
                        "imgur_id": data.get("id"),
                        "imgur_link": data.get("link"),
                        "deletehash": data.get("deletehash")
                    })
                else:
                    error_msg = "Upload failed"
                    try:
                        err_data = resp.json()
                        error_msg = err_data.get("data", {}).get("error", error_msg)
                    except:
                        pass
                    errors.append({"key": key, "error": error_msg})
                    
            except Exception as ex:
                errors.append({"key": key, "error": str(ex)})
    
    # Save to history
    try:
        history = read_json_key(_imgur_history_key(uid)) or []
        if not isinstance(history, list):
            history = []
        for r in results:
            history.append({
                **r,
                "uploaded_at": datetime.utcnow().isoformat(),
                "album_id": album_id
            })
        # Keep last 500 uploads
        history = history[-500:]
        write_json_key(_imgur_history_key(uid), history)
    except Exception as ex:
        logger.warning(f"Failed to save Imgur history: {ex}")
    
    return {
        "ok": True,
        "uploaded": len(results),
        "failed": len(errors),
        "results": results,
        "errors": errors,
        "album_id": album_id,
        "album_url": f"https://imgur.com/a/{album_id}" if album_id else None
    }


@router.get("/history")
async def imgur_history(request: Request, limit: int = 50):
    """Get user's Imgur upload history."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        history = read_json_key(_imgur_history_key(uid)) or []
        if not isinstance(history, list):
            history = []
        # Return most recent first
        return {"history": list(reversed(history[-limit:]))}
    except Exception as ex:
        logger.error(f"Failed to get Imgur history: {ex}")
        return {"history": []}


@router.post("/delete")
async def imgur_delete(request: Request, deletehash: str = Body(..., embed=True)):
    """Delete an image from Imgur using its deletehash."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not IMGUR_CLIENT_ID:
        return JSONResponse({"error": "Imgur integration not configured"}, status_code=500)
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.delete(
                f"{IMGUR_API_BASE}/image/{deletehash}",
                headers={"Authorization": f"Client-ID {IMGUR_CLIENT_ID}"}
            )
            
            if resp.status_code == 200:
                # Remove from history
                try:
                    history = read_json_key(_imgur_history_key(uid)) or []
                    history = [h for h in history if h.get("deletehash") != deletehash]
                    write_json_key(_imgur_history_key(uid), history)
                except:
                    pass
                return {"ok": True}
            else:
                return JSONResponse({"error": "Delete failed"}, status_code=500)
    except Exception as ex:
        logger.error(f"Imgur delete failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)
