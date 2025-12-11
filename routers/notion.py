"""
Notion Integration Router
Allows users to create portfolio pages/galleries in Notion.
Uses Notion API with OAuth 2.0.
Free tier: Unlimited blocks for personal use.
Docs: https://developers.notion.com/
"""
from typing import List, Optional
import os
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode
import base64

import httpx
from fastapi import APIRouter, Request, Body, Query, BackgroundTasks
from fastapi.responses import JSONResponse, RedirectResponse

from core.config import logger
from core.auth import get_uid_from_request
from utils.storage import read_json_key, write_json_key, read_bytes_key

router = APIRouter(prefix="/api/notion", tags=["notion"])

# Frontend URL for redirects after OAuth
FRONTEND_URL = os.getenv("FRONTEND_ORIGIN", "https://photomark.cloud").split(",")[0].strip()

# Notion API configuration
NOTION_CLIENT_ID = os.getenv("NOTION_CLIENT_ID", "")
NOTION_CLIENT_SECRET = os.getenv("NOTION_CLIENT_SECRET", "")
NOTION_REDIRECT_URI = os.getenv("NOTION_REDIRECT_URI", "")
NOTION_OAUTH_URL = "https://api.notion.com/v1/oauth/authorize"
NOTION_TOKEN_URL = "https://api.notion.com/v1/oauth/token"
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_API_VERSION = "2022-06-28"


def _notion_token_key(uid: str) -> str:
    return f"users/{uid}/integrations/notion.json"


def _notion_settings_key(uid: str) -> str:
    return f"users/{uid}/integrations/notion_settings.json"


def _notion_state_key(state: str) -> str:
    return f"oauth/notion/{state}.json"


@router.get("/status")
async def notion_status(request: Request):
    """Check if user has connected their Notion account."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not NOTION_CLIENT_ID:
        return {"connected": False, "configured": False}
    
    try:
        token_data = read_json_key(_notion_token_key(uid))
        if not token_data or not token_data.get("access_token"):
            return {"connected": False, "configured": True}
        
        # Get settings
        settings = read_json_key(_notion_settings_key(uid)) or {}
        
        return {
            "connected": True,
            "configured": True,
            "workspace_name": token_data.get("workspace_name"),
            "workspace_icon": token_data.get("workspace_icon"),
            "bot_id": token_data.get("bot_id"),
            "default_page_id": settings.get("default_page_id"),
        }
    except Exception as ex:
        logger.warning(f"Notion status check failed: {ex}")
        return {"connected": False, "configured": True}


@router.get("/auth")
async def notion_auth(request: Request):
    """Initiate Notion OAuth flow."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not NOTION_CLIENT_ID or not NOTION_REDIRECT_URI:
        return JSONResponse({"error": "Notion integration not configured"}, status_code=500)
    
    state = secrets.token_urlsafe(32)
    
    write_json_key(_notion_state_key(state), {
        "uid": uid,
        "created_at": datetime.utcnow().isoformat()
    })
    
    params = {
        "client_id": NOTION_CLIENT_ID,
        "redirect_uri": NOTION_REDIRECT_URI,
        "response_type": "code",
        "state": state,
        "owner": "user",
    }
    auth_url = f"{NOTION_OAUTH_URL}?{urlencode(params)}"
    
    return {"auth_url": auth_url}


@router.get("/callback")
async def notion_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None)
):
    """Handle Notion OAuth callback."""
    if error:
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?notion_error=denied")
    
    if not code or not state:
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?notion_error=invalid")
    
    state_data = read_json_key(_notion_state_key(state))
    if not state_data or not state_data.get("uid"):
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?notion_error=invalid_state")
    
    uid = state_data["uid"]
    
    try:
        # Notion uses Basic Auth for token exchange
        credentials = base64.b64encode(
            f"{NOTION_CLIENT_ID}:{NOTION_CLIENT_SECRET}".encode()
        ).decode()
        
        async with httpx.AsyncClient() as client:
            token_resp = await client.post(
                NOTION_TOKEN_URL,
                json={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": NOTION_REDIRECT_URI
                },
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "application/json",
                    "Notion-Version": NOTION_API_VERSION
                }
            )
            
            if token_resp.status_code != 200:
                logger.error(f"Notion token exchange failed: {token_resp.status_code}, {token_resp.text}")
                return RedirectResponse(url=f"{FRONTEND_URL}/integrations?notion_error=token_failed")
            
            data = token_resp.json()
        
        # Extract workspace info
        workspace_name = data.get("workspace_name")
        workspace_icon = data.get("workspace_icon")
        bot_id = data.get("bot_id")
        
        write_json_key(_notion_token_key(uid), {
            "access_token": data["access_token"],
            "token_type": data.get("token_type", "bearer"),
            "bot_id": bot_id,
            "workspace_id": data.get("workspace_id"),
            "workspace_name": workspace_name,
            "workspace_icon": workspace_icon,
            "owner": data.get("owner"),
            "connected_at": datetime.utcnow().isoformat()
        })
        
        # Initialize default settings
        write_json_key(_notion_settings_key(uid), {
            "default_page_id": None,
        })
        
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?notion_connected=true")
        
    except Exception as ex:
        logger.exception(f"Notion callback error: {ex}")
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?notion_error=unknown")


