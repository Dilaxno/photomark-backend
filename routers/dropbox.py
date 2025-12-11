"""
Dropbox Backup/Sync Router
Allows users to automatically sync their photos to Dropbox.
Uses Dropbox API v2 with OAuth 2.0.
Free tier: 2GB storage per user.
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

router = APIRouter(prefix="/api/dropbox", tags=["dropbox"])

# Frontend URL for redirects after OAuth
FRONTEND_URL = os.getenv("FRONTEND_ORIGIN", "https://photomark.cloud").split(",")[0].strip()

# Dropbox API configuration
DROPBOX_APP_KEY = os.getenv("DROPBOX_APP_KEY", "")
DROPBOX_APP_SECRET = os.getenv("DROPBOX_APP_SECRET", "")
DROPBOX_REDIRECT_URI = os.getenv("DROPBOX_REDIRECT_URI", "")
DROPBOX_OAUTH_BASE = "https://www.dropbox.com/oauth2"
DROPBOX_API_BASE = "https://api.dropboxapi.com/2"
DROPBOX_CONTENT_BASE = "https://content.dropboxapi.com/2"


def _dropbox_token_key(uid: str) -> str:
    return f"users/{uid}/integrations/dropbox.json"


def _dropbox_settings_key(uid: str) -> str:
    return f"users/{uid}/integrations/dropbox_settings.json"


def _dropbox_state_key(state: str) -> str:
    return f"oauth/dropbox/{state}.json"


def _dropbox_sync_log_key(uid: str) -> str:
    return f"users/{uid}/integrations/dropbox_sync_log.json"


@router.get("/status")
async def dropbox_status(request: Request):
    """Check if user has connected their Dropbox account."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if not DROPBOX_APP_KEY:
        return {"connected": False, "configured": False}

    try:
        token_data = read_json_key(_dropbox_token_key(uid))
        if not token_data or not token_data.get("access_token"):
            return {"connected": False, "configured": True}

        # Check if token is expired (Dropbox tokens can expire)
        expires_at = token_data.get("expires_at")
        if expires_at:
            exp_dt = datetime.fromisoformat(expires_at)
            if datetime.utcnow() > exp_dt:
                # Try to refresh
                refreshed = await _refresh_token(uid, token_data.get("refresh_token"))
                if not refreshed:
                    return {"connected": False, "configured": True, "expired": True}

        # Get sync settings
        settings = read_json_key(_dropbox_settings_key(uid)) or {}

        return {
            "connected": True,
            "configured": True,
            "email": token_data.get("email"),
            "account_id": token_data.get("account_id"),
            "display_name": token_data.get("display_name"),
            "folder_path": token_data.get("folder_path", "/Photomark Backup"),
            "auto_sync": settings.get("auto_sync", False),
            "sync_uploads": settings.get("sync_uploads", True),
            "sync_gallery": settings.get("sync_gallery", True),
            "sync_vaults": settings.get("sync_vaults", False),
        }
    except Exception as ex:
        logger.warning(f"Dropbox status check failed: {ex}")
        return {"connected": False, "configured": True}


