"""
Google Drive Backup/Sync Router
Allows users to automatically sync their photos to Google Drive.
Uses Google Drive API v3 with OAuth 2.0.
Free tier: 15GB storage per user.
"""
from typing import List, Optional
import os
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request, Body, Query, BackgroundTasks
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from core.config import logger
from core.auth import get_uid_from_request
from utils.storage import read_json_key, write_json_key, read_bytes_key, list_keys

router = APIRouter(prefix="/api/google-drive", tags=["google-drive"])

# Frontend URL for redirects after OAuth
FRONTEND_URL = os.getenv("FRONTEND_ORIGIN", "https://photomark.cloud").split(",")[0].strip()

# Google API configuration
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_DRIVE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_DRIVE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_DRIVE_REDIRECT_URI", "")
GOOGLE_OAUTH_BASE = "https://accounts.google.com/o/oauth2/v2"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_DRIVE_API = "https://www.googleapis.com/drive/v3"
GOOGLE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"

# Scopes needed for Drive access
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",  # Access files created by app
    "https://www.googleapis.com/auth/userinfo.email",  # Get user email
]

def _gdrive_token_key(uid: str) -> str:
    return f"users/{uid}/integrations/google_drive.json"


def _gdrive_settings_key(uid: str) -> str:
    return f"users/{uid}/integrations/google_drive_settings.json"


def _gdrive_state_key(state: str) -> str:
    return f"oauth/google_drive/{state}.json"


def _gdrive_sync_log_key(uid: str) -> str:
    return f"users/{uid}/integrations/google_drive_sync_log.json"


@router.get("/status")
async def gdrive_status(request: Request):
    """Check if user has connected their Google Drive account."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not GOOGLE_CLIENT_ID:
        return {"connected": False, "configured": False}
    
    try:
        token_data = read_json_key(_gdrive_token_key(uid))
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
        
        # Get sync settings
        settings = read_json_key(_gdrive_settings_key(uid)) or {}
        
        return {
            "connected": True,
            "configured": True,
            "email": token_data.get("email"),
            "folder_id": token_data.get("folder_id"),
            "folder_name": token_data.get("folder_name", "Photomark Backup"),
            "auto_sync": settings.get("auto_sync", False),
            "sync_uploads": settings.get("sync_uploads", True),
            "sync_gallery": settings.get("sync_gallery", True),
            "sync_vaults": settings.get("sync_vaults", False),
        }
    except Exception as ex:
        logger.warning(f"Google Drive status check failed: {ex}")
        return {"connected": False, "configured": True}


@router.get("/auth")
async def gdrive_auth(request: Request):
    """Initiate Google OAuth flow for Drive access."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not GOOGLE_CLIENT_ID or not GOOGLE_REDIRECT_URI:
        return JSONResponse({"error": "Google Drive integration not configured"}, status_code=500)
    
    state = secrets.token_urlsafe(32)
    
    write_json_key(_gdrive_state_key(state), {
        "uid": uid,
        "created_at": datetime.utcnow().isoformat()
    })
    
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES),
        "state": state,
        "access_type": "offline",  # Get refresh token
        "prompt": "consent",  # Always show consent to get refresh token
    }
    auth_url = f"{GOOGLE_OAUTH_BASE}/auth?{urlencode(params)}"
    
    return {"auth_url": auth_url}



