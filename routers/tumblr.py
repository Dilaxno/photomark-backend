"""
Tumblr Export Router
Allows users to export photos to Tumblr.
Uses Tumblr API v2 with OAuth 2.0.
Free tier with generous limits.
"""
from typing import List, Optional
import os
import secrets
import base64
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request, Body, Query
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from core.config import logger
from core.auth import get_uid_from_request
from utils.storage import read_json_key, write_json_key, read_bytes_key

router = APIRouter(prefix="/api/tumblr", tags=["tumblr"])

# Frontend URL for redirects after OAuth
FRONTEND_URL = os.getenv("FRONTEND_ORIGIN", "https://photomark.cloud").split(",")[0].strip()

# Tumblr API configuration
TUMBLR_CLIENT_ID = os.getenv("TUMBLR_CLIENT_ID", "")
TUMBLR_CLIENT_SECRET = os.getenv("TUMBLR_CLIENT_SECRET", "")
TUMBLR_REDIRECT_URI = os.getenv("TUMBLR_REDIRECT_URI", "")
TUMBLR_API_BASE = "https://api.tumblr.com/v2"
TUMBLR_OAUTH_BASE = "https://www.tumblr.com/oauth2"


def _tumblr_token_key(uid: str) -> str:
    return f"users/{uid}/integrations/tumblr.json"


def _tumblr_state_key(state: str) -> str:
    return f"oauth/tumblr/{state}.json"


@router.get("/status")
async def tumblr_status(request: Request):
    """Check if user has connected their Tumblr account."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not TUMBLR_CLIENT_ID:
        return {"connected": False, "configured": False}
    
    try:
        token_data = read_json_key(_tumblr_token_key(uid))
        if not token_data or not token_data.get("access_token"):
            return {"connected": False, "configured": True}
        
        # Check if token is expired
        expires_at = token_data.get("expires_at")
        if expires_at:
            exp_dt = datetime.fromisoformat(expires_at)
            if datetime.utcnow() > exp_dt:
                # Try to refresh
                refreshed = await _refresh_token(uid, token_data.get("refresh_token"))
                if not refreshed:
                    return {"connected": False, "configured": True, "expired": True}
        
        return {
            "connected": True,
            "configured": True,
            "username": token_data.get("username"),
            "blogs": token_data.get("blogs", [])
        }
    except Exception as ex:
        logger.warning(f"Tumblr status check failed: {ex}")
        return {"connected": False, "configured": True}


@router.get("/auth")
async def tumblr_auth(request: Request):
    """Initiate Tumblr OAuth flow."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not TUMBLR_CLIENT_ID or not TUMBLR_REDIRECT_URI:
        return JSONResponse({"error": "Tumblr integration not configured"}, status_code=500)
    
    state = secrets.token_urlsafe(32)
    
    write_json_key(_tumblr_state_key(state), {
        "uid": uid,
        "created_at": datetime.utcnow().isoformat()
    })
    
    params = {
        "client_id": TUMBLR_CLIENT_ID,
        "redirect_uri": TUMBLR_REDIRECT_URI,
        "response_type": "code",
        "scope": "basic write",
        "state": state
    }
    auth_url = f"{TUMBLR_OAUTH_BASE}/authorize?{urlencode(params)}"
    
    return {"auth_url": auth_url}


@router.get("/callback")
async def tumblr_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None)
):
    """Handle Tumblr OAuth callback."""
    if error:
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?tumblr_error=denied")
    
    if not code or not state:
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?tumblr_error=invalid")
    
    state_data = read_json_key(_tumblr_state_key(state))
    if not state_data or not state_data.get("uid"):
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?tumblr_error=invalid_state")
    
    uid = state_data["uid"]
    
    try:
        async with httpx.AsyncClient() as client:
            # Exchange code for tokens
            token_resp = await client.post(
                f"{TUMBLR_OAUTH_BASE}/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": TUMBLR_REDIRECT_URI,
                    "client_id": TUMBLR_CLIENT_ID,
                    "client_secret": TUMBLR_CLIENT_SECRET
                }
            )
            
            if token_resp.status_code != 200:
                logger.error(f"Tumblr token exchange failed: {token_resp.text}")
                return RedirectResponse(url=f"{FRONTEND_URL}/integrations?tumblr_error=token_failed")
            
            tokens = token_resp.json()
            
            # Get user info
            user_resp = await client.get(
                f"{TUMBLR_API_BASE}/user/info",
                headers={"Authorization": f"Bearer {tokens['access_token']}"}
            )
            
            user_info = {}
            blogs = []
            if user_resp.status_code == 200:
                user_data = user_resp.json().get("response", {}).get("user", {})
                user_info["username"] = user_data.get("name")
                blogs = [{"name": b.get("name"), "url": b.get("url"), "primary": b.get("primary", False)} 
                        for b in user_data.get("blogs", [])]
        
        expires_in = tokens.get("expires_in", 3600)
        expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()
        
        write_json_key(_tumblr_token_key(uid), {
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token"),
            "expires_at": expires_at,
            "blogs": blogs,
            **user_info
        })
        
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?tumblr_connected=true")
        
    except Exception as ex:
        logger.exception(f"Tumblr callback error: {ex}")
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?tumblr_error=unknown")


