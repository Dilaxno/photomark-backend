"""
Unsplash Export Router
Allows users to publish their best photos to Unsplash's community.
Uses Unsplash API with OAuth 2.0.
Free tier: 50 requests/hour (demo), unlimited for production apps.
Docs: https://unsplash.com/developers
"""
from typing import List, Optional
import os
import secrets
from datetime import datetime
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request, Body, Query
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from core.config import logger
from core.auth import get_uid_from_request
from utils.storage import read_json_key, write_json_key, read_bytes_key

router = APIRouter(prefix="/api/unsplash", tags=["unsplash"])

# Frontend URL for redirects after OAuth
FRONTEND_URL = os.getenv("FRONTEND_ORIGIN", "https://photomark.cloud").split(",")[0].strip()

# Unsplash API configuration
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "")
UNSPLASH_SECRET_KEY = os.getenv("UNSPLASH_SECRET_KEY", "")
UNSPLASH_REDIRECT_URI = os.getenv("UNSPLASH_REDIRECT_URI", "")
UNSPLASH_API_BASE = "https://api.unsplash.com"
UNSPLASH_AUTH_URL = "https://unsplash.com/oauth/authorize"
UNSPLASH_TOKEN_URL = "https://unsplash.com/oauth/token"


def _unsplash_token_key(uid: str) -> str:
    """Storage key for user's Unsplash tokens."""
    return f"users/{uid}/integrations/unsplash.json"


def _unsplash_state_key(state: str) -> str:
    """Storage key for OAuth state."""
    return f"oauth/unsplash/{state}.json"


def _unsplash_history_key(uid: str) -> str:
    """Storage key for user's Unsplash upload history."""
    return f"users/{uid}/integrations/unsplash_history.json"