@router.get("/callback")
async def gdrive_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None)
):
    """Handle Google OAuth callback."""
    if error:
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?google_drive_error=denied")
    
    if not code or not state:
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?google_drive_error=invalid")
    
    state_data = read_json_key(_gdrive_state_key(state))
    if not state_data or not state_data.get("uid"):
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?google_drive_error=invalid_state")
    
    uid = state_data["uid"]
    
    try:
        async with httpx.AsyncClient() as client:
            # Exchange code for tokens
            token_resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": GOOGLE_REDIRECT_URI,
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            
            if token_resp.status_code != 200:
                logger.error(f"Google token exchange failed: {token_resp.status_code}, {token_resp.text}")
                return RedirectResponse(url=f"{FRONTEND_URL}/integrations?google_drive_error=token_failed")
            
            tokens = token_resp.json()
            
            # Get user email
            user_resp = await client.get(
                "https://www.googleapis.com/oauth2/v2/userinfo",
                headers={"Authorization": f"Bearer {tokens['access_token']}"}
            )
            
            email = None
            if user_resp.status_code == 200:
                email = user_resp.json().get("email")
            
            # Create Photomark backup folder
            folder_id = await _create_or_get_folder(tokens["access_token"], "Photomark Backup")
        
        expires_in = tokens.get("expires_in", 3600)
        expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()
        
        write_json_key(_gdrive_token_key(uid), {
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token"),
            "expires_at": expires_at,
            "email": email,
            "folder_id": folder_id,
            "folder_name": "Photomark Backup",
            "connected_at": datetime.utcnow().isoformat()
        })
        
        # Initialize default settings
        write_json_key(_gdrive_settings_key(uid), {
            "auto_sync": False,
            "sync_uploads": True,
            "sync_gallery": True,
            "sync_vaults": False,
        })
        
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?google_drive_connected=true")
        
    except Exception as ex:
        logger.exception(f"Google Drive callback error: {ex}")
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?google_drive_error=unknown")



@router.post("/disconnect")
async def gdrive_disconnect(request: Request):
    """Disconnect Google Drive account."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        write_json_key(_gdrive_token_key(uid), {})
        write_json_key(_gdrive_settings_key(uid), {})
        return {"ok": True}
    except Exception as ex:
        logger.error(f"Google Drive disconnect failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/settings")
async def gdrive_update_settings(
    request: Request,
    auto_sync: bool = Body(False),
    sync_uploads: bool = Body(True),
    sync_gallery: bool = Body(True),
    sync_vaults: bool = Body(False),
):
    """Update Google Drive sync settings."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_gdrive_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Google Drive not connected"}, status_code=401)
    
    settings = {
        "auto_sync": auto_sync,
        "sync_uploads": sync_uploads,
        "sync_gallery": sync_gallery,
        "sync_vaults": sync_vaults,
        "updated_at": datetime.utcnow().isoformat()
    }
    
    write_json_key(_gdrive_settings_key(uid), settings)
    return {"ok": True, "settings": settings}


