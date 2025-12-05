"""
Flickr Export Router
Allows users to export photos to Flickr.
Uses Flickr API with OAuth 1.0a.
Free tier: 1000 photos/month.
"""
from typing import List, Optional
import os
import secrets
import hashlib
import hmac
import time
import base64
from urllib.parse import urlencode, quote, parse_qs
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Request, Body, Query
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from core.config import logger
from core.auth import get_uid_from_request
from utils.storage import read_json_key, write_json_key, read_bytes_key

router = APIRouter(prefix="/api/flickr", tags=["flickr"])

# Frontend URL for redirects after OAuth
FRONTEND_URL = os.getenv("FRONTEND_ORIGIN", "https://photomark.cloud").split(",")[0].strip()

# Flickr API configuration
FLICKR_API_KEY = os.getenv("FLICKR_API_KEY", "")
FLICKR_API_SECRET = os.getenv("FLICKR_API_SECRET", "")
FLICKR_REDIRECT_URI = os.getenv("FLICKR_REDIRECT_URI", "")
FLICKR_API_BASE = "https://api.flickr.com/services"


def _flickr_token_key(uid: str) -> str:
    return f"users/{uid}/integrations/flickr.json"


def _flickr_state_key(state: str) -> str:
    return f"oauth/flickr/{state}.json"


def _oauth_signature(method: str, url: str, params: dict, consumer_secret: str, token_secret: str = "") -> str:
    """Generate OAuth 1.0a signature."""
    # Sort and encode parameters
    sorted_params = sorted(params.items())
    param_string = "&".join(f"{quote(str(k), safe='')}={quote(str(v), safe='')}" for k, v in sorted_params)
    
    # Create signature base string
    base_string = f"{method.upper()}&{quote(url, safe='')}&{quote(param_string, safe='')}"
    
    # Create signing key
    signing_key = f"{quote(consumer_secret, safe='')}&{quote(token_secret, safe='')}"
    
    # Generate signature
    signature = hmac.new(
        signing_key.encode('utf-8'),
        base_string.encode('utf-8'),
        hashlib.sha1
    ).digest()
    
    return base64.b64encode(signature).decode('utf-8')