@router.post("/disconnect")
async def tumblr_disconnect(request: Request):
    """Disconnect Tumblr account."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        write_json_key(_tumblr_token_key(uid), {})
        return {"ok": True}
    except Exception as ex:
        logger.error(f"Tumblr disconnect failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.get("/blogs")
async def tumblr_blogs(request: Request):
    """Get user's Tumblr blogs."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_tumblr_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Tumblr not connected"}, status_code=401)
    
    access_token = await _ensure_valid_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "Tumblr token expired"}, status_code=401)
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{TUMBLR_API_BASE}/user/info",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            
            if resp.status_code != 200:
                return JSONResponse({"error": "Failed to fetch blogs"}, status_code=500)
            
            user_data = resp.json().get("response", {}).get("user", {})
            blogs = [{"name": b.get("name"), "url": b.get("url"), "title": b.get("title"), "primary": b.get("primary", False)} 
                    for b in user_data.get("blogs", [])]
            
            return {"blogs": blogs}
            
    except Exception as ex:
        logger.exception(f"Tumblr blogs error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/upload")
async def tumblr_upload(
    request: Request,
    blog_name: str = Body(...),
    keys: List[str] = Body(...),
    caption: Optional[str] = Body(None),
    tags: Optional[List[str]] = Body(None),
    state: str = Body("published")  # published, draft, queue, private
):
    """Upload photos to Tumblr as a photo post."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_tumblr_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Tumblr not connected"}, status_code=401)
    
    access_token = await _ensure_valid_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "Tumblr token expired"}, status_code=401)
    
    if not keys:
        return JSONResponse({"error": "No photos selected"}, status_code=400)
    
    if len(keys) > 10:
        return JSONResponse({"error": "Maximum 10 photos per post"}, status_code=400)
    
    valid_keys = [k for k in keys if k.startswith(f"users/{uid}/")]
    if not valid_keys:
        return JSONResponse({"error": "No valid photos found"}, status_code=400)
    
    try:
        # Read all images and encode as base64
        images_data = []
        for key in valid_keys:
            image_bytes = read_bytes_key(key)
            if image_bytes:
                images_data.append(base64.b64encode(image_bytes).decode('utf-8'))
        
        if not images_data:
            return JSONResponse({"error": "Could not read any images"}, status_code=400)
        
        # Create photo post
        post_data = {
            "type": "photo",
            "state": state,
            "data64": images_data if len(images_data) > 1 else images_data[0]
        }
        
        if caption:
            post_data["caption"] = caption[:65535]
        if tags:
            post_data["tags"] = ",".join(tags[:20])
        
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{TUMBLR_API_BASE}/blog/{blog_name}/post",
                headers={"Authorization": f"Bearer {access_token}"},
                json=post_data
            )
            
            if resp.status_code in (200, 201):
                data = resp.json().get("response", {})
                post_id = data.get("id") or data.get("id_string")
                return {
                    "ok": True,
                    "post_id": post_id,
                    "post_url": f"https://{blog_name}.tumblr.com/post/{post_id}" if post_id else None
                }
            else:
                error_msg = "Upload failed"
                try:
                    err_data = resp.json()
                    error_msg = err_data.get("meta", {}).get("msg", error_msg)
                except:
                    pass
                return JSONResponse({"error": error_msg}, status_code=500)
                
    except Exception as ex:
        logger.exception(f"Tumblr upload error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


async def _ensure_valid_token(uid: str, token_data: dict) -> Optional[str]:
    """Ensure token is valid, refresh if needed."""
    access_token = token_data.get("access_token")
    expires_at = token_data.get("expires_at")
    
    if expires_at:
        try:
            exp_dt = datetime.fromisoformat(expires_at)
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
    """Refresh Tumblr access token."""
    if not refresh_token or not TUMBLR_CLIENT_ID or not TUMBLR_CLIENT_SECRET:
        return None
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{TUMBLR_OAUTH_BASE}/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": TUMBLR_CLIENT_ID,
                    "client_secret": TUMBLR_CLIENT_SECRET
                }
            )
            
            if resp.status_code != 200:
                return None
            
            tokens = resp.json()
            expires_in = tokens.get("expires_in", 3600)
            expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()
            
            existing = read_json_key(_tumblr_token_key(uid)) or {}
            existing.update({
                "access_token": tokens["access_token"],
                "refresh_token": tokens.get("refresh_token", refresh_token),
                "expires_at": expires_at
            })
            write_json_key(_tumblr_token_key(uid), existing)
            
            return tokens["access_token"]
            
    except Exception as ex:
        logger.exception(f"Tumblr token refresh error: {ex}")
        return None
