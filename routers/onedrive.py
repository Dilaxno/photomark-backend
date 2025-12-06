"""
OneDrive Backup/Sync Router
Allows users to automatically sync their photos to OneDrive.
Uses Microsoft Graph API with OAuth 2.0.
Free tier: 5GB storage per user.
"""
from typing import List, Optional
import os
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Request, Body, Query, BackgroundTasks
from fastapi.responses import JSONResponse, RedirectResponse

from core.config import logger
from core.auth import get_uid_from_request
from utils.storage import read_json_key, write_json_key, read_bytes_key, list_keys

router = APIRouter(prefix="/api/onedrive", tags=["onedrive"])

# Frontend URL for redirects after OAuth
FRONTEND_URL = os.getenv("FRONTEND_ORIGIN", "https://photomark.cloud").split(",")[0].strip()

# Microsoft/OneDrive API configuration
ONEDRIVE_CLIENT_ID = os.getenv("ONEDRIVE_CLIENT_ID", "")
ONEDRIVE_CLIENT_SECRET = os.getenv("ONEDRIVE_CLIENT_SECRET", "")
ONEDRIVE_REDIRECT_URI = os.getenv("ONEDRIVE_REDIRECT_URI", "")
MICROSOFT_AUTH_BASE = "https://login.microsoftonline.com/common/oauth2/v2.0"
GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"

# Scopes needed for OneDrive access
ONEDRIVE_SCOPES = [
    "Files.ReadWrite",  # Read and write files
    "User.Read",  # Get user profile (email, name)
    "offline_access",  # Get refresh token
]


def _onedrive_token_key(uid: str) -> str:
    return f"users/{uid}/integrations/onedrive.json"


def _onedrive_settings_key(uid: str) -> str:
    return f"users/{uid}/integrations/onedrive_settings.json"


def _onedrive_state_key(state: str) -> str:
    return f"oauth/onedrive/{state}.json"


def _onedrive_sync_log_key(uid: str) -> str:
    return f"users/{uid}/integrations/onedrive_sync_log.json"


@router.get("/status")
async def onedrive_status(request: Request):
    """Check if user has connected their OneDrive account."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if not ONEDRIVE_CLIENT_ID:
        return {"connected": False, "configured": False}

    try:
        token_data = read_json_key(_onedrive_token_key(uid))
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
        settings = read_json_key(_onedrive_settings_key(uid)) or {}

        return {
            "connected": True,
            "configured": True,
            "email": token_data.get("email"),
            "display_name": token_data.get("display_name"),
            "folder_id": token_data.get("folder_id"),
            "folder_name": token_data.get("folder_name", "Photomark Backup"),
            "auto_sync": settings.get("auto_sync", False),
            "sync_uploads": settings.get("sync_uploads", True),
            "sync_gallery": settings.get("sync_gallery", True),
            "sync_vaults": settings.get("sync_vaults", False),
        }
    except Exception as ex:
        logger.warning(f"OneDrive status check failed: {ex}")
        return {"connected": False, "configured": True}


@router.get("/auth")
async def onedrive_auth(request: Request):
    """Initiate Microsoft OAuth flow for OneDrive access."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    if not ONEDRIVE_CLIENT_ID or not ONEDRIVE_REDIRECT_URI:
        return JSONResponse({"error": "OneDrive integration not configured"}, status_code=500)

    state = secrets.token_urlsafe(32)

    write_json_key(_onedrive_state_key(state), {
        "uid": uid,
        "created_at": datetime.utcnow().isoformat()
    })

    params = {
        "client_id": ONEDRIVE_CLIENT_ID,
        "redirect_uri": ONEDRIVE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(ONEDRIVE_SCOPES),
        "state": state,
        "response_mode": "query",
    }
    auth_url = f"{MICROSOFT_AUTH_BASE}/authorize?{urlencode(params)}"

    return {"auth_url": auth_url}