@router.get("/folders")
async def gdrive_list_folders(
    request: Request,
    parent_id: str = Query(None),  # None = root, or specific folder ID
):
    """List all folders in user's Google Drive (not just backup folder)."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_gdrive_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Google Drive not connected"}, status_code=401)
    
    access_token = await _ensure_valid_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "Google Drive token expired"}, status_code=401)
    
    try:
        async with httpx.AsyncClient() as client:
            # Build query - list folders, optionally within a parent
            query_parts = ["mimeType='application/vnd.google-apps.folder'", "trashed=false"]
            if parent_id:
                query_parts.append(f"'{parent_id}' in parents")
            else:
                query_parts.append("'root' in parents")
            
            resp = await client.get(
                f"{GOOGLE_DRIVE_API}/files",
                params={
                    "q": " and ".join(query_parts),
                    "fields": "files(id,name,parents,modifiedTime)",
                    "pageSize": 100,
                    "orderBy": "name"
                },
                headers={"Authorization": f"Bearer {access_token}"}
            )
            
            if resp.status_code != 200:
                return JSONResponse({"error": "Failed to list folders"}, status_code=500)
            
            folders = resp.json().get("files", [])
            return {"folders": folders, "parent_id": parent_id}
            
    except Exception as ex:
        logger.exception(f"Google Drive folders error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.get("/files")
async def gdrive_list_files(
    request: Request,
    folder_id: str = Query(None),  # None = root, or specific folder ID
    page_token: str = Query(None),
):
    """List image files in a Google Drive folder for importing."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_gdrive_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Google Drive not connected"}, status_code=401)
    
    access_token = await _ensure_valid_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "Google Drive token expired"}, status_code=401)
    
    try:
        async with httpx.AsyncClient() as client:
            # Build query - list image files in folder
            image_mimes = [
                "image/jpeg", "image/png", "image/gif", "image/webp",
                "image/heic", "image/tiff", "image/bmp"
            ]
            mime_query = " or ".join([f"mimeType='{m}'" for m in image_mimes])
            
            query_parts = [f"({mime_query})", "trashed=false"]
            if folder_id:
                query_parts.append(f"'{folder_id}' in parents")
            else:
                query_parts.append("'root' in parents")
            
            params = {
                "q": " and ".join(query_parts),
                "fields": "nextPageToken,files(id,name,mimeType,size,thumbnailLink,modifiedTime)",
                "pageSize": 50,
                "orderBy": "modifiedTime desc"
            }
            if page_token:
                params["pageToken"] = page_token
            
            resp = await client.get(
                f"{GOOGLE_DRIVE_API}/files",
                params=params,
                headers={"Authorization": f"Bearer {access_token}"}
            )
            
            if resp.status_code != 200:
                return JSONResponse({"error": "Failed to list files"}, status_code=500)
            
            data = resp.json()
            files = data.get("files", [])
            next_page = data.get("nextPageToken")
            
            return {
                "files": files,
                "folder_id": folder_id,
                "next_page_token": next_page
            }
            
    except Exception as ex:
        logger.exception(f"Google Drive files error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/import")
async def gdrive_import_files(
    request: Request,
    file_ids: List[str] = Body(...),
):
    """Import/download files from Google Drive to user's gallery."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_gdrive_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Google Drive not connected"}, status_code=401)
    
    access_token = await _ensure_valid_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "Google Drive token expired"}, status_code=401)
    
    if not file_ids:
        return JSONResponse({"error": "No files selected"}, status_code=400)
    
    if len(file_ids) > 50:
        return JSONResponse({"error": "Maximum 50 files per import"}, status_code=400)
    
    imported = []
    failed = []
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            for file_id in file_ids:
                try:
                    # Get file metadata
                    meta_resp = await client.get(
                        f"{GOOGLE_DRIVE_API}/files/{file_id}",
                        params={"fields": "id,name,mimeType,size"},
                        headers={"Authorization": f"Bearer {access_token}"}
                    )
                    
                    if meta_resp.status_code != 200:
                        failed.append({"file_id": file_id, "error": "Could not get file info"})
                        continue
                    
                    file_meta = meta_resp.json()
                    filename = file_meta.get("name", "image.jpg")
                    
                    # Download file content
                    download_resp = await client.get(
                        f"{GOOGLE_DRIVE_API}/files/{file_id}",
                        params={"alt": "media"},
                        headers={"Authorization": f"Bearer {access_token}"}
                    )
                    
                    if download_resp.status_code != 200:
                        failed.append({"file_id": file_id, "error": "Download failed"})
                        continue
                    
                    file_bytes = download_resp.content
                    
                    # Determine content type and extension
                    ext = os.path.splitext(filename)[1].lower() or ".jpg"
                    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".heic", ".tiff", ".tif"):
                        ext = ".jpg"
                    ct_map = {
                        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                        ".webp": "image/webp", ".gif": "image/gif", ".heic": "image/heic",
                        ".tiff": "image/tiff", ".tif": "image/tiff"
                    }
                    content_type = ct_map.get(ext, "image/jpeg")
                    
                    # Save to user's external folder
                    from datetime import datetime as _dt
                    date_prefix = _dt.utcnow().strftime("%Y/%m/%d")
                    base = os.path.splitext(filename)[0] or "import"
                    stamp = int(_dt.utcnow().timestamp())
                    key = f"users/{uid}/external/{date_prefix}/{base}-{stamp}{ext}"
                    
                    from utils.storage import upload_bytes
                    url = upload_bytes(key, file_bytes, content_type=content_type)
                    
                    imported.append({
                        "file_id": file_id,
                        "filename": filename,
                        "key": key,
                        "url": url
                    })
                    
                except Exception as ex:
                    logger.warning(f"Import failed for {file_id}: {ex}")
                    failed.append({"file_id": file_id, "error": str(ex)})
        
        return {
            "ok": True,
            "imported": len(imported),
            "failed": len(failed),
            "details": {"imported": imported, "failed": failed}
        }
        
    except Exception as ex:
        logger.exception(f"Google Drive import error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)



@router.post("/sync")
async def gdrive_sync_now(
    request: Request,
    background_tasks: BackgroundTasks,
    source: str = Body("uploads"),  # uploads, gallery, vaults, all
    keys: Optional[List[str]] = Body(None),  # Specific keys to sync, or None for all
):
    """Manually trigger sync to Google Drive."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_gdrive_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Google Drive not connected"}, status_code=401)
    
    access_token = await _ensure_valid_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "Google Drive token expired"}, status_code=401)
    
    folder_id = token_data.get("folder_id")
    if not folder_id:
        return JSONResponse({"error": "No backup folder configured"}, status_code=400)
    
    # Start background sync
    background_tasks.add_task(_sync_photos_to_drive, uid, access_token, folder_id, source, keys)
    
    return {"ok": True, "message": "Sync started in background"}