@router.post("/disconnect")
async def notion_disconnect(request: Request):
    """Disconnect Notion account."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        write_json_key(_notion_token_key(uid), {})
        write_json_key(_notion_settings_key(uid), {})
        return {"ok": True}
    except Exception as ex:
        logger.error(f"Notion disconnect failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/settings")
async def notion_update_settings(
    request: Request,
    default_page_id: Optional[str] = Body(None),
):
    """Update Notion settings."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_notion_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Notion not connected"}, status_code=401)
    
    settings = {
        "default_page_id": default_page_id,
        "updated_at": datetime.utcnow().isoformat()
    }
    
    write_json_key(_notion_settings_key(uid), settings)
    return {"ok": True, "settings": settings}


async def _get_notion_headers(access_token: str) -> dict:
    """Get headers for Notion API requests."""
    return {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Notion-Version": NOTION_API_VERSION
    }


@router.get("/pages")
async def notion_list_pages(request: Request):
    """List pages the integration has access to."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_notion_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Notion not connected"}, status_code=401)
    
    access_token = token_data["access_token"]
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{NOTION_API_BASE}/search",
                json={
                    "filter": {"property": "object", "value": "page"},
                    "sort": {"direction": "descending", "timestamp": "last_edited_time"}
                },
                headers=await _get_notion_headers(access_token)
            )
            
            if resp.status_code != 200:
                logger.error(f"Notion search failed: {resp.status_code}, {resp.text}")
                return JSONResponse({"error": "Failed to list pages"}, status_code=500)
            
            data = resp.json()
            pages = []
            for result in data.get("results", []):
                title = ""
                if result.get("properties", {}).get("title", {}).get("title"):
                    title_parts = result["properties"]["title"]["title"]
                    title = "".join([t.get("plain_text", "") for t in title_parts])
                elif result.get("properties", {}).get("Name", {}).get("title"):
                    title_parts = result["properties"]["Name"]["title"]
                    title = "".join([t.get("plain_text", "") for t in title_parts])
                
                pages.append({
                    "id": result["id"],
                    "title": title or "Untitled",
                    "url": result.get("url"),
                    "icon": result.get("icon"),
                    "last_edited": result.get("last_edited_time")
                })
            
            return {"pages": pages}
            
    except Exception as ex:
        logger.exception(f"Notion list pages error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.get("/databases")
async def notion_list_databases(request: Request):
    """List databases the integration has access to."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_notion_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Notion not connected"}, status_code=401)
    
    access_token = token_data["access_token"]
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{NOTION_API_BASE}/search",
                json={
                    "filter": {"property": "object", "value": "database"},
                    "sort": {"direction": "descending", "timestamp": "last_edited_time"}
                },
                headers=await _get_notion_headers(access_token)
            )
            
            if resp.status_code != 200:
                return JSONResponse({"error": "Failed to list databases"}, status_code=500)
            
            data = resp.json()
            databases = []
            for result in data.get("results", []):
                title = ""
                if result.get("title"):
                    title = "".join([t.get("plain_text", "") for t in result["title"]])
                
                databases.append({
                    "id": result["id"],
                    "title": title or "Untitled Database",
                    "url": result.get("url"),
                    "icon": result.get("icon"),
                })
            
            return {"databases": databases}
            
    except Exception as ex:
        logger.exception(f"Notion list databases error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/create-gallery")
