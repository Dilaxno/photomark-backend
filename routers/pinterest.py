"""
Pinterest Export Router
Allows users to export photos to Pinterest either individually or in bulk.
Uses Pinterest API v5 for creating pins.
"""
from typing import List, Optional
import os
import httpx
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Body, Depends, Query
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from core.config import logger
from core.auth import get_uid_from_request
from utils.storage import read_json_key, write_json_key, get_presigned_url

router = APIRouter(prefix="/api/pinterest", tags=["pinterest"])

# Pinterest API configuration
PINTEREST_CLIENT_ID = os.getenv("PINTEREST_CLIENT_ID", "")
PINTEREST_CLIENT_SECRET = os.getenv("PINTEREST_CLIENT_SECRET", "")
PINTEREST_REDIRECT_URI = os.getenv("PINTEREST_REDIRECT_URI", "")
PINTEREST_API_BASE = "https://api.pinterest.com/v5"
PINTEREST_OAUTH_BASE = "https://www.pinterest.com/oauth"

# Scopes needed for creating pins
PINTEREST_SCOPES = "boards:read,boards:write,pins:read,pins:write"


def _pinterest_token_key(uid: str) -> str:
    """Storage key for user's Pinterest tokens."""
    return f"users/{uid}/integrations/pinterest.json"


def _pinterest_state_key(state: str) -> str:
    """Storage key for OAuth state verification."""
    return f"oauth/pinterest/{state}.json"


class PinCreatePayload(BaseModel):
    """Payload for creating a single pin."""
    board_id: str
    image_url: str
    title: Optional[str] = None
    description: Optional[str] = None
    link: Optional[str] = None
    alt_text: Optional[str] = None


class BulkPinPayload(BaseModel):
    """Payload for creating multiple pins."""
    board_id: str
    pins: List[dict]  # Each dict has: image_url, title?, description?, link?, alt_text?


@router.get("/status")
async def pinterest_status(request: Request):
    """Check if user has connected their Pinterest account."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        token_data = read_json_key(_pinterest_token_key(uid))
        if not token_data or not token_data.get("access_token"):
            return {"connected": False}
        
        # Check if token is expired
        expires_at = token_data.get("expires_at")
        if expires_at:
            exp_dt = datetime.fromisoformat(expires_at)
            if datetime.utcnow() > exp_dt:
                # Try to refresh
                refreshed = await _refresh_token(uid, token_data.get("refresh_token"))
                if not refreshed:
                    return {"connected": False, "expired": True}
        
        return {
            "connected": True,
            "username": token_data.get("username"),
            "profile_image": token_data.get("profile_image")
        }
    except Exception as ex:
        logger.warning(f"Pinterest status check failed: {ex}")
        return {"connected": False}


@router.get("/auth")
async def pinterest_auth(request: Request):
    """Initiate Pinterest OAuth flow."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not PINTEREST_CLIENT_ID or not PINTEREST_REDIRECT_URI:
        return JSONResponse({"error": "Pinterest integration not configured"}, status_code=500)
    
    # Generate state for CSRF protection
    state = secrets.token_urlsafe(32)
    
    # Store state with uid for verification
    write_json_key(_pinterest_state_key(state), {
        "uid": uid,
        "created_at": datetime.utcnow().isoformat()
    })
    
    # Build authorization URL
    params = {
        "client_id": PINTEREST_CLIENT_ID,
        "redirect_uri": PINTEREST_REDIRECT_URI,
        "response_type": "code",
        "scope": PINTEREST_SCOPES,
        "state": state
    }
    auth_url = f"{PINTEREST_OAUTH_BASE}/?{urlencode(params)}"
    
    return {"auth_url": auth_url}