@router.get("/status")
async def flickr_status(request: Request):
    """Check if user has connected their Flickr account."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not FLICKR_API_KEY:
        return {"connected": False, "configured": False}
    
    try:
        token_data = read_json_key(_flickr_token_key(uid))
        if not token_data or not token_data.get("oauth_token"):
            return {"connected": False, "configured": True}
        
        return {
            "connected": True,
            "configured": True,
            "username": token_data.get("username"),
            "user_nsid": token_data.get("user_nsid")
        }
    except Exception as ex:
        logger.warning(f"Flickr status check failed: {ex}")
        return {"connected": False, "configured": True}


@router.get("/auth")
async def flickr_auth(request: Request):
    """Initiate Flickr OAuth flow."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not FLICKR_API_KEY or not FLICKR_API_SECRET:
        return JSONResponse({"error": "Flickr integration not configured"}, status_code=500)
    
    try:
        # Step 1: Get request token
        oauth_params = {
            "oauth_callback": FLICKR_REDIRECT_URI,
            "oauth_consumer_key": FLICKR_API_KEY,
            "oauth_nonce": secrets.token_hex(16),
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": str(int(time.time())),
            "oauth_version": "1.0"
        }
        
        request_token_url = f"{FLICKR_API_BASE}/oauth/request_token"
        oauth_params["oauth_signature"] = _oauth_signature(
            "GET", request_token_url, oauth_params, FLICKR_API_SECRET
        )
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(request_token_url, params=oauth_params)
            
            if resp.status_code != 200:
                logger.error(f"Flickr request token failed: {resp.text}")
                return JSONResponse({"error": "Failed to start OAuth"}, status_code=500)
            
            # Parse response
            token_data = parse_qs(resp.text)
            oauth_token = token_data.get("oauth_token", [""])[0]
            oauth_token_secret = token_data.get("oauth_token_secret", [""])[0]
            
            if not oauth_token:
                return JSONResponse({"error": "Invalid OAuth response"}, status_code=500)
        
        # Store token secret for callback
        state = secrets.token_urlsafe(32)
        write_json_key(_flickr_state_key(state), {
            "uid": uid,
            "oauth_token": oauth_token,
            "oauth_token_secret": oauth_token_secret,
            "created_at": datetime.utcnow().isoformat()
        })
        
        # Build authorization URL
        auth_url = f"https://www.flickr.com/services/oauth/authorize?oauth_token={oauth_token}&perms=write"
        
        return {"auth_url": auth_url, "state": state}
        
    except Exception as ex:
        logger.exception(f"Flickr auth error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.get("/callback")
async def flickr_callback(
    oauth_token: str = Query(None),
    oauth_verifier: str = Query(None)
):
    """Handle Flickr OAuth callback."""
    if not oauth_token or not oauth_verifier:
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?flickr_error=invalid")
    
    # Find the state data by oauth_token
    # Note: In production, you'd want a more robust lookup
    uid = None
    oauth_token_secret = None
    
    # Search for matching state (simplified - in production use a proper lookup)
    try:
        # This is a simplified approach - you'd want to store the oauth_token -> state mapping
        from utils.storage import s3, R2_BUCKET
        if s3 and R2_BUCKET:
            bucket = s3.Bucket(R2_BUCKET)
            for obj in bucket.objects.filter(Prefix="oauth/flickr/"):
                try:
                    state_data = read_json_key(obj.key)
                    if state_data and state_data.get("oauth_token") == oauth_token:
                        uid = state_data.get("uid")
                        oauth_token_secret = state_data.get("oauth_token_secret")
                        break
                except:
                    continue
    except Exception as ex:
        logger.error(f"Flickr callback state lookup failed: {ex}")
    
    if not uid or not oauth_token_secret:
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?flickr_error=invalid_state")
    
    try:
        # Step 3: Exchange for access token
        oauth_params = {
            "oauth_consumer_key": FLICKR_API_KEY,
            "oauth_nonce": secrets.token_hex(16),
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": str(int(time.time())),
            "oauth_token": oauth_token,
            "oauth_verifier": oauth_verifier,
            "oauth_version": "1.0"
        }
        
        access_token_url = f"{FLICKR_API_BASE}/oauth/access_token"
        oauth_params["oauth_signature"] = _oauth_signature(
            "GET", access_token_url, oauth_params, FLICKR_API_SECRET, oauth_token_secret
        )
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(access_token_url, params=oauth_params)
            
            if resp.status_code != 200:
                logger.error(f"Flickr access token failed: {resp.text}")
                return RedirectResponse(url=f"{FRONTEND_URL}/integrations?flickr_error=token_failed")
            
            # Parse response
            token_data = parse_qs(resp.text)
            access_token = token_data.get("oauth_token", [""])[0]
            access_token_secret = token_data.get("oauth_token_secret", [""])[0]
            user_nsid = token_data.get("user_nsid", [""])[0]
            username = token_data.get("username", [""])[0]
        
        # Store tokens
        write_json_key(_flickr_token_key(uid), {
            "oauth_token": access_token,
            "oauth_token_secret": access_token_secret,
            "user_nsid": user_nsid,
            "username": username,
            "connected_at": datetime.utcnow().isoformat()
        })
        
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?flickr_connected=true")
        
    except Exception as ex:
        logger.exception(f"Flickr callback error: {ex}")
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?flickr_error=unknown")


@router.post("/disconnect")
async def flickr_disconnect(request: Request):
    """Disconnect Flickr account."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        write_json_key(_flickr_token_key(uid), {})
        return {"ok": True}
    except Exception as ex:
        logger.error(f"Flickr disconnect failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.get("/albums")
async def flickr_albums(request: Request):
    """Get user's Flickr photosets (albums)."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_flickr_token_key(uid))
    if not token_data or not token_data.get("oauth_token"):
        return JSONResponse({"error": "Flickr not connected"}, status_code=401)
    
    try:
        oauth_params = {
            "method": "flickr.photosets.getList",
            "api_key": FLICKR_API_KEY,
            "user_id": token_data.get("user_nsid"),
            "format": "json",
            "nojsoncallback": "1",
            "oauth_consumer_key": FLICKR_API_KEY,
            "oauth_nonce": secrets.token_hex(16),
            "oauth_signature_method": "HMAC-SHA1",
            "oauth_timestamp": str(int(time.time())),
            "oauth_token": token_data["oauth_token"],
            "oauth_version": "1.0"
        }
        
        url = f"{FLICKR_API_BASE}/rest"
        oauth_params["oauth_signature"] = _oauth_signature(
            "GET", url, oauth_params, FLICKR_API_SECRET, token_data.get("oauth_token_secret", "")
        )
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=oauth_params)
            
            if resp.status_code != 200:
                return JSONResponse({"error": "Failed to fetch albums"}, status_code=500)
            
            data = resp.json()
            photosets = data.get("photosets", {}).get("photoset", [])
            
            albums = []
            for ps in photosets:
                albums.append({
                    "id": ps.get("id"),
                    "title": ps.get("title", {}).get("_content", ""),
                    "description": ps.get("description", {}).get("_content", ""),
                    "photos": ps.get("photos", 0)
                })
            
            return {"albums": albums}
            
    except Exception as ex:
        logger.exception(f"Flickr albums error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/upload")