@router.get("/status")
async def unsplash_status(request: Request):
    """Check if user has connected their Unsplash account."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not UNSPLASH_ACCESS_KEY:
        return {"connected": False, "configured": False}
    
    try:
        token_data = read_json_key(_unsplash_token_key(uid))
        if not token_data or not token_data.get("access_token"):
            return {"connected": False, "configured": True}
        
        # Get upload history count
        history_count = 0
        try:
            history = read_json_key(_unsplash_history_key(uid)) or []
            history_count = len(history) if isinstance(history, list) else 0
        except:
            pass
        
        return {
            "connected": True,
            "configured": True,
            "username": token_data.get("username"),
            "uploads_count": history_count
        }
    except Exception as ex:
        logger.warning(f"Unsplash status check failed: {ex}")
        return {"connected": False, "configured": True}


@router.get("/auth")
async def unsplash_auth(request: Request):
    """Initiate Unsplash OAuth flow."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not UNSPLASH_ACCESS_KEY or not UNSPLASH_SECRET_KEY:
        return JSONResponse({"error": "Unsplash integration not configured"}, status_code=500)
    
    try:
        # Generate state for CSRF protection
        state = secrets.token_urlsafe(32)
        
        # Store state with user ID
        write_json_key(_unsplash_state_key(state), {
            "uid": uid,
            "created_at": datetime.utcnow().isoformat()
        })
        
        # Build authorization URL
        # Scopes: public (read public data), write_photos (upload photos)
        params = {
            "client_id": UNSPLASH_ACCESS_KEY,
            "redirect_uri": UNSPLASH_REDIRECT_URI,
            "response_type": "code",
            "scope": "public+write_photos",
            "state": state
        }
        
        auth_url = f"{UNSPLASH_AUTH_URL}?{urlencode(params)}"
        
        return {"auth_url": auth_url, "state": state}
        
    except Exception as ex:
        logger.exception(f"Unsplash auth error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.get("/callback")
async def unsplash_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None)
):
    """Handle Unsplash OAuth callback."""
    if error:
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?unsplash_error=denied")
    
    if not code or not state:
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?unsplash_error=invalid")
    
    # Verify state and get user ID
    try:
        state_data = read_json_key(_unsplash_state_key(state))
        if not state_data:
            return RedirectResponse(url=f"{FRONTEND_URL}/integrations?unsplash_error=invalid_state")
        
        uid = state_data.get("uid")
        if not uid:
            return RedirectResponse(url=f"{FRONTEND_URL}/integrations?unsplash_error=invalid_state")
    except Exception as ex:
        logger.error(f"Unsplash callback state lookup failed: {ex}")
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?unsplash_error=invalid_state")
    
    try:
        # Exchange code for access token
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                UNSPLASH_TOKEN_URL,
                data={
                    "client_id": UNSPLASH_ACCESS_KEY,
                    "client_secret": UNSPLASH_SECRET_KEY,
                    "redirect_uri": UNSPLASH_REDIRECT_URI,
                    "code": code,
                    "grant_type": "authorization_code"
                }
            )
            
            if resp.status_code != 200:
                logger.error(f"Unsplash token exchange failed: {resp.text}")
                return RedirectResponse(url=f"{FRONTEND_URL}/integrations?unsplash_error=token_failed")
            
            token_data = resp.json()
            access_token = token_data.get("access_token")
            
            if not access_token:
                return RedirectResponse(url=f"{FRONTEND_URL}/integrations?unsplash_error=token_failed")
            
            # Get user profile
            profile_resp = await client.get(
                f"{UNSPLASH_API_BASE}/me",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            
            username = None
            profile_url = None
            if profile_resp.status_code == 200:
                profile = profile_resp.json()
                username = profile.get("username")
                profile_url = profile.get("links", {}).get("html")
        
        # Store tokens
        write_json_key(_unsplash_token_key(uid), {
            "access_token": access_token,
            "token_type": token_data.get("token_type", "Bearer"),
            "scope": token_data.get("scope"),
            "username": username,
            "profile_url": profile_url,
            "connected_at": datetime.utcnow().isoformat()
        })
        
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?unsplash_connected=true")
        
    except Exception as ex:
        logger.exception(f"Unsplash callback error: {ex}")
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?unsplash_error=unknown")


@router.post("/disconnect")
async def unsplash_disconnect(request: Request):
    """Disconnect Unsplash account."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        write_json_key(_unsplash_token_key(uid), {})
        return {"ok": True}
    except Exception as ex:
        logger.error(f"Unsplash disconnect failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)



@router.get("/profile")
async def unsplash_profile(request: Request):
    """Get user's Unsplash profile info."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_unsplash_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Unsplash not connected"}, status_code=401)
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{UNSPLASH_API_BASE}/me",
                headers={"Authorization": f"Bearer {token_data['access_token']}"}
            )
            
            if resp.status_code == 401:
                # Token expired
                return JSONResponse({"error": "Token expired", "expired": True}, status_code=401)
            
            if resp.status_code != 200:
                return JSONResponse({"error": "Failed to fetch profile"}, status_code=500)
            
            profile = resp.json()
            
            return {
                "username": profile.get("username"),
                "name": profile.get("name"),
                "bio": profile.get("bio"),
                "profile_url": profile.get("links", {}).get("html"),
                "portfolio_url": profile.get("portfolio_url"),
                "total_photos": profile.get("total_photos", 0),
                "total_likes": profile.get("total_likes", 0),
                "total_downloads": profile.get("downloads", 0),
                "profile_image": profile.get("profile_image", {}).get("medium")
            }
            
    except Exception as ex:
        logger.exception(f"Unsplash profile error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/upload")
