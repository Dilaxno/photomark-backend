"""
Webflow Integration Router
Allows users to push photos to Webflow CMS for building portfolio websites.
Uses Webflow API v2 with OAuth 2.0.
Free tier: 2 projects, 50 CMS items.
Docs: https://developers.webflow.com/
"""
from typing import List, Optional
import os
import secrets
import base64
from datetime import datetime
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request, Body, Query
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from core.config import logger
from core.auth import get_uid_from_request
from utils.storage import read_json_key, write_json_key, read_bytes_key, get_presigned_url

router = APIRouter(prefix="/api/webflow", tags=["webflow"])

# Frontend URL for redirects after OAuth
FRONTEND_URL = os.getenv("FRONTEND_ORIGIN", "https://photomark.cloud").split(",")[0].strip()

# Webflow API configuration
WEBFLOW_CLIENT_ID = os.getenv("WEBFLOW_CLIENT_ID", "")
WEBFLOW_CLIENT_SECRET = os.getenv("WEBFLOW_CLIENT_SECRET", "")
WEBFLOW_REDIRECT_URI = os.getenv("WEBFLOW_REDIRECT_URI", "")
WEBFLOW_API_BASE = "https://api.webflow.com/v2"
WEBFLOW_AUTH_URL = "https://webflow.com/oauth/authorize"
WEBFLOW_TOKEN_URL = "https://api.webflow.com/oauth/access_token"


def _webflow_token_key(uid: str) -> str:
    """Storage key for user's Webflow tokens."""
    return f"users/{uid}/integrations/webflow.json"


def _webflow_state_key(state: str) -> str:
    """Storage key for OAuth state."""
    return f"oauth/webflow/{state}.json"


def _webflow_history_key(uid: str) -> str:
    """Storage key for user's Webflow upload history."""
    return f"users/{uid}/integrations/webflow_history.json"


@router.get("/status")
async def webflow_status(request: Request):
    """Check if user has connected their Webflow account."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not WEBFLOW_CLIENT_ID:
        return {"connected": False, "configured": False}
    
    try:
        token_data = read_json_key(_webflow_token_key(uid))
        if not token_data or not token_data.get("access_token"):
            return {"connected": False, "configured": True}
        
        # Get upload history count
        history_count = 0
        try:
            history = read_json_key(_webflow_history_key(uid)) or []
            history_count = len(history) if isinstance(history, list) else 0
        except:
            pass
        
        return {
            "connected": True,
            "configured": True,
            "user_id": token_data.get("user_id"),
            "email": token_data.get("email"),
            "uploads_count": history_count
        }
    except Exception as ex:
        logger.warning(f"Webflow status check failed: {ex}")
        return {"connected": False, "configured": True}


@router.get("/auth")
async def webflow_auth(request: Request):
    """Initiate Webflow OAuth flow."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not WEBFLOW_CLIENT_ID or not WEBFLOW_CLIENT_SECRET:
        return JSONResponse({"error": "Webflow integration not configured"}, status_code=500)
    
    try:
        # Generate state for CSRF protection
        state = secrets.token_urlsafe(32)
        
        # Store state with user ID
        write_json_key(_webflow_state_key(state), {
            "uid": uid,
            "created_at": datetime.utcnow().isoformat()
        })
        
        # Build authorization URL
        # Scopes: sites:read, cms:read, cms:write
        params = {
            "client_id": WEBFLOW_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": WEBFLOW_REDIRECT_URI,
            "scope": "sites:read cms:read cms:write assets:read assets:write",
            "state": state
        }
        
        auth_url = f"{WEBFLOW_AUTH_URL}?{urlencode(params, safe='')}"
        
        return {"auth_url": auth_url, "state": state}
        
    except Exception as ex:
        logger.exception(f"Webflow auth error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.get("/callback")
async def webflow_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None)
):
    """Handle Webflow OAuth callback."""
    if error:
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?webflow_error=denied")
    
    if not code or not state:
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?webflow_error=invalid")
    
    # Verify state and get user ID
    try:
        state_data = read_json_key(_webflow_state_key(state))
        if not state_data:
            return RedirectResponse(url=f"{FRONTEND_URL}/integrations?webflow_error=invalid_state")
        
        uid = state_data.get("uid")
        if not uid:
            return RedirectResponse(url=f"{FRONTEND_URL}/integrations?webflow_error=invalid_state")
    except Exception as ex:
        logger.error(f"Webflow callback state lookup failed: {ex}")
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?webflow_error=invalid_state")
    
    try:
        # Exchange code for access token
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                WEBFLOW_TOKEN_URL,
                data={
                    "client_id": WEBFLOW_CLIENT_ID,
                    "client_secret": WEBFLOW_CLIENT_SECRET,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": WEBFLOW_REDIRECT_URI
                }
            )
            
            if resp.status_code != 200:
                logger.error(f"Webflow token exchange failed: {resp.text}")
                return RedirectResponse(url=f"{FRONTEND_URL}/integrations?webflow_error=token_failed")
            
            token_data = resp.json()
            access_token = token_data.get("access_token")
            
            if not access_token:
                return RedirectResponse(url=f"{FRONTEND_URL}/integrations?webflow_error=token_failed")
            
            # Get user info
            user_resp = await client.get(
                f"{WEBFLOW_API_BASE}/user",
                headers={"Authorization": f"Bearer {access_token}"}
            )
            
            user_id = None
            email = None
            if user_resp.status_code == 200:
                user_info = user_resp.json()
                user_id = user_info.get("id")
                email = user_info.get("email")
        
        # Store tokens
        write_json_key(_webflow_token_key(uid), {
            "access_token": access_token,
            "token_type": token_data.get("token_type", "Bearer"),
            "user_id": user_id,
            "email": email,
            "connected_at": datetime.utcnow().isoformat()
        })
        
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?webflow_connected=true")
        
    except Exception as ex:
        logger.exception(f"Webflow callback error: {ex}")
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?webflow_error=unknown")