@router.post("/upload")
async def gdrive_upload_photos(
    request: Request,
    keys: List[str] = Body(...),
    folder_name: Optional[str] = Body(None),  # Create subfolder with this name
):
    """Upload specific photos to Google Drive."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_gdrive_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Google Drive not connected"}, status_code=401)
    
    access_token = await _ensure_valid_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "Google Drive token expired"}, status_code=401)
    
    if not keys:
        return JSONResponse({"error": "No photos selected"}, status_code=400)
    
    # Validate keys belong to user
    valid_keys = [k for k in keys if k.startswith(f"users/{uid}/")]
    if not valid_keys:
        return JSONResponse({"error": "No valid photos found"}, status_code=400)
    
    parent_folder_id = token_data.get("folder_id")
    
    # Create subfolder if requested
    if folder_name:
        parent_folder_id = await _create_or_get_folder(access_token, folder_name, parent_folder_id)
    
    uploaded = []
    failed = []
    
    for key in valid_keys:
        try:
            image_bytes = read_bytes_key(key)
            if not image_bytes:
                failed.append({"key": key, "error": "Could not read file"})
                continue
            
            filename = key.split("/")[-1]
            content_type = _get_content_type(filename)
            
            file_id = await _upload_file_to_drive(
                access_token, 
                image_bytes, 
                filename, 
                content_type, 
                parent_folder_id
            )
            
            if file_id:
                uploaded.append({"key": key, "file_id": file_id, "filename": filename})
            else:
                failed.append({"key": key, "error": "Upload failed"})
                
        except Exception as ex:
            logger.warning(f"Failed to upload {key}: {ex}")
            failed.append({"key": key, "error": str(ex)})
    
    return {
        "ok": True,
        "uploaded": len(uploaded),
        "failed": len(failed),
        "details": {"uploaded": uploaded, "failed": failed}
    }


@router.get("/sync-status")
async def gdrive_sync_status(request: Request):
    """Get last sync status and history."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    sync_log = read_json_key(_gdrive_sync_log_key(uid)) or {}
    
    return {
        "last_sync": sync_log.get("last_sync"),
        "last_sync_count": sync_log.get("last_sync_count", 0),
        "total_synced": sync_log.get("total_synced", 0),
        "last_error": sync_log.get("last_error"),
    }



# ============ Helper Functions ============

