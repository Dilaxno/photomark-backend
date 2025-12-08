"""
Cloud Storage Picker Router
Allows users to browse and import files from connected cloud storage accounts
(Google Drive, Dropbox, OneDrive) into their uploads, vaults, or portfolio.
"""
from typing import List, Optional
import os
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Request, Body, Query
from fastapi.responses import JSONResponse

from core.config import logger
from core.auth import get_uid_from_request
from utils.storage import read_json_key, write_json_key, write_bytes_key

router = APIRouter(prefix="/api/cloud-picker", tags=["cloud-picker"])

# API configurations (reuse from existing routers)
GOOGLE_DRIVE_API = "https://www.googleapis.com/drive/v3"
DROPBOX_API_BASE = "https://api.dropboxapi.com/2"
DROPBOX_CONTENT_BASE = "https://content.dropboxapi.com/2"
GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"

# Token key helpers
def _gdrive_token_key(uid: str) -> str:
    return f"users/{uid}/integrations/google_drive.json"

def _dropbox_token_key(uid: str) -> str:
    return f"users/{uid}/integrations/dropbox.json"

def _onedrive_token_key(uid: str) -> str:
    return f"users/{uid}/integrations/onedrive.json"


# ============ Connection Status ============

@router.get("/status")
async def cloud_picker_status(request: Request):
    """Get connection status for all cloud storage providers."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    providers = {}
    
    # Google Drive
    gdrive_data = read_json_key(_gdrive_token_key(uid))
    providers["google-drive"] = {
        "connected": bool(gdrive_data and gdrive_data.get("access_token")),
        "email": gdrive_data.get("email") if gdrive_data else None,
        "display_name": gdrive_data.get("email") if gdrive_data else None,
    }
    
    # Dropbox
    dropbox_data = read_json_key(_dropbox_token_key(uid))
    providers["dropbox"] = {
        "connected": bool(dropbox_data and dropbox_data.get("access_token")),
        "email": dropbox_data.get("email") if dropbox_data else None,
        "display_name": dropbox_data.get("display_name") if dropbox_data else None,
    }
    
    # OneDrive
    onedrive_data = read_json_key(_onedrive_token_key(uid))
    providers["onedrive"] = {
        "connected": bool(onedrive_data and onedrive_data.get("access_token")),
        "email": onedrive_data.get("email") if onedrive_data else None,
        "display_name": onedrive_data.get("display_name") if onedrive_data else None,
    }
    
    return {"providers": providers}


# ============ Google Drive ============

@router.get("/google-drive/files")
async def gdrive_list_files(
    request: Request,
    folder_id: str = Query(None),
    page_token: str = Query(None),
):
    """List files and folders from Google Drive."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_gdrive_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Google Drive not connected"}, status_code=401)
    
    access_token = await _ensure_gdrive_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "Google Drive token expired"}, status_code=401)
    
    try:
        async with httpx.AsyncClient() as client:
            # Build query - show images and folders
            parent = folder_id or "root"
            query = f"'{parent}' in parents and trashed=false"
            
            params = {
                "q": query,
                "fields": "nextPageToken,files(id,name,mimeType,size,thumbnailLink,modifiedTime,webContentLink)",
                "pageSize": 50,
                "orderBy": "folder,name",
            }
            if page_token:
                params["pageToken"] = page_token
            
            resp = await client.get(
                f"{GOOGLE_DRIVE_API}/files",
                params=params,
                headers={"Authorization": f"Bearer {access_token}"}
            )
            
            if resp.status_code != 200:
                logger.error(f"Google Drive list failed: {resp.status_code} - {resp.text}")
                return JSONResponse({"error": "Failed to list files"}, status_code=500)
            
            data = resp.json()
            files = []
            
            for f in data.get("files", []):
                is_folder = f.get("mimeType") == "application/vnd.google-apps.folder"
                is_image = f.get("mimeType", "").startswith("image/")
                
                # Only include folders and images
                if not is_folder and not is_image:
                    continue
                
                files.append({
                    "id": f.get("id"),
                    "name": f.get("name"),
                    "type": "folder" if is_folder else "file",
                    "mimeType": f.get("mimeType"),
                    "size": int(f.get("size", 0)) if f.get("size") else None,
                    "thumbnail": f.get("thumbnailLink"),
                    "modifiedTime": f.get("modifiedTime"),
                })
            
            return {
                "files": files,
                "nextPageToken": data.get("nextPageToken"),
                "currentFolder": folder_id or "root",
            }
            
    except Exception as ex:
        logger.exception(f"Google Drive list error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/google-drive/download")
