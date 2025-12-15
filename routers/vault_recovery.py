"""
Vault Recovery Router - Trash and Version History Management
Provides soft-delete, trash recovery, and point-in-time vault restoration
"""
from typing import List, Optional
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, Body, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, desc

from core.config import logger, s3, R2_BUCKET
from core.auth import get_uid_from_request, resolve_workspace_uid, has_role_access
from core.database import get_db
from models.vault_trash import VaultTrash, VaultVersion
from models.gallery import GalleryAsset

router = APIRouter(prefix="/api", tags=["vault-recovery"])

# Constants
TRASH_RETENTION_DAYS = 30
MAX_VERSIONS_PER_VAULT = 20


def _vault_key(uid: str, vault: str) -> tuple:
    """Generate storage key for vault JSON file"""
    safe = "".join(c for c in vault if c.isalnum() or c in ("-", "_", " ")).strip().replace(" ", "_")
    if not safe:
        raise ValueError("invalid vault name")
    return f"users/{uid}/vaults/{safe}.json", safe


def _vault_meta_key(uid: str, vault: str) -> str:
    """Generate storage key for vault metadata"""
    _, safe = _vault_key(uid, vault)
    return f"users/{uid}/vaults/_meta/{safe}.json"


# ============== TRASH ENDPOINTS ==============

@router.get("/vault-recovery/trash")
async def list_trash(request: Request, db: Session = Depends(get_db)):
    """List all vaults in trash for the current user"""
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    
    try:
        items = db.query(VaultTrash).filter(
            VaultTrash.owner_uid == eff_uid,
            VaultTrash.restored_at.is_(None),
            VaultTrash.expires_at > datetime.utcnow()
        ).order_by(desc(VaultTrash.deleted_at)).all()
        
        result = []
        for item in items:
            days_remaining = max(0, (item.expires_at - datetime.utcnow()).days)
            result.append({
                "id": item.id,
                "vaultName": item.vault_name,
                "displayName": item.display_name or item.vault_name.replace("_", " "),
                "photoCount": item.photo_count,
                "totalSize": item.total_size_bytes,
                "deletedAt": item.deleted_at.isoformat() if item.deleted_at else None,
                "expiresAt": item.expires_at.isoformat() if item.expires_at else None,
                "daysRemaining": days_remaining,
            })
        
        return {"items": result, "total": len(result)}
    except Exception as ex:
        logger.error(f"Failed to list trash: {ex}")
        return JSONResponse({"error": "Failed to list trash"}, status_code=500)


@router.post("/vault-recovery/trash/restore")
async def restore_from_trash(
    request: Request,
    trash_id: int = Body(..., embed=True),
    db: Session = Depends(get_db)
):
    """Restore a vault from trash"""
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    
    try:
        trash_item = db.query(VaultTrash).filter(
            VaultTrash.id == trash_id,
            VaultTrash.owner_uid == eff_uid,
            VaultTrash.restored_at.is_(None)
        ).first()
        
        if not trash_item:
            return JSONResponse({"error": "Trash item not found"}, status_code=404)
        
        # Check if vault name already exists
        from routers.vaults import _read_vault, _write_vault, _write_vault_meta
        existing_keys = _read_vault(eff_uid, trash_item.vault_name)
        
        if existing_keys:
            # Merge with existing vault
            original_keys = trash_item.original_keys or []
            merged_keys = sorted(set(existing_keys) | set(original_keys))
            _write_vault(eff_uid, trash_item.vault_name, merged_keys)
        else:
            # Restore vault with original keys
            _write_vault(eff_uid, trash_item.vault_name, trash_item.original_keys or [])
            if trash_item.vault_metadata:
                _write_vault_meta(eff_uid, trash_item.vault_name, trash_item.vault_metadata)
        
        # Mark as restored
        trash_item.restored_at = datetime.utcnow()
        db.commit()
        
        return {
            "ok": True,
            "vaultName": trash_item.vault_name,
            "photoCount": trash_item.photo_count,
            "message": f"Vault '{trash_item.display_name or trash_item.vault_name}' restored successfully"
        }
    except Exception as ex:
        db.rollback()
        logger.error(f"Failed to restore from trash: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/vault-recovery/trash/delete")
