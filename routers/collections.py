"""
Collections router for organizing photos into user-created collections.
Collections are stored in users/{uid}/collections/{collection_name}/ as symlinks/references.
"""
from fastapi import APIRouter, Request, Body, Depends
from fastapi.responses import JSONResponse
from typing import Optional, List
import os
import json
from datetime import datetime

from core.config import s3, R2_BUCKET, STATIC_DIR as static_dir, logger
from core.auth import resolve_workspace_uid, has_role_access
from utils.storage import read_json_key, write_json_key, get_presigned_url, read_bytes_key
from typing import Optional
from sqlalchemy.orm import Session
from core.database import get_db

router = APIRouter(prefix="/api/collections", tags=["collections"])


def _get_url_for_key(key: str, expires_in: int = 3600) -> str:
    return get_presigned_url(key, expires_in=expires_in) or ""


def _get_collections_index_key(uid: str) -> str:
    return f"users/{uid}/collections/_index.json"


def _get_collection_data_key(uid: str, collection_name: str) -> str:
    # Sanitize collection name
    safe_name = "".join(c for c in collection_name if c.isalnum() or c in (' ', '-', '_')).strip()
    safe_name = safe_name.replace(' ', '_')[:50]
    return f"users/{uid}/collections/{safe_name}/data.json"


def _get_thumbnail_url(key: str, expires_in: int = 3600) -> Optional[str]:
    """Get thumbnail URL for a key if it exists."""
    try:
        from utils.thumbnails import get_thumbnail_key
        thumb_key = get_thumbnail_key(key, 'small')
        # Check if thumbnail exists before generating URL
        if s3 and R2_BUCKET:
            try:
                s3.Object(R2_BUCKET, thumb_key).load()
                return get_presigned_url(thumb_key, expires_in=expires_in)
            except Exception:
                return None
        return None
    except Exception:
        return None


@router.get("")
async def list_collections(request: Request):
    """List all collections for the current user."""
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    
    uid = eff_uid
    index_key = _get_collections_index_key(uid)
    
    try:
        index_data = read_json_key(index_key)
        if not isinstance(index_data, dict):
            index_data = {"collections": []}
        
        collections = index_data.get("collections", [])
        
        # Enrich with photo counts and cover images
        enriched = []
        for coll in collections:
            name = coll.get("name", "")
            data_key = _get_collection_data_key(uid, name)
            coll_data = read_json_key(data_key)
            photos = coll_data.get("photos", []) if isinstance(coll_data, dict) else []
            photo_count = len(photos)
            
            # Get cover image (first photo in collection) - prefer thumbnail for faster loading
            cover_url = ""
            cover_key = ""
            if photos and len(photos) > 0:
                first_photo = photos[0]
                cover_key = first_photo.get("key", "")
                if cover_key:
                    # Try thumbnail first, fallback to full image
                    cover_url = _get_thumbnail_url(cover_key, expires_in=3600) or _get_url_for_key(cover_key, expires_in=3600)
            
            enriched.append({
                **coll,
                "photo_count": photo_count,
                "cover_url": cover_url,
                "cover_key": cover_key,
            })
        
        return {"collections": enriched}
    except Exception as ex:
        logger.exception(f"Failed to list collections for {uid}: {ex}")
        return {"collections": []}


@router.post("/create")
async def create_collection(request: Request, payload: dict = Body(...)):
    """Create a new collection."""
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    
    uid = eff_uid
    name = str(payload.get("name", "")).strip()
    description = str(payload.get("description", "")).strip()
    
    if not name:
        return JSONResponse({"error": "Collection name is required"}, status_code=400)
    
    if len(name) > 100:
        return JSONResponse({"error": "Collection name too long"}, status_code=400)
    
    index_key = _get_collections_index_key(uid)
    
    try:
        index_data = read_json_key(index_key)
        if not isinstance(index_data, dict):
            index_data = {"collections": []}
        
        collections = index_data.get("collections", [])
        
        # Check if collection already exists
        if any(c.get("name", "").lower() == name.lower() for c in collections):
            return JSONResponse({"error": "Collection already exists"}, status_code=409)
        
        # Create new collection entry
        new_collection = {
            "name": name,
            "description": description,
            "created_at": datetime.utcnow().isoformat(),
            "updated_at": datetime.utcnow().isoformat(),
        }
        
        collections.append(new_collection)
        index_data["collections"] = collections
        
        # Save index
        write_json_key(index_key, index_data)
        
        # Create empty collection data file
        data_key = _get_collection_data_key(uid, name)
        write_json_key(data_key, {"photos": [], "created_at": datetime.utcnow().isoformat()})
        
        return {"ok": True, "collection": new_collection}
    except Exception as ex:
        logger.exception(f"Failed to create collection for {uid}: {ex}")
        return JSONResponse({"error": "Failed to create collection"}, status_code=500)