@router.get("/callback")
async def pinterest_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None)
):
    """Handle Pinterest OAuth callback."""
    if error:
        logger.warning(f"Pinterest OAuth error: {error}")
        return RedirectResponse(url="/gallery?pinterest_error=denied")
    
    if not code or not state:
        return RedirectResponse(url="/gallery?pinterest_error=invalid")
    
    # Verify state
    state_data = read_json_key(_pinterest_state_key(state))
    if not state_data or not state_data.get("uid"):
        return RedirectResponse(url="/gallery?pinterest_error=invalid_state")
    
    uid = state_data["uid"]
    
    try:
        # Exchange code for tokens
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                f"{PINTEREST_API_BASE}/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": PINTEREST_REDIRECT_URI
                },
                auth=(PINTEREST_CLIENT_ID, PINTEREST_CLIENT_SECRET),
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            
            if token_resp.status_code != 200:
                logger.error(f"Pinterest token exchange failed: {token_resp.text}")
                return RedirectResponse(url="/gallery?pinterest_error=token_failed")
            
            tokens = token_resp.json()
            
            # Get user info
            user_resp = await client.get(
                f"{PINTEREST_API_BASE}/user_account",
                headers={"Authorization": f"Bearer {tokens['access_token']}"}
            )
            
            user_info = {}
            if user_resp.status_code == 200:
                user_data = user_resp.json()
                user_info = {
                    "username": user_data.get("username"),
                    "profile_image": user_data.get("profile_image")
                }
        
        # Calculate expiration
        expires_in = tokens.get("expires_in", 3600)
        expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()
        
        # Store tokens
        write_json_key(_pinterest_token_key(uid), {
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token"),
            "expires_at": expires_at,
            **user_info
        })
        
        return RedirectResponse(url="/gallery?pinterest_connected=true")
        
    except Exception as ex:
        logger.exception(f"Pinterest callback error: {ex}")
        return RedirectResponse(url="/gallery?pinterest_error=unknown")


@router.post("/disconnect")
async def pinterest_disconnect(request: Request):
    """Disconnect Pinterest account."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        # Clear stored tokens
        write_json_key(_pinterest_token_key(uid), {})
        return {"ok": True}
    except Exception as ex:
        logger.error(f"Pinterest disconnect failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.get("/boards")
async def pinterest_boards(request: Request):
    """Get user's Pinterest boards."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_pinterest_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Pinterest not connected"}, status_code=401)
    
    access_token = await _ensure_valid_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "Pinterest token expired"}, status_code=401)
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{PINTEREST_API_BASE}/boards",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"page_size": 100}
            )
            
            if resp.status_code != 200:
                logger.error(f"Pinterest boards fetch failed: {resp.text}")
                return JSONResponse({"error": "Failed to fetch boards"}, status_code=500)
            
            data = resp.json()
            boards = []
            for item in data.get("items", []):
                boards.append({
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "description": item.get("description"),
                    "pin_count": item.get("pin_count", 0),
                    "image_url": item.get("media", {}).get("image_cover_url")
                })
            
            return {"boards": boards}
            
    except Exception as ex:
        logger.exception(f"Pinterest boards error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/boards/create")