async def permanent_delete_from_trash(
    request: Request,
    trash_ids: List[int] = Body(..., embed=True),
    db: Session = Depends(get_db)
):
    """Permanently delete vaults from trash"""
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    
    try:
        deleted_count = db.query(VaultTrash).filter(
            VaultTrash.id.in_(trash_ids),
            VaultTrash.owner_uid == eff_uid
        ).delete(synchronize_session=False)
        db.commit()
        
        return {"ok": True, "deleted": deleted_count}
    except Exception as ex:
        db.rollback()
        logger.error(f"Failed to delete from trash: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


# ============== VERSION HISTORY ENDPOINTS ==============

@router.get("/vault-recovery/versions")
async def list_vault_versions(
    request: Request,
    vault: str,
    db: Session = Depends(get_db)
):
    """List all version snapshots for a vault"""
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    
    try:
        _, safe_vault = _vault_key(eff_uid, vault)
        
        versions = db.query(VaultVersion).filter(
            VaultVersion.owner_uid == eff_uid,
            VaultVersion.vault_name == safe_vault
        ).order_by(desc(VaultVersion.version_number)).limit(MAX_VERSIONS_PER_VAULT).all()
        
        result = [v.to_dict() for v in versions]
        return {"versions": result, "total": len(result)}
    except Exception as ex:
        logger.error(f"Failed to list versions: {ex}")
        return JSONResponse({"error": "Failed to list versions"}, status_code=500)


@router.post("/vault-recovery/versions/create")
async def create_vault_snapshot(
    request: Request,
    vault: str = Body(..., embed=True),
    description: Optional[str] = Body(None, embed=True),
    db: Session = Depends(get_db)
):
    """Create a manual snapshot of the current vault state"""
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    
    try:
        from routers.vaults import _read_vault, _read_vault_meta
        
        _, safe_vault = _vault_key(eff_uid, vault)
        keys = _read_vault(eff_uid, vault)
        meta = _read_vault_meta(eff_uid, vault)
        
        # Calculate total size
        total_size = db.query(func.sum(GalleryAsset.size_bytes)).filter(
            GalleryAsset.user_uid == eff_uid,
            GalleryAsset.key.in_(keys)
        ).scalar() or 0
        
        # Get next version number
        max_ver = db.query(func.max(VaultVersion.version_number)).filter(
            VaultVersion.owner_uid == eff_uid,
            VaultVersion.vault_name == safe_vault
        ).scalar() or 0
        
        # Create snapshot
        snapshot = VaultVersion(
            owner_uid=eff_uid,
            vault_name=safe_vault,
            version_number=max_ver + 1,
            snapshot_keys=keys,
            vault_metadata=meta or {},
            photo_count=len(keys),
            total_size_bytes=total_size,
            description=description or f"Manual snapshot"
        )
        db.add(snapshot)
        
        # Cleanup old versions (keep only MAX_VERSIONS_PER_VAULT)
        old_versions = db.query(VaultVersion).filter(
            VaultVersion.owner_uid == eff_uid,
            VaultVersion.vault_name == safe_vault
        ).order_by(desc(VaultVersion.version_number)).offset(MAX_VERSIONS_PER_VAULT).all()
        
        for old in old_versions:
            db.delete(old)
        
        db.commit()
        
        return {
            "ok": True,
            "version": snapshot.to_dict(),
            "message": f"Snapshot v{snapshot.version_number} created"
        }
    except Exception as ex:
        db.rollback()
        logger.error(f"Failed to create snapshot: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/vault-recovery/versions/restore")
async def restore_vault_version(
    request: Request,
    version_id: int = Body(..., embed=True),
    db: Session = Depends(get_db)
):
    """Restore a vault to a specific version snapshot"""
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    
    try:
        version = db.query(VaultVersion).filter(
            VaultVersion.id == version_id,
            VaultVersion.owner_uid == eff_uid
        ).first()
        
        if not version:
            return JSONResponse({"error": "Version not found"}, status_code=404)
        
        from routers.vaults import _read_vault, _write_vault, _write_vault_meta
        
        # Create a snapshot of current state before restoring
        current_keys = _read_vault(eff_uid, version.vault_name)
        if current_keys:
            current_meta = {}
            try:
                from routers.vaults import _read_vault_meta
                current_meta = _read_vault_meta(eff_uid, version.vault_name) or {}
            except:
                pass
            
            max_ver = db.query(func.max(VaultVersion.version_number)).filter(
                VaultVersion.owner_uid == eff_uid,
                VaultVersion.vault_name == version.vault_name
            ).scalar() or 0
            
            backup_snapshot = VaultVersion(
                owner_uid=eff_uid,
                vault_name=version.vault_name,
                version_number=max_ver + 1,
                snapshot_keys=current_keys,
                vault_metadata=current_meta,
                photo_count=len(current_keys),
                description=f"Auto-backup before restoring to v{version.version_number}"
            )
            db.add(backup_snapshot)
        
        # Restore the vault to the selected version
        _write_vault(eff_uid, version.vault_name, version.snapshot_keys or [])
        if version.vault_metadata:
            _write_vault_meta(eff_uid, version.vault_name, version.vault_metadata)
        
        db.commit()
        
        return {
            "ok": True,
            "vaultName": version.vault_name,
            "restoredVersion": version.version_number,
            "photoCount": version.photo_count,
            "message": f"Vault restored to version {version.version_number}"
        }
    except Exception as ex:
        db.rollback()
        logger.error(f"Failed to restore version: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.get("/vault-recovery/versions/{version_id}/preview")
async def preview_version(
    request: Request,
    version_id: int,
    db: Session = Depends(get_db)
):
    """Preview photos in a specific version snapshot"""
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    
    try:
        version = db.query(VaultVersion).filter(
            VaultVersion.id == version_id,
            VaultVersion.owner_uid == eff_uid
        ).first()
        
        if not version:
            return JSONResponse({"error": "Version not found"}, status_code=404)
        
        from utils.storage import get_presigned_url
        import os
        
        items = []
        for key in (version.snapshot_keys or [])[:50]:  # Limit preview to 50 items
            name = os.path.basename(key)
            url = get_presigned_url(key, expires_in=3600) or ""
            items.append({"key": key, "name": name, "url": url})
        
        return {
            "version": version.to_dict(),
            "items": items,
            "totalCount": len(version.snapshot_keys or [])
        }
    except Exception as ex:
        logger.error(f"Failed to preview version: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)