@router.get("/auth")
async def dropbox_auth(request: Request):
    """Initiate Dropbox OAuth flow."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if not DROPBOX_APP_KEY or not DROPBOX_REDIRECT_URI:
        return JSONResponse({"error": "Dropbox integration not configured"}, status_code=500)

    state = secrets.token_urlsafe(32)

    write_json_key(_dropbox_state_key(state), {
        "uid": uid,
        "created_at": datetime.utcnow().isoformat()
    })

    params = {
        "client_id": DROPBOX_APP_KEY,
        "redirect_uri": DROPBOX_REDIRECT_URI,
        "response_type": "code",
        "state": state,
        "token_access_type": "offline",  # Get refresh token
    }
    auth_url = f"{DROPBOX_OAUTH_BASE}/authorize?{urlencode(params)}"

    return {"auth_url": auth_url}


@router.get("/callback")
async def dropbox_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    error_description: str = Query(None)
):
    """Handle Dropbox OAuth callback."""
    if error:
        logger.warning(f"Dropbox OAuth error: {error} - {error_description}")
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?dropbox_error=denied")

    if not code or not state:
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?dropbox_error=invalid")

    state_data = read_json_key(_dropbox_state_key(state))
    if not state_data or not state_data.get("uid"):
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?dropbox_error=invalid_state")

    uid = state_data["uid"]

    try:
        async with httpx.AsyncClient() as client:
            # Exchange code for tokens
            token_resp = await client.post(
                f"{DROPBOX_OAUTH_BASE}/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": DROPBOX_REDIRECT_URI,
                    "client_id": DROPBOX_APP_KEY,
                    "client_secret": DROPBOX_APP_SECRET
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )

            if token_resp.status_code != 200:
                logger.error(f"Dropbox token exchange failed: {token_resp.status_code}, {token_resp.text}")
                return RedirectResponse(url=f"{FRONTEND_URL}/integrations?dropbox_error=token_failed")

            tokens = token_resp.json()

            # Get user account info
            account_resp = await client.post(
                f"{DROPBOX_API_BASE}/users/get_current_account",
                headers={"Authorization": f"Bearer {tokens['access_token']}"}
            )

            email = None
            account_id = None
            display_name = None
            if account_resp.status_code == 200:
                account_data = account_resp.json()
                email = account_data.get("email")
                account_id = account_data.get("account_id")
                display_name = account_data.get("name", {}).get("display_name")

            # Create Photomark backup folder
            folder_path = await _create_folder_if_not_exists(tokens["access_token"], "/Photomark Backup")

        expires_in = tokens.get("expires_in")
        expires_at = None
        if expires_in:
            expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()

        write_json_key(_dropbox_token_key(uid), {
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token"),
            "expires_at": expires_at,
            "email": email,
            "account_id": account_id,
            "display_name": display_name,
            "folder_path": folder_path,
            "connected_at": datetime.utcnow().isoformat()
        })

        # Initialize default settings
        write_json_key(_dropbox_settings_key(uid), {
            "auto_sync": False,
            "sync_uploads": True,
            "sync_gallery": True,
            "sync_vaults": False,
        })

        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?dropbox_connected=true")

    except Exception as ex:
        logger.exception(f"Dropbox callback error: {ex}")
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?dropbox_error=unknown")


@router.post("/disconnect")
async def dropbox_disconnect(request: Request):
    """Disconnect Dropbox account."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        # Optionally revoke the token
        token_data = read_json_key(_dropbox_token_key(uid))
        if token_data and token_data.get("access_token"):
            try:
                async with httpx.AsyncClient() as client:
                    await client.post(
                        f"{DROPBOX_API_BASE}/auth/token/revoke",
                        headers={"Authorization": f"Bearer {token_data['access_token']}"}
                    )
            except Exception:
                pass  # Token revocation is best-effort

        write_json_key(_dropbox_token_key(uid), {})
        write_json_key(_dropbox_settings_key(uid), {})
        return {"ok": True}
    except Exception as ex:
        logger.error(f"Dropbox disconnect failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/settings")
async def dropbox_update_settings(
    request: Request,
    auto_sync: bool = Body(False),
    sync_uploads: bool = Body(True),
    sync_gallery: bool = Body(True),
    sync_vaults: bool = Body(False),
):
    """Update Dropbox sync settings."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    token_data = read_json_key(_dropbox_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Dropbox not connected"}, status_code=401)

    settings = {
        "auto_sync": auto_sync,
        "sync_uploads": sync_uploads,
        "sync_gallery": sync_gallery,
        "sync_vaults": sync_vaults,
        "updated_at": datetime.utcnow().isoformat()
    }

    write_json_key(_dropbox_settings_key(uid), settings)
    return {"ok": True, "settings": settings}


@router.post("/sync")
async def dropbox_sync_now(
    request: Request,
    background_tasks: BackgroundTasks,
    source: str = Body("uploads"),  # uploads, gallery, vaults, all
    keys: Optional[List[str]] = Body(None),  # Specific keys to sync, or None for all
):
    """Manually trigger sync to Dropbox."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    token_data = read_json_key(_dropbox_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Dropbox not connected"}, status_code=401)

    access_token = await _ensure_valid_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "Dropbox token expired"}, status_code=401)

    folder_path = token_data.get("folder_path", "/Photomark Backup")

    # Start background sync
    background_tasks.add_task(_sync_photos_to_dropbox, uid, access_token, folder_path, source, keys)

    return {"ok": True, "message": "Sync started in background"}