@router.post("/disconnect")
async def webflow_disconnect(request: Request):
    """Disconnect Webflow account."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        write_json_key(_webflow_token_key(uid), {})
        return {"ok": True}
    except Exception as ex:
        logger.error(f"Webflow disconnect failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.get("/sites")
async def webflow_sites(request: Request):
    """Get user's Webflow sites."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_webflow_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Webflow not connected"}, status_code=401)
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{WEBFLOW_API_BASE}/sites",
                headers={"Authorization": f"Bearer {token_data['access_token']}"}
            )
            
            if resp.status_code == 401:
                return JSONResponse({"error": "Token expired", "expired": True}, status_code=401)
            
            if resp.status_code != 200:
                return JSONResponse({"error": "Failed to fetch sites"}, status_code=500)
            
            data = resp.json()
            sites = data.get("sites", [])
            
            return {
                "sites": [
                    {
                        "id": s.get("id"),
                        "displayName": s.get("displayName"),
                        "shortName": s.get("shortName"),
                        "previewUrl": s.get("previewUrl"),
                        "createdOn": s.get("createdOn"),
                        "lastPublished": s.get("lastPublished")
                    }
                    for s in sites
                ]
            }
            
    except Exception as ex:
        logger.exception(f"Webflow sites error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.get("/sites/{site_id}/collections")
async def webflow_collections(request: Request, site_id: str):
    """Get CMS collections for a Webflow site."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_webflow_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Webflow not connected"}, status_code=401)
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{WEBFLOW_API_BASE}/sites/{site_id}/collections",
                headers={"Authorization": f"Bearer {token_data['access_token']}"}
            )
            
            if resp.status_code == 401:
                return JSONResponse({"error": "Token expired", "expired": True}, status_code=401)
            
            if resp.status_code != 200:
                return JSONResponse({"error": "Failed to fetch collections"}, status_code=500)
            
            data = resp.json()
            collections = data.get("collections", [])
            
            return {
                "collections": [
                    {
                        "id": c.get("id"),
                        "displayName": c.get("displayName"),
                        "singularName": c.get("singularName"),
                        "slug": c.get("slug"),
                        "createdOn": c.get("createdOn"),
                        "lastUpdated": c.get("lastUpdated")
                    }
                    for c in collections
                ]
            }
            
    except Exception as ex:
        logger.exception(f"Webflow collections error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.get("/collections/{collection_id}/fields")
async def webflow_collection_fields(request: Request, collection_id: str):
    """Get fields for a CMS collection."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_webflow_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Webflow not connected"}, status_code=401)
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{WEBFLOW_API_BASE}/collections/{collection_id}",
                headers={"Authorization": f"Bearer {token_data['access_token']}"}
            )
            
            if resp.status_code == 401:
                return JSONResponse({"error": "Token expired", "expired": True}, status_code=401)
            
            if resp.status_code != 200:
                return JSONResponse({"error": "Failed to fetch collection"}, status_code=500)
            
            collection = resp.json()
            fields = collection.get("fields", [])
            
            return {
                "collection": {
                    "id": collection.get("id"),
                    "displayName": collection.get("displayName"),
                    "singularName": collection.get("singularName"),
                    "slug": collection.get("slug")
                },
                "fields": [
                    {
                        "id": f.get("id"),
                        "slug": f.get("slug"),
                        "displayName": f.get("displayName"),
                        "type": f.get("type"),
                        "isRequired": f.get("isRequired", False),
                        "validations": f.get("validations")
                    }
                    for f in fields
                ]
            }
            
    except Exception as ex:
        logger.exception(f"Webflow collection fields error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)



@router.post("/upload")
async def webflow_upload(
    request: Request,
    site_id: str = Body(...),
    collection_id: str = Body(...),
    keys: List[str] = Body(...),
    name_field: str = Body("name"),
    image_field: str = Body("image"),
    slug_field: str = Body("slug"),
    description_field: Optional[str] = Body(None),
    is_draft: bool = Body(False)
):
    """
    Upload photos to Webflow CMS collection.
    
    Creates CMS items with the photo as an image field.
    Photos are first uploaded to Webflow's asset CDN, then linked to CMS items.
    
    Args:
        site_id: Webflow site ID
        collection_id: CMS collection ID to add items to
        keys: List of photo keys from user's gallery
        name_field: Field slug for the item name (default: "name")
        image_field: Field slug for the image (default: "image")
        slug_field: Field slug for the URL slug (default: "slug")
        description_field: Optional field slug for description
        is_draft: If True, items are created as drafts (default: False)
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_webflow_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Webflow not connected"}, status_code=401)
    
    if not keys:
        return JSONResponse({"error": "No photos selected"}, status_code=400)
    
    # Webflow free tier has 50 CMS items limit
    if len(keys) > 25:
        return JSONResponse({"error": "Maximum 25 photos per upload batch"}, status_code=400)
    
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
                
                # Get filename
                filename = key.split("/")[-1]
                name_without_ext = filename.rsplit(".", 1)[0] if "." in filename else filename
                # Clean up name for display
                display_name = name_without_ext.replace("-", " ").replace("_", " ").title()
                
                # Generate slug from filename
                slug = name_without_ext.lower().replace(" ", "-").replace("_", "-")
                # Remove any non-alphanumeric characters except hyphens
                slug = "".join(c for c in slug if c.isalnum() or c == "-")
                slug = slug[:100]  # Webflow slug limit
                
                # Determine content type
                content_type = "image/jpeg"
                if filename.lower().endswith(".png"):
                    content_type = "image/png"
                elif filename.lower().endswith(".webp"):
                    content_type = "image/webp"
                elif filename.lower().endswith(".gif"):
                    content_type = "image/gif"
                
                # Step 1: Upload image to Webflow assets
                # First, get a presigned upload URL
                asset_resp = await client.post(
                    f"{WEBFLOW_API_BASE}/sites/{site_id}/assets",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "fileName": filename,
                        "fileHash": secrets.token_hex(16)  # Unique hash for the file
                    }
                )
                
                if asset_resp.status_code not in (200, 201):
                    error_msg = "Failed to create asset upload"
                    try:
                        err_data = asset_resp.json()
                        error_msg = err_data.get("message", error_msg)
                    except:
                        pass
                    errors.append({"key": key, "error": error_msg})
                    continue
                
                asset_data = asset_resp.json()
                upload_url = asset_data.get("uploadUrl")
                upload_details = asset_data.get("uploadDetails", {})
                asset_id = asset_data.get("id")
                
                if not upload_url:
                    # Alternative: Use direct asset upload
                    # Upload directly to Webflow's asset endpoint
                    files = {"file": (filename, image_bytes, content_type)}
                    direct_resp = await client.post(
                        f"{WEBFLOW_API_BASE}/sites/{site_id}/assets",
                        headers={"Authorization": f"Bearer {access_token}"},
                        files=files
                    )
                    
                    if direct_resp.status_code not in (200, 201):
                        errors.append({"key": key, "error": "Failed to upload asset"})
                        continue
                    
                    asset_data = direct_resp.json()
                    asset_id = asset_data.get("id")
                    asset_url = asset_data.get("url") or asset_data.get("hostedUrl")
                else:
                    # Upload to presigned URL
                    # Build multipart form data as required by Webflow
                    form_data = {}
                    for k, v in upload_details.items():
                        form_data[k] = v
                    
                    upload_resp = await client.post(
                        upload_url,
                        data=form_data,
                        files={"file": (filename, image_bytes, content_type)}
                    )
                    
                    if upload_resp.status_code not in (200, 201, 204):
                        errors.append({"key": key, "error": "Failed to upload to CDN"})
                        continue
                    
                    asset_url = asset_data.get("url") or asset_data.get("hostedUrl")
                
                # Step 2: Create CMS item with the uploaded image
                field_data = {
                    name_field: display_name,
                    slug_field: slug,
                }
                
                # Add image field - Webflow expects image as an object with url
                if asset_url:
                    field_data[image_field] = {
                        "url": asset_url,
                        "alt": display_name
                    }
                elif asset_id:
                    field_data[image_field] = {
                        "fileId": asset_id,
                        "alt": display_name
                    }
                
                # Add description if field is specified
                if description_field:
                    field_data[description_field] = f"Photo: {display_name}"
                
                item_resp = await client.post(
                    f"{WEBFLOW_API_BASE}/collections/{collection_id}/items",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json"
                    },
                    json={
                        "fieldData": field_data,
                        "isDraft": is_draft
                    }
                )
                
                if item_resp.status_code in (200, 201):
                    item_data = item_resp.json()
                    results.append({
                        "key": key,
                        "item_id": item_data.get("id"),
                        "name": display_name,
                        "slug": slug,
                        "asset_id": asset_id
                    })
                elif item_resp.status_code == 401:
                    errors.append({"key": key, "error": "Token expired"})
                    break
                elif item_resp.status_code == 409:
                    # Duplicate slug - try with timestamp
                    slug = f"{slug}-{int(datetime.utcnow().timestamp())}"
                    field_data[slug_field] = slug
                    
                    retry_resp = await client.post(
                        f"{WEBFLOW_API_BASE}/collections/{collection_id}/items",
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Content-Type": "application/json"
                        },
                        json={
                            "fieldData": field_data,
                            "isDraft": is_draft
                        }
                    )
                    
                    if retry_resp.status_code in (200, 201):
                        item_data = retry_resp.json()
                        results.append({
                            "key": key,
                            "item_id": item_data.get("id"),
                            "name": display_name,
                            "slug": slug,
                            "asset_id": asset_id
                        })
                    else:
                        errors.append({"key": key, "error": "Duplicate slug"})
                else:
                    error_msg = "Failed to create CMS item"
                    try:
                        err_data = item_resp.json()
                        error_msg = err_data.get("message", error_msg)
                        if "problems" in err_data:
                            problems = err_data.get("problems", [])
                            if problems:
                                error_msg = problems[0].get("message", error_msg)
                    except:
                        pass
                    errors.append({"key": key, "error": error_msg})
                    
            except Exception as ex:
                errors.append({"key": key, "error": str(ex)})
    
    # Save to history
    try:
        history = read_json_key(_webflow_history_key(uid)) or []
        if not isinstance(history, list):
            history = []
        for r in results:
            history.append({
                **r,
                "site_id": site_id,
                "collection_id": collection_id,
                "uploaded_at": datetime.utcnow().isoformat()
            })
        # Keep last 500 uploads
        history = history[-500:]
        write_json_key(_webflow_history_key(uid), history)
    except Exception as ex:
        logger.warning(f"Failed to save Webflow history: {ex}")
    
    return {
        "ok": True,
        "uploaded": len(results),
        "failed": len(errors),
        "results": results,
        "errors": errors
    }


@router.get("/history")
async def webflow_history(request: Request, limit: int = 50):
    """Get user's Webflow upload history."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        history = read_json_key(_webflow_history_key(uid)) or []
        if not isinstance(history, list):
            history = []
        # Return most recent first
        return {"history": list(reversed(history[-limit:]))}
    except Exception as ex:
        logger.error(f"Failed to get Webflow history: {ex}")
        return {"history": []}


@router.post("/publish")
async def webflow_publish(
    request: Request,
    site_id: str = Body(...)
):
    """Publish a Webflow site to make CMS changes live."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_webflow_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Webflow not connected"}, status_code=401)
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{WEBFLOW_API_BASE}/sites/{site_id}/publish",
                headers={
                    "Authorization": f"Bearer {token_data['access_token']}",
                    "Content-Type": "application/json"
                },
                json={}
            )
            
            if resp.status_code == 401:
                return JSONResponse({"error": "Token expired", "expired": True}, status_code=401)
            
            if resp.status_code not in (200, 201, 202):
                error_msg = "Failed to publish site"
                try:
                    err_data = resp.json()
                    error_msg = err_data.get("message", error_msg)
                except:
                    pass
                return JSONResponse({"error": error_msg}, status_code=500)
            
            return {"ok": True, "message": "Site published successfully"}
            
    except Exception as ex:
        logger.exception(f"Webflow publish error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)