@router.post("/delete")
async def delete_collection(request: Request, payload: dict = Body(...)):
    """Delete a collection (does not delete the actual photos)."""
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    
    uid = eff_uid
    name = str(payload.get("name", "")).strip()
    
    if not name:
        return JSONResponse({"error": "Collection name is required"}, status_code=400)
    
    index_key = _get_collections_index_key(uid)
    
    try:
        index_data = read_json_key(index_key)
        if not isinstance(index_data, dict):
            return JSONResponse({"error": "Collection not found"}, status_code=404)
        
        collections = index_data.get("collections", [])
        new_collections = [c for c in collections if c.get("name", "").lower() != name.lower()]
        
        if len(new_collections) == len(collections):
            return JSONResponse({"error": "Collection not found"}, status_code=404)
        
        index_data["collections"] = new_collections
        write_json_key(index_key, index_data)
        
        # Optionally delete the collection data file
        # For now, we keep it as a soft delete
        
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"Failed to delete collection for {uid}: {ex}")
        return JSONResponse({"error": "Failed to delete collection"}, status_code=500)


@router.get("/{collection_name}/photos")
async def get_collection_photos(request: Request, collection_name: str):
    """Get all photos in a collection."""
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    
    uid = eff_uid
    data_key = _get_collection_data_key(uid, collection_name)
    
    try:
        coll_data = read_json_key(data_key)
        if not isinstance(coll_data, dict):
            return JSONResponse({"error": "Collection not found"}, status_code=404)
        
        photo_refs = coll_data.get("photos", [])
        
        # Resolve photo references to actual photo data
        photos = []
        for ref in photo_refs:
            key = ref.get("key", "")
            if not key:
                continue
            
            # Get presigned URL for the photo
            url = _get_url_for_key(key, expires_in=3600)
            if url:
                # Get thumbnail URL if available
                thumb_url = _get_thumbnail_url(key, expires_in=3600)
                
                photos.append({
                    "key": key,
                    "url": url,
                    "thumb_url": thumb_url,
                    "name": ref.get("name", os.path.basename(key)),
                    "added_at": ref.get("added_at", ""),
                    "source_tab": ref.get("source_tab", ""),
                })
        
        return {"photos": photos, "collection_name": collection_name}
    except Exception as ex:
        logger.exception(f"Failed to get collection photos for {uid}: {ex}")
        return JSONResponse({"error": "Failed to get collection photos"}, status_code=500)