@router.post("/upload")
async def dropbox_upload_photos(
    request: Request,
    keys: List[str] = Body(...),
    folder_name: Optional[str] = Body(None),  # Create subfolder with this name
):
    """Upload specific photos to Dropbox."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    token_data = read_json_key(_dropbox_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Dropbox not connected"}, status_code=401)

    access_token = await _ensure_valid_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "Dropbox token expired"}, status_code=401)

    if not keys:
        return JSONResponse({"error": "No photos selected"}, status_code=400)

    # Validate keys belong to user
    valid_keys = [k for k in keys if k.startswith(f"users/{uid}/")]
    if not valid_keys:
        return JSONResponse({"error": "No valid photos found"}, status_code=400)

    base_folder = token_data.get("folder_path", "/Photomark Backup")

    # Create subfolder if requested
    if folder_name:
        target_folder = f"{base_folder}/{folder_name}"
        await _create_folder_if_not_exists(access_token, target_folder)
    else:
        target_folder = base_folder

    uploaded = []
    failed = []

    for key in valid_keys:
        try:
            image_bytes = read_bytes_key(key)
            if not image_bytes:
                failed.append({"key": key, "error": "Could not read file"})
                continue

            filename = key.split("/")[-1]
            file_path = f"{target_folder}/{filename}"

            success = await _upload_file_to_dropbox(access_token, image_bytes, file_path)

            if success:
                uploaded.append({"key": key, "path": file_path, "filename": filename})
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
async def dropbox_sync_status(request: Request):
    """Get last sync status and history."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    sync_log = read_json_key(_dropbox_sync_log_key(uid)) or {}

    return {
        "last_sync": sync_log.get("last_sync"),
        "last_sync_count": sync_log.get("last_sync_count", 0),
        "total_synced": sync_log.get("total_synced", 0),
        "last_error": sync_log.get("last_error"),
    }


@router.get("/folders")
async def dropbox_list_folders(
    request: Request,
    path: str = Query(""),  # Empty string = root
):
    """List all folders in user's Dropbox."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    token_data = read_json_key(_dropbox_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Dropbox not connected"}, status_code=401)

    access_token = await _ensure_valid_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "Dropbox token expired"}, status_code=401)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{DROPBOX_API_BASE}/files/list_folder",
                json={
                    "path": path if path else "",
                    "recursive": False,
                    "include_deleted": False,
                    "include_has_explicit_shared_members": False,
                    "include_mounted_folders": True,
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json"
                }
            )

            if resp.status_code != 200:
                return JSONResponse({"error": "Failed to list folders"}, status_code=500)

            data = resp.json()
            entries = data.get("entries", [])
            
            # Filter to only folders
            folders = [
                {
                    "id": e.get("id"),
                    "name": e.get("name"),
                    "path": e.get("path_display"),
                }
                for e in entries if e.get(".tag") == "folder"
            ]
            
            return {"folders": folders, "path": path}

    except Exception as ex:
        logger.exception(f"Dropbox folders error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.get("/files")
async def dropbox_list_files(
    request: Request,
    path: str = Query(""),  # Empty string = root
    cursor: str = Query(None),
):
    """List image files in a Dropbox folder for importing."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    token_data = read_json_key(_dropbox_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Dropbox not connected"}, status_code=401)

    access_token = await _ensure_valid_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "Dropbox token expired"}, status_code=401)

    try:
        async with httpx.AsyncClient() as client:
            if cursor:
                # Continue from cursor
                resp = await client.post(
                    f"{DROPBOX_API_BASE}/files/list_folder/continue",
                    json={"cursor": cursor},
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json"
                    }
                )
            else:
                resp = await client.post(
                    f"{DROPBOX_API_BASE}/files/list_folder",
                    json={
                        "path": path if path else "",
                        "recursive": False,
                        "include_deleted": False,
                    },
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": "application/json"
                    }
                )

            if resp.status_code != 200:
                return JSONResponse({"error": "Failed to list files"}, status_code=500)

            data = resp.json()
            entries = data.get("entries", [])
            has_more = data.get("has_more", False)
            next_cursor = data.get("cursor") if has_more else None
            
            # Filter to only image files
            image_extensions = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".tiff", ".tif", ".bmp"}
            files = []
            for e in entries:
                if e.get(".tag") == "file":
                    name = e.get("name", "")
                    ext = os.path.splitext(name)[1].lower()
                    if ext in image_extensions:
                        files.append({
                            "id": e.get("id"),
                            "name": name,
                            "path": e.get("path_display"),
                            "size": e.get("size"),
                            "modified": e.get("client_modified"),
                        })
            
            return {
                "files": files,
                "path": path,
                "cursor": next_cursor,
                "has_more": has_more
            }

    except Exception as ex:
        logger.exception(f"Dropbox files error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/import")