async def gdrive_download_files(
    request: Request,
    file_ids: List[str] = Body(...),
    destination: str = Body("uploads"),  # uploads, vault, portfolio
    vault_name: str = Body(None),
):
    """Download files from Google Drive and import to Photomark."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_gdrive_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Google Drive not connected"}, status_code=401)
    
    access_token = await _ensure_gdrive_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "Google Drive token expired"}, status_code=401)
    
    if not file_ids:
        return JSONResponse({"error": "No files selected"}, status_code=400)
    
    imported = []
    failed = []
    
    async with httpx.AsyncClient(timeout=120.0) as client:
        for file_id in file_ids[:50]:  # Limit to 50 files
            try:
                # Get file metadata
                meta_resp = await client.get(
                    f"{GOOGLE_DRIVE_API}/files/{file_id}",
                    params={"fields": "id,name,mimeType,size"},
                    headers={"Authorization": f"Bearer {access_token}"}
                )
                
                if meta_resp.status_code != 200:
                    failed.append({"id": file_id, "error": "Could not get file info"})
                    continue
                
                meta = meta_resp.json()
                filename = meta.get("name", f"file_{file_id}")
                
                # Download file content
                download_resp = await client.get(
                    f"{GOOGLE_DRIVE_API}/files/{file_id}",
                    params={"alt": "media"},
                    headers={"Authorization": f"Bearer {access_token}"}
                )
                
                if download_resp.status_code != 200:
                    failed.append({"id": file_id, "name": filename, "error": "Download failed"})
                    continue
                
                file_bytes = download_resp.content
                
                # Save to appropriate destination
                key = await _save_imported_file(uid, filename, file_bytes, destination, vault_name)
                
                if key:
                    imported.append({"id": file_id, "name": filename, "key": key})
                else:
                    failed.append({"id": file_id, "name": filename, "error": "Save failed"})
                    
            except Exception as ex:
                logger.warning(f"Failed to import {file_id}: {ex}")
                failed.append({"id": file_id, "error": str(ex)})
    
    return {
        "ok": True,
        "imported": len(imported),
        "failed": len(failed),
        "details": {"imported": imported, "failed": failed}
    }


# ============ Dropbox ============

@router.get("/dropbox/files")
async def dropbox_list_files(
    request: Request,
    path: str = Query(""),
):
    """List files and folders from Dropbox."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_dropbox_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Dropbox not connected"}, status_code=401)
    
    access_token = await _ensure_dropbox_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "Dropbox token expired"}, status_code=401)
    
    try:
        async with httpx.AsyncClient() as client:
            folder_path = path if path else ""
            
            resp = await client.post(
                f"{DROPBOX_API_BASE}/files/list_folder",
                json={
                    "path": folder_path,
                    "include_media_info": True,
                    "include_deleted": False,
                    "include_has_explicit_shared_members": False,
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json"
                }
            )
            
            if resp.status_code != 200:
                logger.error(f"Dropbox list failed: {resp.status_code} - {resp.text}")
                return JSONResponse({"error": "Failed to list files"}, status_code=500)
            
            data = resp.json()
            files = []
            
            for entry in data.get("entries", []):
                tag = entry.get(".tag")
                is_folder = tag == "folder"
                
                # Check if it's an image
                is_image = False
                if tag == "file":
                    name_lower = entry.get("name", "").lower()
                    is_image = any(name_lower.endswith(ext) for ext in [
                        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".tiff", ".tif"
                    ])
                
                if not is_folder and not is_image:
                    continue
                
                files.append({
                    "id": entry.get("id"),
                    "name": entry.get("name"),
                    "path": entry.get("path_display") or entry.get("path_lower"),
                    "type": "folder" if is_folder else "file",
                    "size": entry.get("size"),
                    "modifiedTime": entry.get("server_modified"),
                })
            
            return {
                "files": files,
                "hasMore": data.get("has_more", False),
                "cursor": data.get("cursor"),
                "currentPath": folder_path or "/",
            }
            
    except Exception as ex:
        logger.exception(f"Dropbox list error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/dropbox/download")