async def pinterest_create_board(
    request: Request,
    name: str = Body(..., embed=True),
    description: Optional[str] = Body(None, embed=True),
    privacy: str = Body("PUBLIC", embed=True)
):
    """Create a new Pinterest board."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_pinterest_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Pinterest not connected"}, status_code=401)
    
    access_token = await _ensure_valid_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "Pinterest token expired"}, status_code=401)
    
    try:
        async with httpx.AsyncClient() as client:
            payload = {"name": name, "privacy": privacy}
            if description:
                payload["description"] = description
            
            resp = await client.post(
                f"{PINTEREST_API_BASE}/boards",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json"
                },
                json=payload
            )
            
            if resp.status_code not in (200, 201):
                logger.error(f"Pinterest board create failed: {resp.text}")
                return JSONResponse({"error": "Failed to create board"}, status_code=500)
            
            board = resp.json()
            return {
                "id": board.get("id"),
                "name": board.get("name"),
                "description": board.get("description")
            }
            
    except Exception as ex:
        logger.exception(f"Pinterest board create error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/pin")
async def pinterest_create_pin(request: Request, payload: PinCreatePayload):
    """Create a single pin on Pinterest."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_pinterest_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Pinterest not connected"}, status_code=401)
    
    access_token = await _ensure_valid_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "Pinterest token expired"}, status_code=401)
    
    try:
        pin_data = {
            "board_id": payload.board_id,
            "media_source": {
                "source_type": "image_url",
                "url": payload.image_url
            }
        }
        
        if payload.title:
            pin_data["title"] = payload.title[:100]  # Pinterest limit
        if payload.description:
            pin_data["description"] = payload.description[:500]
        if payload.link:
            pin_data["link"] = payload.link
        if payload.alt_text:
            pin_data["alt_text"] = payload.alt_text[:500]
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{PINTEREST_API_BASE}/pins",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json"
                },
                json=pin_data
            )
            
            if resp.status_code not in (200, 201):
                logger.error(f"Pinterest pin create failed: {resp.text}")
                error_msg = "Failed to create pin"
                try:
                    err_data = resp.json()
                    if err_data.get("message"):
                        error_msg = err_data["message"]
                except:
                    pass
                return JSONResponse({"error": error_msg}, status_code=500)
            
            pin = resp.json()
            return {
                "ok": True,
                "pin_id": pin.get("id"),
                "pin_url": f"https://www.pinterest.com/pin/{pin.get('id')}"
            }
            
    except Exception as ex:
        logger.exception(f"Pinterest pin create error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/pins/bulk")
async def pinterest_bulk_pins(request: Request, payload: BulkPinPayload):
    """Create multiple pins on Pinterest (bulk export)."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_pinterest_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Pinterest not connected"}, status_code=401)
    
    access_token = await _ensure_valid_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "Pinterest token expired"}, status_code=401)
    
    if not payload.pins:
        return JSONResponse({"error": "No pins provided"}, status_code=400)
    
    # Limit bulk operations
    if len(payload.pins) > 50:
        return JSONResponse({"error": "Maximum 50 pins per bulk operation"}, status_code=400)
    
    results = []
    errors = []
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        for i, pin_info in enumerate(payload.pins):
            try:
                image_url = pin_info.get("image_url")
                if not image_url:
                    errors.append({"index": i, "error": "Missing image_url"})
                    continue
                
                pin_data = {
                    "board_id": payload.board_id,
                    "media_source": {
                        "source_type": "image_url",
                        "url": image_url
                    }
                }
                
                if pin_info.get("title"):
                    pin_data["title"] = str(pin_info["title"])[:100]
                if pin_info.get("description"):
                    pin_data["description"] = str(pin_info["description"])[:500]
                if pin_info.get("link"):
                    pin_data["link"] = pin_info["link"]
                if pin_info.get("alt_text"):
                    pin_data["alt_text"] = str(pin_info["alt_text"])[:500]
                
                resp = await client.post(
                    f"{PINTEREST_API_BASE}/pins",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json"
                    },
                    json=pin_data
                )
                
                if resp.status_code in (200, 201):
                    pin = resp.json()
                    results.append({
                        "index": i,
                        "pin_id": pin.get("id"),
                        "pin_url": f"https://www.pinterest.com/pin/{pin.get('id')}"
                    })
                else:
                    error_msg = "Failed"
                    try:
                        err_data = resp.json()
                        error_msg = err_data.get("message", "Failed")
                    except:
                        pass
                    errors.append({"index": i, "error": error_msg})
                    
            except Exception as ex:
                errors.append({"index": i, "error": str(ex)})
    
    return {
        "ok": True,
        "created": len(results),
        "failed": len(errors),
        "results": results,
        "errors": errors
    }


@router.post("/export")
async def pinterest_export_photos(
    request: Request,
    board_id: str = Body(...),
    keys: List[str] = Body(...),
    title_template: Optional[str] = Body(None),
    description: Optional[str] = Body(None),
    link: Optional[str] = Body(None)
):
    """Export photos from storage to Pinterest.
    
    This endpoint takes storage keys, generates public URLs, and creates pins.
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_pinterest_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Pinterest not connected"}, status_code=401)
    
    access_token = await _ensure_valid_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "Pinterest token expired"}, status_code=401)
    
    if not keys:
        return JSONResponse({"error": "No photos selected"}, status_code=400)
    
    if len(keys) > 50:
        return JSONResponse({"error": "Maximum 50 photos per export"}, status_code=400)
    
    # Validate keys belong to user
    valid_keys = [k for k in keys if k.startswith(f"users/{uid}/")]
    if not valid_keys:
        return JSONResponse({"error": "No valid photos found"}, status_code=400)
    
    results = []
    errors = []
    
    async with httpx.AsyncClient(timeout=30.0) as client:
        for i, key in enumerate(valid_keys):
            try:
                # Generate public URL for the image (long expiry for Pinterest to fetch)
                image_url = get_presigned_url(key, expires_in=3600 * 24)  # 24 hours
                if not image_url:
                    errors.append({"key": key, "error": "Could not generate URL"})
                    continue
                
                # Extract filename for title
                filename = key.split("/")[-1]
                name_without_ext = filename.rsplit(".", 1)[0] if "." in filename else filename
                
                # Build title from template or filename
                title = name_without_ext
                if title_template:
                    title = title_template.replace("{name}", name_without_ext).replace("{index}", str(i + 1))
                
                pin_data = {
                    "board_id": board_id,
                    "media_source": {
                        "source_type": "image_url",
                        "url": image_url
                    },
                    "title": title[:100]
                }
                
                if description:
                    pin_data["description"] = description[:500]
                if link:
                    pin_data["link"] = link
                
                resp = await client.post(
                    f"{PINTEREST_API_BASE}/pins",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json"
                    },
                    json=pin_data
                )
                
                if resp.status_code in (200, 201):
                    pin = resp.json()
                    results.append({
                        "key": key,
                        "pin_id": pin.get("id"),
                        "pin_url": f"https://www.pinterest.com/pin/{pin.get('id')}"
                    })
                else:
                    error_msg = "Failed"
                    try:
                        err_data = resp.json()
                        error_msg = err_data.get("message", "Failed")
                    except:
                        pass
                    errors.append({"key": key, "error": error_msg})
                    
            except Exception as ex:
                errors.append({"key": key, "error": str(ex)})
    
    return {
        "ok": True,
        "exported": len(results),
        "failed": len(errors),
        "results": results,
        "errors": errors
    }