async def dropbox_import_files(
    request: Request,
    file_paths: List[str] = Body(...),
):
    """Import/download files from Dropbox to user's gallery."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    token_data = read_json_key(_dropbox_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "Dropbox not connected"}, status_code=401)

    access_token = await _ensure_valid_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "Dropbox token expired"}, status_code=401)

    if not file_paths:
        return JSONResponse({"error": "No files selected"}, status_code=400)

    if len(file_paths) > 50:
        return JSONResponse({"error": "Maximum 50 files per import"}, status_code=400)

    imported = []
    failed = []

    try:
        import json
        async with httpx.AsyncClient(timeout=60.0) as client:
            for file_path in file_paths:
                try:
                    # Download file content
                    dropbox_arg = json.dumps({"path": file_path})
                    download_resp = await client.post(
                        f"{DROPBOX_CONTENT_BASE}/files/download",
                        headers={
                            "Authorization": f"Bearer {access_token}",
                            "Dropbox-API-Arg": dropbox_arg
                        }
                    )

                    if download_resp.status_code != 200:
                        failed.append({"path": file_path, "error": "Download failed"})
                        continue

                    file_bytes = download_resp.content
                    
                    # Get filename from path
                    filename = file_path.split("/")[-1] if "/" in file_path else file_path

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
                        "path": file_path,
                        "filename": filename,
                        "key": key,
                        "url": url
                    })

                except Exception as ex:
                    logger.warning(f"Import failed for {file_path}: {ex}")
                    failed.append({"path": file_path, "error": str(ex)})

        return {
            "ok": True,
            "imported": len(imported),
            "failed": len(failed),
            "details": {"imported": imported, "failed": failed}
        }

    except Exception as ex:
        logger.exception(f"Dropbox import error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


# ============ Helper Functions ============

async def _create_folder_if_not_exists(access_token: str, folder_path: str) -> str:
    """Create a folder in Dropbox if it doesn't exist."""
    try:
        async with httpx.AsyncClient() as client:
            # Try to create the folder
            resp = await client.post(
                f"{DROPBOX_API_BASE}/files/create_folder_v2",
                json={"path": folder_path, "autorename": False},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json"
                }
            )

            if resp.status_code == 200:
                return folder_path

            # Check if folder already exists (conflict error)
            if resp.status_code == 409:
                error_data = resp.json()
                if error_data.get("error", {}).get(".tag") == "path" and \
                   error_data.get("error", {}).get("path", {}).get(".tag") == "conflict":
                    return folder_path

            logger.warning(f"Create folder response: {resp.status_code} - {resp.text}")
            return folder_path

    except Exception as ex:
        logger.exception(f"Create folder error: {ex}")
        return folder_path