async def unsplash_upload(
    request: Request,
    keys: List[str] = Body(...),
    description: Optional[str] = Body(None),
    location: Optional[str] = Body(None),
    tags: Optional[str] = Body(None)
):
    """
    Upload photos to Unsplash.
    
    Note: Unsplash has strict quality guidelines. Photos must be:
    - High resolution (minimum 5MP recommended)
    - Original work by the uploader
    - Not contain watermarks, borders, or signatures
    - Properly exposed and in focus
    
    Unsplash may reject photos that don't meet their quality standards.
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_unsplash_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Unsplash not connected"}, status_code=401)
    
    if not keys:
        return JSONResponse({"error": "No photos selected"}, status_code=400)
    
    # Unsplash recommends uploading one photo at a time for quality review
    if len(keys) > 10:
        return JSONResponse({"error": "Maximum 10 photos per upload batch"}, status_code=400)
    
    valid_keys = [k for k in keys if k.startswith(f"users/{uid}/")]
    if not valid_keys:
        return JSONResponse({"error": "No valid photos found"}, status_code=400)
    
    results = []
    errors = []
    access_token = token_data["access_token"]
    
    async with httpx.AsyncClient(timeout=120.0) as client:
        for key in valid_keys:
            try:
                # Read image bytes
                image_bytes = read_bytes_key(key)
                if not image_bytes:
                    errors.append({"key": key, "error": "Could not read image"})
                    continue
                
                # Check minimum file size (rough quality check)
                if len(image_bytes) < 100000:  # ~100KB minimum
                    errors.append({"key": key, "error": "Image too small for Unsplash (min ~100KB)"})
                    continue
                
                # Get filename for title
                filename = key.split("/")[-1]
                name_without_ext = filename.rsplit(".", 1)[0] if "." in filename else filename
                
                # Determine content type
                content_type = "image/jpeg"
                if filename.lower().endswith(".png"):
                    content_type = "image/png"
                elif filename.lower().endswith(".webp"):
                    content_type = "image/webp"
                
                # Upload to Unsplash
                # Unsplash uses multipart form upload
                files = {"photo": (filename, image_bytes, content_type)}
                data = {}
                
                if description:
                    data["description"] = description[:500]
                if location:
                    data["location[name]"] = location[:100]
                if tags:
                    # Unsplash accepts comma-separated tags
                    data["tags"] = tags[:200]
                
                resp = await client.post(
                    f"{UNSPLASH_API_BASE}/photos",
                    headers={"Authorization": f"Bearer {access_token}"},
                    files=files,
                    data=data if data else None
                )
                
                if resp.status_code in (200, 201):
                    photo_data = resp.json()
                    results.append({
                        "key": key,
                        "unsplash_id": photo_data.get("id"),
                        "url": photo_data.get("links", {}).get("html"),
                        "download_url": photo_data.get("links", {}).get("download")
                    })
                elif resp.status_code == 401:
                    errors.append({"key": key, "error": "Token expired"})
                    break
                elif resp.status_code == 403:
                    errors.append({"key": key, "error": "Upload permission denied"})
                elif resp.status_code == 422:
                    # Validation error - photo doesn't meet quality standards
                    try:
                        err_data = resp.json()
                        err_msg = err_data.get("errors", ["Quality standards not met"])[0]
                    except:
                        err_msg = "Photo doesn't meet Unsplash quality standards"
                    errors.append({"key": key, "error": err_msg})
                else:
                    error_msg = "Upload failed"
                    try:
                        err_data = resp.json()
                        error_msg = err_data.get("errors", [error_msg])[0]
                    except:
                        pass
                    errors.append({"key": key, "error": error_msg})
                    
            except Exception as ex:
                errors.append({"key": key, "error": str(ex)})
    
    # Save to history
    try:
        history = read_json_key(_unsplash_history_key(uid)) or []
        if not isinstance(history, list):
            history = []
        for r in results:
            history.append({
                **r,
                "uploaded_at": datetime.utcnow().isoformat()
            })
        # Keep last 500 uploads
        history = history[-500:]
        write_json_key(_unsplash_history_key(uid), history)
    except Exception as ex:
        logger.warning(f"Failed to save Unsplash history: {ex}")
    
    return {
        "ok": True,
        "uploaded": len(results),
        "failed": len(errors),
        "results": results,
        "errors": errors
    }


@router.get("/history")
async def unsplash_history(request: Request, limit: int = 50):
    """Get user's Unsplash upload history."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        history = read_json_key(_unsplash_history_key(uid)) or []
        if not isinstance(history, list):
            history = []
        # Return most recent first
        return {"history": list(reversed(history[-limit:]))}
    except Exception as ex:
        logger.error(f"Failed to get Unsplash history: {ex}")
        return {"history": []}


@router.get("/photos")
async def unsplash_photos(request: Request, page: int = 1, per_page: int = 20):
    """Get user's photos on Unsplash."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_unsplash_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Unsplash not connected"}, status_code=401)
    
    try:
        username = token_data.get("username")
        if not username:
            return JSONResponse({"error": "Username not found"}, status_code=400)
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{UNSPLASH_API_BASE}/users/{username}/photos",
                headers={"Authorization": f"Bearer {token_data['access_token']}"},
                params={"page": page, "per_page": min(per_page, 30)}
            )
            
            if resp.status_code != 200:
                return JSONResponse({"error": "Failed to fetch photos"}, status_code=500)
            
            photos = resp.json()
            
            return {
                "photos": [
                    {
                        "id": p.get("id"),
                        "url": p.get("links", {}).get("html"),
                        "thumb": p.get("urls", {}).get("thumb"),
                        "small": p.get("urls", {}).get("small"),
                        "description": p.get("description"),
                        "likes": p.get("likes", 0),
                        "downloads": p.get("downloads", 0),
                        "created_at": p.get("created_at")
                    }
                    for p in photos
                ]
            }
            
    except Exception as ex:
        logger.exception(f"Unsplash photos error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)