@router.post("/{collection_name}/add")
async def add_to_collection(request: Request, collection_name: str, payload: dict = Body(...)):
    """Add photos to a collection."""
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    
    uid = eff_uid
    keys = payload.get("keys", [])
    source_tab = payload.get("source_tab", "")
    
    if not keys or not isinstance(keys, list):
        return JSONResponse({"error": "keys array is required"}, status_code=400)
    
    data_key = _get_collection_data_key(uid, collection_name)
    
    try:
        coll_data = read_json_key(data_key)
        if not isinstance(coll_data, dict):
            return JSONResponse({"error": "Collection not found"}, status_code=404)
        
        existing_photos = coll_data.get("photos", [])
        existing_keys = {p.get("key") for p in existing_photos}
        
        added = 0
        for key in keys:
            if key in existing_keys:
                continue
            existing_photos.append({
                "key": key,
                "name": os.path.basename(key),
                "added_at": datetime.utcnow().isoformat(),
                "source_tab": source_tab,
            })
            added += 1
        
        coll_data["photos"] = existing_photos
        coll_data["updated_at"] = datetime.utcnow().isoformat()
        write_json_key(data_key, coll_data)
        
        # Update index timestamp
        index_key = _get_collections_index_key(uid)
        index_data = read_json_key(index_key)
        if isinstance(index_data, dict):
            for coll in index_data.get("collections", []):
                if coll.get("name", "").lower() == collection_name.lower():
                    coll["updated_at"] = datetime.utcnow().isoformat()
                    break
            write_json_key(index_key, index_data)
        
        return {"ok": True, "added": added}
    except Exception as ex:
        logger.exception(f"Failed to add to collection for {uid}: {ex}")
        return JSONResponse({"error": "Failed to add to collection"}, status_code=500)


@router.post("/{collection_name}/remove")
async def remove_from_collection(request: Request, collection_name: str, payload: dict = Body(...)):
    """Remove photos from a collection."""
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    
    uid = eff_uid
    keys = payload.get("keys", [])
    
    if not keys or not isinstance(keys, list):
        return JSONResponse({"error": "keys array is required"}, status_code=400)
    
    data_key = _get_collection_data_key(uid, collection_name)
    
    try:
        coll_data = read_json_key(data_key)
        if not isinstance(coll_data, dict):
            return JSONResponse({"error": "Collection not found"}, status_code=404)
        
        existing_photos = coll_data.get("photos", [])
        keys_set = set(keys)
        new_photos = [p for p in existing_photos if p.get("key") not in keys_set]
        removed = len(existing_photos) - len(new_photos)
        
        coll_data["photos"] = new_photos
        coll_data["updated_at"] = datetime.utcnow().isoformat()
        write_json_key(data_key, coll_data)
        
        return {"ok": True, "removed": removed}
    except Exception as ex:
        logger.exception(f"Failed to remove from collection for {uid}: {ex}")
        return JSONResponse({"error": "Failed to remove from collection"}, status_code=500)


@router.post("/{collection_name}/rename")
async def rename_collection(request: Request, collection_name: str, payload: dict = Body(...)):
    """Rename a collection."""
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not has_role_access(req_uid, eff_uid, 'gallery'):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    
    uid = eff_uid
    new_name = str(payload.get("new_name", "")).strip()
    
    if not new_name:
        return JSONResponse({"error": "new_name is required"}, status_code=400)
    
    if len(new_name) > 100:
        return JSONResponse({"error": "Collection name too long"}, status_code=400)
    
    index_key = _get_collections_index_key(uid)
    
    try:
        index_data = read_json_key(index_key)
        if not isinstance(index_data, dict):
            return JSONResponse({"error": "Collection not found"}, status_code=404)
        
        collections = index_data.get("collections", [])
        
        # Check if new name already exists
        if any(c.get("name", "").lower() == new_name.lower() for c in collections):
            return JSONResponse({"error": "A collection with this name already exists"}, status_code=409)
        
        # Find and update the collection
        found = False
        for coll in collections:
            if coll.get("name", "").lower() == collection_name.lower():
                old_name = coll["name"]
                coll["name"] = new_name
                coll["updated_at"] = datetime.utcnow().isoformat()
                found = True
                break
        
        if not found:
            return JSONResponse({"error": "Collection not found"}, status_code=404)
        
        # Move collection data file
        old_data_key = _get_collection_data_key(uid, old_name)
        new_data_key = _get_collection_data_key(uid, new_name)
        
        coll_data = read_json_key(old_data_key)
        if isinstance(coll_data, dict):
            write_json_key(new_data_key, coll_data)
        
        index_data["collections"] = collections
        write_json_key(index_key, index_data)
        
        return {"ok": True, "new_name": new_name}
    except Exception as ex:
        logger.exception(f"Failed to rename collection for {uid}: {ex}")
        return JSONResponse({"error": "Failed to rename collection"}, status_code=500)