async def _upload_file_to_dropbox(access_token: str, file_bytes: bytes, file_path: str) -> bool:
    """Upload a file to Dropbox."""
    try:
        import json
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Dropbox uses a special header for upload parameters
            dropbox_arg = json.dumps({
                "path": file_path,
                "mode": "overwrite",
                "autorename": True,
                "mute": False
            })

            resp = await client.post(
                f"{DROPBOX_CONTENT_BASE}/files/upload",
                content=file_bytes,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/octet-stream",
                    "Dropbox-API-Arg": dropbox_arg
                }
            )

            if resp.status_code == 200:
                return True

            logger.error(f"Dropbox upload failed: {resp.status_code} - {resp.text}")
            return False

    except Exception as ex:
        logger.exception(f"Dropbox upload error: {ex}")
        return False


async def _sync_photos_to_dropbox(
    uid: str,
    access_token: str,
    folder_path: str,
    source: str,
    specific_keys: Optional[List[str]] = None
):
    """Background task to sync photos to Dropbox."""
    try:
        sync_log = read_json_key(_dropbox_sync_log_key(uid)) or {}
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
            write_json_key(_dropbox_sync_log_key(uid), sync_log)
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
                    subfolder_path = f"{folder_path}/{subfolder_name}"
                    await _create_folder_if_not_exists(access_token, subfolder_path)
                    subfolder_map[subfolder_name] = subfolder_path

                target_folder = subfolder_map.get(subfolder_name, folder_path)

                # Read and upload file
                image_bytes = read_bytes_key(key)
                if not image_bytes:
                    continue

                filename = key.split("/")[-1]
                file_path = f"{target_folder}/{filename}"

                success = await _upload_file_to_dropbox(access_token, image_bytes, file_path)

                if success:
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
        write_json_key(_dropbox_sync_log_key(uid), sync_log)

        logger.info(f"Dropbox sync completed for {uid}: {uploaded_count} files uploaded")

    except Exception as ex:
        logger.exception(f"Dropbox sync error for {uid}: {ex}")
        sync_log = read_json_key(_dropbox_sync_log_key(uid)) or {}
        sync_log["last_error"] = str(ex)
        sync_log["last_sync"] = datetime.utcnow().isoformat()
        write_json_key(_dropbox_sync_log_key(uid), sync_log)


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
        except Exception:
            pass

    return access_token


async def _refresh_token(uid: str, refresh_token: str) -> Optional[str]:
    """Refresh Dropbox access token."""
    if not refresh_token or not DROPBOX_APP_KEY or not DROPBOX_APP_SECRET:
        return None

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{DROPBOX_OAUTH_BASE}/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": DROPBOX_APP_KEY,
                    "client_secret": DROPBOX_APP_SECRET
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )

            if resp.status_code != 200:
                logger.warning(f"Dropbox token refresh failed: {resp.status_code}")
                return None

            tokens = resp.json()
            expires_in = tokens.get("expires_in")
            expires_at = None
            if expires_in:
                expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()

            existing = read_json_key(_dropbox_token_key(uid)) or {}
            existing.update({
                "access_token": tokens["access_token"],
                "expires_at": expires_at
            })

            write_json_key(_dropbox_token_key(uid), existing)

            return tokens["access_token"]

    except Exception as ex:
        logger.exception(f"Dropbox token refresh error: {ex}")
        return None


# ============ Auto-sync Hook ============

async def trigger_auto_sync_if_enabled(uid: str, keys: List[str]):
    """Trigger auto-sync if user has it enabled. Call this after uploads."""
    try:
        settings = read_json_key(_dropbox_settings_key(uid)) or {}
        if not settings.get("auto_sync"):
            return

        token_data = read_json_key(_dropbox_token_key(uid))
        if not token_data or not token_data.get("access_token"):
            return

        access_token = await _ensure_valid_token(uid, token_data)
        if not access_token:
            return

        folder_path = token_data.get("folder_path", "/Photomark Backup")

        # Sync the specific keys
        await _sync_photos_to_dropbox(uid, access_token, folder_path, "uploads", keys)

    except Exception as ex:
        logger.warning(f"Dropbox auto-sync trigger failed for {uid}: {ex}")