async def _ensure_valid_token(uid: str, token_data: dict) -> Optional[str]:
    """Ensure token is valid, refresh if needed."""
    access_token = token_data.get("access_token")
    expires_at = token_data.get("expires_at")
    
    if expires_at:
        try:
            exp_dt = datetime.fromisoformat(expires_at)
            # Refresh if expiring in less than 5 minutes
            if datetime.utcnow() > (exp_dt - timedelta(minutes=5)):
                refresh_token = token_data.get("refresh_token")
                if refresh_token:
                    new_token = await _refresh_token(uid, refresh_token)
                    if new_token:
                        return new_token
                return None
        except:
            pass
    
    return access_token


async def _refresh_token(uid: str, refresh_token: str) -> Optional[str]:
    """Refresh Pinterest access token."""
    if not refresh_token or not PINTEREST_CLIENT_ID or not PINTEREST_CLIENT_SECRET:
        return None
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{PINTEREST_API_BASE}/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token
                },
                auth=(PINTEREST_CLIENT_ID, PINTEREST_CLIENT_SECRET),
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            
            if resp.status_code != 200:
                logger.error(f"Pinterest token refresh failed: {resp.text}")
                return None
            
            tokens = resp.json()
            expires_in = tokens.get("expires_in", 3600)
            expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()
            
            # Update stored tokens
            existing = read_json_key(_pinterest_token_key(uid)) or {}
            existing.update({
                "access_token": tokens["access_token"],
                "refresh_token": tokens.get("refresh_token", refresh_token),
                "expires_at": expires_at
            })
            write_json_key(_pinterest_token_key(uid), existing)
            
            return tokens["access_token"]
            
    except Exception as ex:
        logger.exception(f"Pinterest token refresh error: {ex}")
        return None