async def notion_create_gallery(
    request: Request,
    page_id: str = Body(...),
    title: str = Body("Photo Gallery"),
    photo_urls: List[str] = Body(...),
    captions: Optional[List[str]] = Body(None),
):
    """Create a gallery of photos in a Notion page."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_notion_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Notion not connected"}, status_code=401)
    
    if not photo_urls:
        return JSONResponse({"error": "No photos provided"}, status_code=400)
    
    if len(photo_urls) > 100:
        return JSONResponse({"error": "Maximum 100 photos per gallery"}, status_code=400)
    
    access_token = token_data["access_token"]
    
    try:
        # Build blocks for the gallery
        blocks = [
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text", "text": {"content": title}}]
                }
            },
            {
                "object": "block",
                "type": "divider",
                "divider": {}
            }
        ]
        
        # Add image blocks
        for i, url in enumerate(photo_urls):
            image_block = {
                "object": "block",
                "type": "image",
                "image": {
                    "type": "external",
                    "external": {"url": url}
                }
            }
            blocks.append(image_block)
            
            # Add caption if provided
            if captions and i < len(captions) and captions[i]:
                caption_block = {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": captions[i]}}],
                        "color": "gray"
                    }
                }
                blocks.append(caption_block)
        
        async with httpx.AsyncClient(timeout=60.0) as client:
            # Append blocks to the page (Notion limits to 100 blocks per request)
            resp = await client.patch(
                f"{NOTION_API_BASE}/blocks/{page_id}/children",
                json={"children": blocks[:100]},
                headers=await _get_notion_headers(access_token)
            )
            
            if resp.status_code != 200:
                logger.error(f"Notion create gallery failed: {resp.status_code}, {resp.text}")
                return JSONResponse({"error": "Failed to create gallery"}, status_code=500)
            
            return {
                "ok": True,
                "photos_added": min(len(photo_urls), 100),
                "page_id": page_id
            }
            
    except Exception as ex:
        logger.exception(f"Notion create gallery error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/create-page")
async def notion_create_page(
    request: Request,
    parent_page_id: str = Body(...),
    title: str = Body("My Portfolio"),
    cover_url: Optional[str] = Body(None),
    icon_emoji: Optional[str] = Body("📷"),
):
    """Create a new page in Notion."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_notion_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Notion not connected"}, status_code=401)
    
    access_token = token_data["access_token"]
    
    try:
        page_data = {
            "parent": {"page_id": parent_page_id},
            "properties": {
                "title": {
                    "title": [{"type": "text", "text": {"content": title}}]
                }
            }
        }
        
        if icon_emoji:
            page_data["icon"] = {"type": "emoji", "emoji": icon_emoji}
        
        if cover_url:
            page_data["cover"] = {"type": "external", "external": {"url": cover_url}}
        
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{NOTION_API_BASE}/pages",
                json=page_data,
                headers=await _get_notion_headers(access_token)
            )
            
            if resp.status_code != 200:
                logger.error(f"Notion create page failed: {resp.status_code}, {resp.text}")
                return JSONResponse({"error": "Failed to create page"}, status_code=500)
            
            data = resp.json()
            return {
                "ok": True,
                "page_id": data["id"],
                "url": data.get("url")
            }
            
    except Exception as ex:
        logger.exception(f"Notion create page error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/add-to-database")
async def notion_add_to_database(
    request: Request,
    database_id: str = Body(...),
    photos: List[dict] = Body(...),  # [{url, title, description}]
):
    """Add photos as entries to a Notion database."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_notion_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Notion not connected"}, status_code=401)
    
    if not photos:
        return JSONResponse({"error": "No photos provided"}, status_code=400)
    
    access_token = token_data["access_token"]
    added = []
    failed = []
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            # First, get database schema to understand properties
            db_resp = await client.get(
                f"{NOTION_API_BASE}/databases/{database_id}",
                headers=await _get_notion_headers(access_token)
            )
            
            if db_resp.status_code != 200:
                return JSONResponse({"error": "Could not access database"}, status_code=500)
            
            db_schema = db_resp.json()
            properties = db_schema.get("properties", {})
            
            # Find title property
            title_prop = None
            for prop_name, prop_data in properties.items():
                if prop_data.get("type") == "title":
                    title_prop = prop_name
                    break
            
            if not title_prop:
                return JSONResponse({"error": "Database has no title property"}, status_code=400)
            
            for photo in photos[:50]:  # Limit to 50 per request
                try:
                    page_data = {
                        "parent": {"database_id": database_id},
                        "properties": {
                            title_prop: {
                                "title": [{"type": "text", "text": {"content": photo.get("title", "Untitled")}}]
                            }
                        }
                    }
                    
                    # Set cover image
                    if photo.get("url"):
                        page_data["cover"] = {"type": "external", "external": {"url": photo["url"]}}
                    
                    resp = await client.post(
                        f"{NOTION_API_BASE}/pages",
                        json=page_data,
                        headers=await _get_notion_headers(access_token)
                    )
                    
                    if resp.status_code == 200:
                        data = resp.json()
                        added.append({"title": photo.get("title"), "page_id": data["id"]})
                    else:
                        failed.append({"title": photo.get("title"), "error": "Create failed"})
                        
                except Exception as ex:
                    failed.append({"title": photo.get("title", "Unknown"), "error": str(ex)})
        
        return {
            "ok": True,
            "added": len(added),
            "failed": len(failed),
            "details": {"added": added, "failed": failed}
        }
        
    except Exception as ex:
        logger.exception(f"Notion add to database error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)
