from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
from typing import Optional, List
import os
from datetime import datetime

from core.config import logger  # type: ignore
from core.config import s3_backup, BACKUP_BUCKET  # type: ignore
from core.auth import resolve_workspace_uid, has_role_access  # type: ignore
from utils.storage import backup_read_bytes_key, backup_delete_key, upload_bytes  # type: ignore

router = APIRouter(prefix="/api", tags=["backups"])


def _is_image_key(name: str) -> bool:
    n = (name or "").lower()
    return any(n.endswith(ext) for ext in [".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"]) and ("/_history.txt" not in n)


# Allowed subfolders for backup gallery (only user-uploaded photos)
# Excludes: profile photos, shop logos, brand kit, avatars, settings, billing
ALLOWED_BACKUP_PREFIXES = (
    "/watermarked/",   # Watermarked uploads from /upload
    "/external/",      # Raw uploads from /api/uploads (My Uploads)
    "/vaults/",        # Vault photos
    "/portfolio/",     # Portfolio photos
    "/gallery/",       # Gallery photos
    "/photos/",        # General photos
)

# Explicitly excluded paths (profile, branding, settings)
EXCLUDED_BACKUP_PREFIXES = (
    "/settings/",
    "/billing/",
    "/profile/",
    "/avatar/",
    "/logo/",
    "/brandkit/",
    "/brand-kit/",
    "/brand_kit/",
    "/pfp/",
    "/banner/",
    "/shop/",
)


def _is_allowed_backup_key(key: str) -> bool:
    """Check if a storage key should be included in backup gallery.
    Only includes actual user photos (uploads, vaults, gallery, portfolio).
    Excludes profile photos, shop logos, brand kit, settings, etc.
    """
    k = (key or "").lower()
    
    # First check exclusions
    for excl in EXCLUDED_BACKUP_PREFIXES:
        if excl in k:
            return False
    
    # Then check if it's in an allowed photo folder
    for allowed in ALLOWED_BACKUP_PREFIXES:
        if allowed in k:
            return True
    
    return False


@router.get("/backups/photos")
async def list_backup_photos(request: Request, limit: int = 100, cursor: Optional[str] = None):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    if not s3_backup or not BACKUP_BUCKET:
        return JSONResponse({"error": "Backup storage unavailable"}, status_code=503)

    uid = eff_uid
    prefix = f"users/{uid}/"
    try:
        client = s3_backup.meta.client
        params = {
            'Bucket': BACKUP_BUCKET,
            'Prefix': prefix,
            'MaxKeys': max(1, min(int(limit or 100), 1000)),
        }
        if cursor:
            params['ContinuationToken'] = cursor
        resp = client.list_objects_v2(**params)
        items = []
        for obj in resp.get('Contents', []) or []:
            key = obj.get('Key') or ''
            if not key or key.endswith('/'):
                continue
            name = os.path.basename(key)
            if not _is_image_key(name):
                continue
            # Filter: only include actual user photos, not profile/shop/branding assets
            if not _is_allowed_backup_key(key):
                continue
            lm = obj.get('LastModified')
            when = lm.isoformat() if hasattr(lm, 'isoformat') else (lm or datetime.utcnow()).isoformat()
            try:
                url = client.generate_presigned_url('get_object', Params={'Bucket': BACKUP_BUCKET, 'Key': key}, ExpiresIn=3600)
            except Exception:
                url = ''
            items.append({
                'key': key,
                'url': url,
                'name': name,
                'size': int(obj.get('Size') or 0),
                'last_modified': when,
            })
        next_token = resp.get('NextContinuationToken') or resp.get('ContinuationToken') or None
        return { 'items': items, 'next': next_token, 'total': len(items) }
    except Exception as ex:
        logger.warning(f"backup list failed: {ex}")
        return JSONResponse({"error": "Failed to list backups"}, status_code=500)


@router.post("/backups/restore")
async def restore_from_backup(request: Request, keys: List[str] = Body(..., embed=True)):
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    if not s3_backup or not BACKUP_BUCKET:
        return JSONResponse({"error": "Backup storage unavailable"}, status_code=503)

    uid = eff_uid
    restored: List[str] = []
    errors: List[str] = []
    for k in keys or []:
        key = (k or '').strip().lstrip('/')
        if not key.startswith(f"users/{uid}/"):
            errors.append(f"forbidden: {key}")
            continue
        try:
            data = backup_read_bytes_key(key)
            if not data:
                errors.append(f"missing: {key}")
                continue
            import mimetypes
            ct = mimetypes.guess_type(key)[0] or 'application/octet-stream'
            _ = upload_bytes(key, data, content_type=ct)
            restored.append(key)
        except Exception as ex:
            errors.append(f"{key}: {ex}")
    return { 'ok': True, 'restored': restored, 'errors': errors }


@router.post("/backups/delete")
async def delete_from_backup(request: Request, keys: List[str] = Body(..., embed=True)):
    """Permanently delete selected items from backup storage."""
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)

    if not s3_backup or not BACKUP_BUCKET:
        return JSONResponse({"error": "Backup storage unavailable"}, status_code=503)

    uid = eff_uid
    deleted: List[str] = []
    errors: List[str] = []
    for k in keys or []:
        key = (k or '').strip().lstrip('/')
        if not key.startswith(f"users/{uid}/"):
            errors.append(f"forbidden: {key}")
            continue
        try:
            ok = backup_delete_key(key)
            if ok:
                deleted.append(key)
            else:
                errors.append(f"failed: {key}")
        except Exception as ex:
            errors.append(f"{key}: {ex}")
    return { 'ok': True, 'deleted': deleted, 'errors': errors }