async def _create_or_get_folder(access_token: str, folder_name: str, parent_id: str = None) -> Optional[str]:
    """Create a folder in Google Drive or return existing one."""
    try:
        async with httpx.AsyncClient() as client:
            # Check if folder exists
            query = f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
            if parent_id:
                query += f" and '{parent_id}' in parents"
            
            resp = await client.get(
                f"{GOOGLE_DRIVE_API}/files",
                params={"q": query, "fields": "files(id,name)"},
                headers={"Authorization": f"Bearer {access_token}"}
            )
            
            if resp.status_code == 200:
                files = resp.json().get("files", [])
                if files:
                    return files[0]["id"]
            
            # Create folder
            metadata = {
                "name": folder_name,
                "mimeType": "application/vnd.google-apps.folder"
            }
            if parent_id:
                metadata["parents"] = [parent_id]
            
            create_resp = await client.post(
                f"{GOOGLE_DRIVE_API}/files",
                json=metadata,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json"
                }
            )
            
            if create_resp.status_code in (200, 201):
                return create_resp.json().get("id")
            
            return None
            
    except Exception as ex:
        logger.exception(f"Create folder error: {ex}")
        return None


async def _upload_file_to_drive(
    access_token: str, 
    file_bytes: bytes, 
    filename: str, 
    content_type: str,
    parent_folder_id: str
) -> Optional[str]:
    """Upload a file to Google Drive using resumable upload."""
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Create file metadata
            metadata = {
                "name": filename,
                "parents": [parent_folder_id] if parent_folder_id else []
            }
            
            # Initiate resumable upload
            init_resp = await client.post(
                f"{GOOGLE_UPLOAD_API}/files?uploadType=resumable",
                json=metadata,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "X-Upload-Content-Type": content_type,
                    "X-Upload-Content-Length": str(len(file_bytes))
                }
            )
            
            if init_resp.status_code != 200:
                logger.error(f"Drive upload init failed: {init_resp.status_code}")
                return None
            
            upload_url = init_resp.headers.get("Location")
            if not upload_url:
                return None
            
            # Upload file content
            upload_resp = await client.put(
                upload_url,
                content=file_bytes,
                headers={"Content-Type": content_type}
            )
            
            if upload_resp.status_code in (200, 201):
                return upload_resp.json().get("id")
            
            return None
            
    except Exception as ex:
        logger.exception(f"Drive upload error: {ex}")
        return None


def _get_content_type(filename: str) -> str:
    """Get content type from filename."""
    ext = filename.lower().split(".")[-1] if "." in filename else ""
    content_types = {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "gif": "image/gif",
        "webp": "image/webp",
        "heic": "image/heic",
        "tiff": "image/tiff",
        "tif": "image/tiff",
    }
    return content_types.get(ext, "application/octet-stream")