async def flickr_upload(
    request: Request,
    keys: List[str] = Body(...),
    title_template: Optional[str] = Body(None),
    description: Optional[str] = Body(None),
    tags: Optional[str] = Body(None),
    is_public: bool = Body(True),
    photoset_id: Optional[str] = Body(None)
):
    """Upload photos to Flickr."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_flickr_token_key(uid))
    if not token_data or not token_data.get("oauth_token"):
        return JSONResponse({"error": "Flickr not connected"}, status_code=401)
    
    if not keys:
        return JSONResponse({"error": "No photos selected"}, status_code=400)
    
    if len(keys) > 50:
        return JSONResponse({"error": "Maximum 50 photos per upload"}, status_code=400)
    
    valid_keys = [k for k in keys if k.startswith(f"users/{uid}/")]
    if not valid_keys:
        return JSONResponse({"error": "No valid photos found"}, status_code=400)
    
    results = []
    errors = []
    
    async with httpx.AsyncClient(timeout=120.0) as client:
        for i, key in enumerate(valid_keys):
            try:
                # Read image bytes
                image_bytes = read_bytes_key(key)
                if not image_bytes:
                    errors.append({"key": key, "error": "Could not read image"})
                    continue
                
                # Get title
                filename = key.split("/")[-1]
                name_without_ext = filename.rsplit(".", 1)[0] if "." in filename else filename
                title = name_without_ext
                if title_template:
                    title = title_template.replace("{name}", name_without_ext).replace("{index}", str(i + 1))
                
                # Build OAuth params for upload
                oauth_params = {
                    "oauth_consumer_key": FLICKR_API_KEY,
                    "oauth_nonce": secrets.token_hex(16),
                    "oauth_signature_method": "HMAC-SHA1",
                    "oauth_timestamp": str(int(time.time())),
                    "oauth_token": token_data["oauth_token"],
                    "oauth_version": "1.0"
                }
                
                upload_url = "https://up.flickr.com/services/upload/"
                oauth_params["oauth_signature"] = _oauth_signature(
                    "POST", upload_url, oauth_params, FLICKR_API_SECRET, token_data.get("oauth_token_secret", "")
                )
                
                # Build multipart form
                files = {"photo": (filename, image_bytes, "image/jpeg")}
                data = {
                    "title": title[:255],
                    "is_public": "1" if is_public else "0",
                    "is_friend": "0",
                    "is_family": "0",
                    **oauth_params
                }
                if description:
                    data["description"] = description[:2000]
                if tags:
                    data["tags"] = tags[:255]
                
                resp = await client.post(upload_url, data=data, files=files)
                
                if resp.status_code == 200 and "<photoid>" in resp.text:
                    # Parse photo ID from XML response
                    import re
                    match = re.search(r"<photoid>(\d+)</photoid>", resp.text)
                    if match:
                        photo_id = match.group(1)
                        results.append({
                            "key": key,
                            "photo_id": photo_id,
                            "url": f"https://www.flickr.com/photos/{token_data.get('user_nsid')}/{photo_id}"
                        })
                        
                        # Add to photoset if specified
                        if photoset_id and photo_id:
                            try:
                                add_params = {
                                    "method": "flickr.photosets.addPhoto",
                                    "api_key": FLICKR_API_KEY,
                                    "photoset_id": photoset_id,
                                    "photo_id": photo_id,
                                    "format": "json",
                                    "nojsoncallback": "1",
                                    "oauth_consumer_key": FLICKR_API_KEY,
                                    "oauth_nonce": secrets.token_hex(16),
                                    "oauth_signature_method": "HMAC-SHA1",
                                    "oauth_timestamp": str(int(time.time())),
                                    "oauth_token": token_data["oauth_token"],
                                    "oauth_version": "1.0"
                                }
                                add_url = f"{FLICKR_API_BASE}/rest"
                                add_params["oauth_signature"] = _oauth_signature(
                                    "POST", add_url, add_params, FLICKR_API_SECRET, token_data.get("oauth_token_secret", "")
                                )
                                await client.post(add_url, data=add_params)
                            except:
                                pass
                    else:
                        errors.append({"key": key, "error": "Could not parse response"})
                else:
                    errors.append({"key": key, "error": "Upload failed"})
                    
            except Exception as ex:
                errors.append({"key": key, "error": str(ex)})
    
    return {
        "ok": True,
        "uploaded": len(results),
        "failed": len(errors),
        "results": results,
        "errors": errors
    }