@router.get("/callback")
async def onedrive_callback(
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    error_description: str = Query(None)
):
    """Handle Microsoft OAuth callback."""
    if error:
        logger.warning(f"OneDrive OAuth error: {error} - {error_description}")
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?onedrive_error=denied")

    if not code or not state:
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?onedrive_error=invalid")

    state_data = read_json_key(_onedrive_state_key(state))
    if not state_data or not state_data.get("uid"):
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?onedrive_error=invalid_state")

    uid = state_data["uid"]

    try:
        async with httpx.AsyncClient() as client:
            # Exchange code for tokens
            token_resp = await client.post(
                f"{MICROSOFT_AUTH_BASE}/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": ONEDRIVE_REDIRECT_URI,
                    "client_id": ONEDRIVE_CLIENT_ID,
                    "client_secret": ONEDRIVE_CLIENT_SECRET,
                    "scope": " ".join(ONEDRIVE_SCOPES),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )

            if token_resp.status_code != 200:
                logger.error(f"OneDrive token exchange failed: {token_resp.status_code}, {token_resp.text}")
                return RedirectResponse(url=f"{FRONTEND_URL}/integrations?onedrive_error=token_failed")

            tokens = token_resp.json()

            # Get user profile
            user_resp = await client.get(
                f"{GRAPH_API_BASE}/me",
                headers={"Authorization": f"Bearer {tokens['access_token']}"}
            )

            email = None
            display_name = None
            if user_resp.status_code == 200:
                user_data = user_resp.json()
                email = user_data.get("mail") or user_data.get("userPrincipalName")
                display_name = user_data.get("displayName")

            # Create Photomark backup folder in OneDrive
            folder_id, folder_name = await _create_or_get_folder(tokens["access_token"], "Photomark Backup")

        expires_in = tokens.get("expires_in", 3600)
        expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()

        write_json_key(_onedrive_token_key(uid), {
            "access_token": tokens["access_token"],
            "refresh_token": tokens.get("refresh_token"),
            "expires_at": expires_at,
            "email": email,
            "display_name": display_name,
            "folder_id": folder_id,
            "folder_name": folder_name or "Photomark Backup",
            "connected_at": datetime.utcnow().isoformat()
        })

        # Initialize default settings
        write_json_key(_onedrive_settings_key(uid), {
            "auto_sync": False,
            "sync_uploads": True,
            "sync_gallery": True,
            "sync_vaults": False,
        })

        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?onedrive_connected=true")

    except Exception as ex:
        logger.exception(f"OneDrive callback error: {ex}")
        return RedirectResponse(url=f"{FRONTEND_URL}/integrations?onedrive_error=unknown")


@router.post("/disconnect")
async def onedrive_disconnect(request: Request):
    """Disconnect OneDrive account."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        write_json_key(_onedrive_token_key(uid), {})
        write_json_key(_onedrive_settings_key(uid), {})
        return {"ok": True}
    except Exception as ex:
        logger.error(f"OneDrive disconnect failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/settings")
async def onedrive_update_settings(
    request: Request,
    auto_sync: bool = Body(False),
    sync_uploads: bool = Body(True),
    sync_gallery: bool = Body(True),
    sync_vaults: bool = Body(False),
):
    """Update OneDrive sync settings."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    token_data = read_json_key(_onedrive_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "OneDrive not connected"}, status_code=401)

    settings = {
        "auto_sync": auto_sync,
        "sync_uploads": sync_uploads,
        "sync_gallery": sync_gallery,
        "sync_vaults": sync_vaults,
        "updated_at": datetime.utcnow().isoformat()
    }

    write_json_key(_onedrive_settings_key(uid), settings)
    return {"ok": True, "settings": settings}