async def _sync_photos_to_drive(
    uid: str, 
    access_token: str, 
    folder_id: str, 
    source: str,
    specific_keys: Optional[List[str]] = None
):
    """Background task to sync photos to Google Drive."""
    try:
        sync_log = read_json_key(_gdrive_sync_log_key(uid)) or {}
        synced_keys = set(sync_log.get("synced_keys", []))
        
        keys_to_sync = []
        
        if specific_keys:
            keys_to_sync = [k for k in specific_keys if k.startswith(f"users/{uid}/")]
        else:
            # Get all keys based on source
            if source in ("uploads", "all"):
                uploads_keys = list_keys(f"users/{uid}/external/") or []
                keys_to_sync.extend(uploads_keys)
            
            if source in ("gallery", "all"):
                gallery_keys = list_keys(f"users/{uid}/watermarked/") or []
                keys_to_sync.extend(gallery_keys)
            
            if source in ("vaults", "all"):
                # Get vault photos - this would need vault listing logic
                pass
        
        # Filter out already synced
        new_keys = [k for k in keys_to_sync if k not in synced_keys]
        
        if not new_keys:
            sync_log["last_sync"] = datetime.utcnow().isoformat()
            sync_log["last_sync_count"] = 0
            write_json_key(_gdrive_sync_log_key(uid), sync_log)
            return
        
        # Create subfolders for organization
        subfolder_map = {}
        
        uploaded_count = 0
        for key in new_keys[:100]:  # Limit to 100 per sync
            try:
                # Determine subfolder based on key path
                if "/external/" in key:
                    subfolder_name = "My Uploads"
                elif "/watermarked/" in key:
                    subfolder_name = "Gallery"
                else:
                    subfolder_name = "Other"
                
                # Get or create subfolder
                if subfolder_name not in subfolder_map:
                    subfolder_id = await _create_or_get_folder(access_token, subfolder_name, folder_id)
                    subfolder_map[subfolder_name] = subfolder_id
                
                parent_id = subfolder_map.get(subfolder_name, folder_id)
                
                # Read and upload file
                image_bytes = read_bytes_key(key)
                if not image_bytes:
                    continue
                
                filename = key.split("/")[-1]
                content_type = _get_content_type(filename)
                
                file_id = await _upload_file_to_drive(
                    access_token, image_bytes, filename, content_type, parent_id
                )
                
                if file_id:
                    synced_keys.add(key)
                    uploaded_count += 1
                    
            except Exception as ex:
                logger.warning(f"Sync failed for {key}: {ex}")
                continue
        
        # Update sync log
        sync_log["last_sync"] = datetime.utcnow().isoformat()
        sync_log["last_sync_count"] = uploaded_count
        sync_log["total_synced"] = len(synced_keys)
        sync_log["synced_keys"] = list(synced_keys)[-1000]  # Keep last 1000
        sync_log["last_error"] = None
        write_json_key(_gdrive_sync_log_key(uid), sync_log)
        
        logger.info(f"Google Drive sync completed for {uid}: {uploaded_count} files uploaded")
        
    except Exception as ex:
        logger.exception(f"Google Drive sync error for {uid}: {ex}")
        sync_log = read_json_key(_gdrive_sync_log_key(uid)) or {}
        sync_log["last_error"] = str(ex)
        sync_log["last_sync"] = datetime.utcnow().isoformat()
        write_json_key(_gdrive_sync_log_key(uid), sync_log)


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
    """Refresh Google access token."""
    if not refresh_token or not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        return None
    
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )
            
            if resp.status_code != 200:
                logger.warning(f"Google token refresh failed: {resp.status_code}")
                return None
            
            tokens = resp.json()
            expires_in = tokens.get("expires_in", 3600)
            expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()
            
            existing = read_json_key(_gdrive_token_key(uid)) or {}
            existing.update({
                "access_token": tokens["access_token"],
                "expires_at": expires_at
            })
            # Keep existing refresh_token if not provided in response
            if tokens.get("refresh_token"):
                existing["refresh_token"] = tokens["refresh_token"]
            
            write_json_key(_gdrive_token_key(uid), existing)
            
            return tokens["access_token"]
            
    except Exception as ex:
        logger.exception(f"Google token refresh error: {ex}")
        return None


# ============ Auto-sync Hook ============
# This function can be called from upload endpoints to trigger auto-sync

async def trigger_auto_sync_if_enabled(uid: str, keys: List[str]):
    """Trigger auto-sync if user has it enabled. Call this after uploads."""
    try:
        settings = read_json_key(_gdrive_settings_key(uid)) or {}
        if not settings.get("auto_sync"):
            return
        
        token_data = read_json_key(_gdrive_token_key(uid))
        if not token_data or not token_data.get("access_token"):
            return
        
        access_token = await _ensure_valid_token(uid, token_data)
        if not access_token:
            return
        
        folder_id = token_data.get("folder_id")
        if not folder_id:
            return
        
        # Sync the specific keys
        await _sync_photos_to_drive(uid, access_token, folder_id, "uploads", keys)
        
    except Exception as ex:
        logger.warning(f"Auto-sync trigger failed for {uid}: {ex}")