async def dropbox_download_files(
    request: Request,
    file_paths: List[str] = Body(...),
    destination: str = Body("uploads"),
    vault_name: str = Body(None),
):
    """Download files from Dropbox and import to Photomark."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_dropbox_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Dropbox not connected"}, status_code=401)
    
    access_token = await _ensure_dropbox_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "Dropbox token expired"}, status_code=401)
    
    if not file_paths:
        return JSONResponse({"error": "No files selected"}, status_code=400)
    
    imported = []
    failed = []
    
    async with httpx.AsyncClient(timeout=120.0) as client:
        for file_path in file_paths[:50]:
            try:
                import json
                
                # Download file
                resp = await client.post(
                    f"{DROPBOX_CONTENT_BASE}/files/download",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Dropbox-API-Arg": json.dumps({"path": file_path})
                    }
                )
                
                if resp.status_code != 200:
                    failed.append({"path": file_path, "error": "Download failed"})
                    continue
                
                # Get filename from header or path
                api_result = resp.headers.get("Dropbox-API-Result", "{}")
                result_data = json.loads(api_result)
                filename = result_data.get("name") or file_path.split("/")[-1]
                
                file_bytes = resp.content
                
                key = await _save_imported_file(uid, filename, file_bytes, destination, vault_name)
                
                if key:
                    imported.append({"path": file_path, "name": filename, "key": key})
                else:
                    failed.append({"path": file_path, "name": filename, "error": "Save failed"})
                    
            except Exception as ex:
                logger.warning(f"Failed to import {file_path}: {ex}")
                failed.append({"path": file_path, "error": str(ex)})
    
    return {
        "ok": True,
        "imported": len(imported),
        "failed": len(failed),
        "details": {"imported": imported, "failed": failed}
    }


# ============ OneDrive ============

@router.get("/onedrive/files")
async def onedrive_list_files(
    request: Request,
    folder_id: str = Query(None),
):
    """List files and folders from OneDrive."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_onedrive_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "OneDrive not connected"}, status_code=401)
    
    access_token = await _ensure_onedrive_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "OneDrive token expired"}, status_code=401)
    
    try:
        async with httpx.AsyncClient() as client:
            if folder_id:
                url = f"{GRAPH_API_BASE}/me/drive/items/{folder_id}/children"
            else:
                url = f"{GRAPH_API_BASE}/me/drive/root/children"
            
            resp = await client.get(
                url,
                params={
                    "$select": "id,name,size,file,folder,lastModifiedDateTime,@microsoft.graph.downloadUrl",
                    "$top": 100,
                },
                headers={"Authorization": f"Bearer {access_token}"}
            )
            
            if resp.status_code != 200:
                logger.error(f"OneDrive list failed: {resp.status_code} - {resp.text}")
                return JSONResponse({"error": "Failed to list files"}, status_code=500)
            
            data = resp.json()
            files = []
            
            for item in data.get("value", []):
                is_folder = "folder" in item
                is_image = False
                
                if "file" in item:
                    mime = item.get("file", {}).get("mimeType", "")
                    is_image = mime.startswith("image/")
                
                if not is_folder and not is_image:
                    continue
                
                files.append({
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "type": "folder" if is_folder else "file",
                    "size": item.get("size"),
                    "modifiedTime": item.get("lastModifiedDateTime"),
                    "downloadUrl": item.get("@microsoft.graph.downloadUrl"),
                })
            
            return {
                "files": files,
                "nextLink": data.get("@odata.nextLink"),
                "currentFolder": folder_id or "root",
            }
            
    except Exception as ex:
        logger.exception(f"OneDrive list error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/onedrive/download")