@router.post("/sync")
async def onedrive_sync_now(
    request: Request,
    background_tasks: BackgroundTasks,
    source: str = Body("uploads"),
    keys: Optional[List[str]] = Body(None),
):
    """Manually trigger sync to OneDrive."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    token_data = read_json_key(_onedrive_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "OneDrive not connected"}, status_code=401)

    access_token = await _ensure_valid_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "OneDrive token expired"}, status_code=401)

    folder_id = token_data.get("folder_id")
    if not folder_id:
        return JSONResponse({"error": "No backup folder configured"}, status_code=400)

    background_tasks.add_task(_sync_photos_to_onedrive, uid, access_token, folder_id, source, keys)

    return {"ok": True, "message": "Sync started in background"}


@router.post("/upload")
async def onedrive_upload_photos(
    request: Request,
    keys: List[str] = Body(...),
    folder_name: Optional[str] = Body(None),
):
    """Upload specific photos to OneDrive."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    token_data = read_json_key(_onedrive_token_key(uid))
    if not token_data or not token_data.get("access_token"):
        return JSONResponse({"error": "OneDrive not connected"}, status_code=401)

    access_token = await _ensure_valid_token(uid, token_data)
    if not access_token:
        return JSONResponse({"error": "OneDrive token expired"}, status_code=401)

    if not keys:
        return JSONResponse({"error": "No photos selected"}, status_code=400)

    valid_keys = [k for k in keys if k.startswith(f"users/{uid}/")]
    if not valid_keys:
        return JSONResponse({"error": "No valid photos found"}, status_code=400)

    parent_folder_id = token_data.get("folder_id")

    if folder_name:
        subfolder_id, _ = await _create_or_get_folder(access_token, folder_name, parent_folder_id)
        parent_folder_id = subfolder_id or parent_folder_id

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

            file_id = await _upload_file_to_onedrive(
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
async def onedrive_sync_status(request: Request):
    """Get last sync status and history."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    sync_log = read_json_key(_onedrive_sync_log_key(uid)) or {}

    return {
        "last_sync": sync_log.get("last_sync"),
        "last_sync_count": sync_log.get("last_sync_count", 0),
        "total_synced": sync_log.get("total_synced", 0),
        "last_error": sync_log.get("last_error"),
    }


# ============ Helper Functions ============

async def _create_or_get_folder(
    access_token: str,
    folder_name: str,
    parent_id: str = None
) -> tuple[Optional[str], Optional[str]]:
    """Create a folder in OneDrive or return existing one."""
    try:
        async with httpx.AsyncClient() as client:
            # Build the path for the API call
            if parent_id:
                base_url = f"{GRAPH_API_BASE}/me/drive/items/{parent_id}/children"
            else:
                base_url = f"{GRAPH_API_BASE}/me/drive/root/children"

            # Check if folder exists
            search_resp = await client.get(
                base_url,
                params={"$filter": f"name eq '{folder_name}' and folder ne null"},
                headers={"Authorization": f"Bearer {access_token}"}
            )

            if search_resp.status_code == 200:
                items = search_resp.json().get("value", [])
                for item in items:
                    if item.get("name") == folder_name and "folder" in item:
                        return item.get("id"), item.get("name")

            # Create folder
            create_resp = await client.post(
                base_url,
                json={
                    "name": folder_name,
                    "folder": {},
                    "@microsoft.graph.conflictBehavior": "fail"
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json"
                }
            )

            if create_resp.status_code in (200, 201):
                data = create_resp.json()
                return data.get("id"), data.get("name")

            # If conflict (folder exists), try to get it
            if create_resp.status_code == 409:
                search_resp2 = await client.get(
                    base_url,
                    params={"$filter": f"name eq '{folder_name}'"},
                    headers={"Authorization": f"Bearer {access_token}"}
                )
                if search_resp2.status_code == 200:
                    items = search_resp2.json().get("value", [])
                    if items:
                        return items[0].get("id"), items[0].get("name")

            logger.warning(f"Create folder response: {create_resp.status_code} - {create_resp.text}")
            return None, None

    except Exception as ex:
        logger.exception(f"Create folder error: {ex}")
        return None, None


async def _upload_file_to_onedrive(
    access_token: str,
    file_bytes: bytes,
    filename: str,
    content_type: str,
    parent_folder_id: str
) -> Optional[str]:
    """Upload a file to OneDrive."""
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # For files < 4MB, use simple upload
            if len(file_bytes) < 4 * 1024 * 1024:
                upload_url = f"{GRAPH_API_BASE}/me/drive/items/{parent_folder_id}:/{filename}:/content"

                resp = await client.put(
                    upload_url,
                    content=file_bytes,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Content-Type": content_type,
                    }
                )

                if resp.status_code in (200, 201):
                    return resp.json().get("id")

                logger.error(f"OneDrive upload failed: {resp.status_code} - {resp.text}")
                return None

            # For larger files, use upload session
            session_url = f"{GRAPH_API_BASE}/me/drive/items/{parent_folder_id}:/{filename}:/createUploadSession"

            session_resp = await client.post(
                session_url,
                json={
                    "item": {
                        "@microsoft.graph.conflictBehavior": "rename",
                        "name": filename
                    }
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json"
                }
            )

            if session_resp.status_code not in (200, 201):
                logger.error(f"OneDrive session creation failed: {session_resp.status_code}")
                return None

            upload_url = session_resp.json().get("uploadUrl")
            if not upload_url:
                return None

            # Upload in chunks
            file_size = len(file_bytes)
            chunk_size = 10 * 1024 * 1024  # 10MB chunks

            for start in range(0, file_size, chunk_size):
                end = min(start + chunk_size, file_size)
                chunk = file_bytes[start:end]

                chunk_resp = await client.put(
                    upload_url,
                    content=chunk,
                    headers={
                        "Content-Length": str(len(chunk)),
                        "Content-Range": f"bytes {start}-{end-1}/{file_size}",
                    }
                )

                if chunk_resp.status_code in (200, 201):
                    return chunk_resp.json().get("id")
                elif chunk_resp.status_code != 202:
                    logger.error(f"OneDrive chunk upload failed: {chunk_resp.status_code}")
                    return None

            return None

    except Exception as ex:
        logger.exception(f"OneDrive upload error: {ex}")
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


async def _sync_photos_to_onedrive(
    uid: str,
    access_token: str,
    folder_id: str,
    source: str,
    specific_keys: Optional[List[str]] = None
):
    """Background task to sync photos to OneDrive."""
    try:
        sync_log = read_json_key(_onedrive_sync_log_key(uid)) or {}
        synced_keys = set(sync_log.get("synced_keys", []))

        keys_to_sync = []

        if specific_keys:
            keys_to_sync = [k for k in specific_keys if k.startswith(f"users/{uid}/")]
        else:
            if source in ("uploads", "all"):
                uploads_keys = list_keys(f"users/{uid}/external/") or []
                keys_to_sync.extend(uploads_keys)

            if source in ("gallery", "all"):
                gallery_keys = list_keys(f"users/{uid}/watermarked/") or []
                keys_to_sync.extend(gallery_keys)

        new_keys = [k for k in keys_to_sync if k not in synced_keys]

        if not new_keys:
            sync_log["last_sync"] = datetime.utcnow().isoformat()
            sync_log["last_sync_count"] = 0
            write_json_key(_onedrive_sync_log_key(uid), sync_log)
            return

        subfolder_map = {}
        uploaded_count = 0

        for key in new_keys[:100]:
            try:
                if "/external/" in key:
                    subfolder_name = "My Uploads"
                elif "/watermarked/" in key:
                    subfolder_name = "Gallery"
                else:
                    subfolder_name = "Other"

                if subfolder_name not in subfolder_map:
                    subfolder_id, _ = await _create_or_get_folder(access_token, subfolder_name, folder_id)
                    subfolder_map[subfolder_name] = subfolder_id

                parent_id = subfolder_map.get(subfolder_name, folder_id)

                image_bytes = read_bytes_key(key)
                if not image_bytes:
                    continue

                filename = key.split("/")[-1]
                content_type = _get_content_type(filename)

                file_id = await _upload_file_to_onedrive(
                    access_token, image_bytes, filename, content_type, parent_id
                )

                if file_id:
                    synced_keys.add(key)
                    uploaded_count += 1

            except Exception as ex:
                logger.warning(f"Sync failed for {key}: {ex}")
                continue

        sync_log["last_sync"] = datetime.utcnow().isoformat()
        sync_log["last_sync_count"] = uploaded_count
        sync_log["total_synced"] = len(synced_keys)
        sync_log["synced_keys"] = list(synced_keys)[-1000]
        sync_log["last_error"] = None
        write_json_key(_onedrive_sync_log_key(uid), sync_log)

        logger.info(f"OneDrive sync completed for {uid}: {uploaded_count} files uploaded")

    except Exception as ex:
        logger.exception(f"OneDrive sync error for {uid}: {ex}")
        sync_log = read_json_key(_onedrive_sync_log_key(uid)) or {}
        sync_log["last_error"] = str(ex)
        sync_log["last_sync"] = datetime.utcnow().isoformat()
        write_json_key(_onedrive_sync_log_key(uid), sync_log)


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
    """Refresh Microsoft access token."""
    if not refresh_token or not ONEDRIVE_CLIENT_ID or not ONEDRIVE_CLIENT_SECRET:
        return None

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{MICROSOFT_AUTH_BASE}/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": ONEDRIVE_CLIENT_ID,
                    "client_secret": ONEDRIVE_CLIENT_SECRET,
                    "scope": " ".join(ONEDRIVE_SCOPES),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"}
            )

            if resp.status_code != 200:
                logger.warning(f"OneDrive token refresh failed: {resp.status_code}")
                return None

            tokens = resp.json()
            expires_in = tokens.get("expires_in", 3600)
            expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()

            existing = read_json_key(_onedrive_token_key(uid)) or {}
            existing.update({
                "access_token": tokens["access_token"],
                "expires_at": expires_at
            })
            if tokens.get("refresh_token"):
                existing["refresh_token"] = tokens["refresh_token"]

            write_json_key(_onedrive_token_key(uid), existing)

            return tokens["access_token"]

    except Exception as ex:
        logger.exception(f"OneDrive token refresh error: {ex}")
        return None


# ============ Auto-sync Hook ============

async def trigger_auto_sync_if_enabled(uid: str, keys: List[str]):
    """Trigger auto-sync if user has it enabled. Call this after uploads."""
    try:
        settings = read_json_key(_onedrive_settings_key(uid)) or {}
        if not settings.get("auto_sync"):
            return

        token_data = read_json_key(_onedrive_token_key(uid))
        if not token_data or not token_data.get("access_token"):
            return

        access_token = await _ensure_valid_token(uid, token_data)
        if not access_token:
            return

        folder_id = token_data.get("folder_id")
        if not folder_id:
            return

        await _sync_photos_to_onedrive(uid, access_token, folder_id, "uploads", keys)

    except Exception as ex:
        logger.warning(f"OneDrive auto-sync trigger failed for {uid}: {ex}")