async def onedrive_download_files(
    request: Request,
    file_ids: List[str] = Body(...),
    destination: str = Body("uploads"),
    vault_name: str = Body(None),
):
    """Download files from OneDrive and import to Photomark."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    token_data = read_json_key(_onedrive_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "OneDrive not connected"}, status_code=401)
    
    access_token = await _ensure_onedrive_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "OneDrive token expired"}, status_code=401)
    
    if not file_ids:
        return JSONResponse({"error": "No files selected"}, status_code=400)
    
    imported = []
    failed = []
    
    async with httpx.AsyncClient(timeout=120.0) as client:
        for file_id in file_ids[:50]:
            try:
                # Get file metadata with download URL
                meta_resp = await client.get(
                    f"{GRAPH_API_BASE}/me/drive/items/{file_id}",
                    params={"$select": "id,name,@microsoft.graph.downloadUrl"},
                    headers={"Authorization": f"Bearer {access_token}"}
                )
                
                if meta_resp.status_code != 200:
                    failed.append({"id": file_id, "error": "Could not get file info"})
                    continue
                
                meta = meta_resp.json()
                filename = meta.get("name", f"file_{file_id}")
                download_url = meta.get("@microsoft.graph.downloadUrl")
                
                if not download_url:
                    failed.append({"id": file_id, "name": filename, "error": "No download URL"})
                    continue
                
                # Download file content
                download_resp = await client.get(download_url)
                
                if download_resp.status_code != 200:
                    failed.append({"id": file_id, "name": filename, "error": "Download failed"})
                    continue
                
                file_bytes = download_resp.content
                
                key = await _save_imported_file(uid, filename, file_bytes, destination, vault_name)
                
                if key:
                    imported.append({"id": file_id, "name": filename, "key": key})
                else:
                    failed.append({"id": file_id, "name": filename, "error": "Save failed"})
                    
            except Exception as ex:
                logger.warning(f"Failed to import {file_id}: {ex}")
                failed.append({"id": file_id, "error": str(ex)})
    
    return {
        "ok": True,
        "imported": len(imported),
        "failed": len(failed),
        "details": {"imported": imported, "failed": failed}
    }


# ============ Helper Functions ============

async def _save_imported_file(
    uid: str,
    filename: str,
    file_bytes: bytes,
    destination: str,
    vault_name: Optional[str] = None
) -> Optional[str]:
    """Save imported file to the appropriate destination."""
    try:
        import uuid
        from datetime import datetime
        
        # Sanitize filename
        safe_name = "".join(c for c in filename if c.isalnum() or c in "._- ").strip()
        if not safe_name:
            safe_name = f"imported_{uuid.uuid4().hex[:8]}"
        
        # Ensure extension
        if "." not in safe_name:
            safe_name += ".jpg"
        
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        unique_id = uuid.uuid4().hex[:6]
        
        if destination == "vault" and vault_name:
            key = f"users/{uid}/vaults/{vault_name}/{timestamp}_{unique_id}_{safe_name}"
        elif destination == "portfolio":
            key = f"users/{uid}/portfolio/{timestamp}_{unique_id}_{safe_name}"
        else:  # uploads
            key = f"users/{uid}/external/{timestamp}_{unique_id}_{safe_name}"
        
        write_bytes_key(key, file_bytes)
        return key
        
    except Exception as ex:
        logger.exception(f"Save imported file error: {ex}")
        return None


async def _ensure_gdrive_token(uid: str, token_data: dict) -> Optional[str]:
    """Ensure Google Drive token is valid."""
    from routers.google_drive import _ensure_valid_token
    return await _ensure_valid_token(uid, token_data)


async def _ensure_dropbox_token(uid: str, token_data: dict) -> Optional[str]:
    """Ensure Dropbox token is valid."""
    from routers.dropbox import _ensure_valid_token
    return await _ensure_valid_token(uid, token_data)


async def _ensure_onedrive_token(uid: str, token_data: dict) -> Optional[str]:
    """Ensure OneDrive token is valid."""
    from routers.onedrive import _ensure_valid_token
    return await _ensure_valid_token(uid, token_data)
