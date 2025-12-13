"""
Mapbox Integration Router

Provides endpoints for photo location mapping using Mapbox.
Free tier: 50,000 map loads/month.
"""
from typing import List, Optional
import os
from datetime import datetime

from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from core.config import logger
from core.auth import get_uid_from_request, resolve_workspace_uid
from utils.storage import read_json_key, write_json_key, list_keys

router = APIRouter(prefix="/api/mapbox", tags=["mapbox"])

# Mapbox configuration
MAPBOX_ACCESS_TOKEN = os.getenv("MAPBOX_ACCESS_TOKEN", "")


def _mapbox_settings_key(uid: str) -> str:
    return f"users/{uid}/integrations/mapbox_settings.json"


def _photo_locations_key(uid: str) -> str:
    return f"users/{uid}/integrations/photo_locations.json"


class PhotoLocation(BaseModel):
    key: str
    latitude: float
    longitude: float
    name: Optional[str] = None
    taken_at: Optional[str] = None
    thumbnail_url: Optional[str] = None


@router.get("/status")
async def mapbox_status(request: Request):
    """Check if Mapbox is configured and get user settings."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    # Check if Mapbox is configured at server level
    configured = bool(MAPBOX_ACCESS_TOKEN)
    
    try:
        settings = read_json_key(_mapbox_settings_key(uid)) or {}
        locations = read_json_key(_photo_locations_key(uid)) or {}
        
        return {
            "connected": configured,
            "configured": configured,
            "map_style": settings.get("map_style", "streets-v12"),
            "show_clusters": settings.get("show_clusters", True),
            "default_zoom": settings.get("default_zoom", 3),
            "photo_count": len(locations.get("photos", [])),
        }
    except Exception as ex:
        logger.warning(f"Mapbox status check failed: {ex}")
        return {"connected": False, "configured": configured}


@router.get("/token")
async def mapbox_get_token(request: Request):
    """Get the Mapbox access token for frontend use."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not MAPBOX_ACCESS_TOKEN:
        return JSONResponse({"error": "Mapbox not configured"}, status_code=500)
    
    return {"access_token": MAPBOX_ACCESS_TOKEN}


@router.post("/settings")
async def mapbox_update_settings(
    request: Request,
    map_style: str = Body("streets-v12"),
    show_clusters: bool = Body(True),
    default_zoom: int = Body(3),
):
    """Update Mapbox display settings."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    # Validate map style
    valid_styles = [
        "streets-v12", "outdoors-v12", "light-v11", "dark-v11",
        "satellite-v9", "satellite-streets-v12", "navigation-day-v1"
    ]
    if map_style not in valid_styles:
        map_style = "streets-v12"
    
    settings = {
        "map_style": map_style,
        "show_clusters": show_clusters,
        "default_zoom": max(1, min(20, default_zoom)),
        "updated_at": datetime.utcnow().isoformat()
    }
    
    write_json_key(_mapbox_settings_key(uid), settings)
    return {"ok": True, "settings": settings}


@router.get("/photos")
async def mapbox_get_photos(request: Request, source: Optional[str] = None):
    """Get all photos with location data for map display.
    
    Args:
        source: Optional filter - 'uploads', 'gallery', or 'vaults'
    """
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        locations = read_json_key(_photo_locations_key(eff_uid)) or {}
        photos = locations.get("photos", [])
        logger.info(f"[mapbox.photos] uid={eff_uid} total_photos={len(photos)} source_filter={source}")
        
        # Filter by source if specified
        if source and source in ("uploads", "gallery", "vaults"):
            filtered_photos = []
            for photo in photos:
                key = photo.get("key", "")
                if source == "uploads" and "/external/" in key:
                    filtered_photos.append(photo)
                elif source == "gallery" and "/watermarked/" in key:
                    filtered_photos.append(photo)
                elif source == "vaults" and "/vaults/" in key and "/_" not in key:
                    # Exclude meta files like /_meta/, /_approvals/, etc.
                    filtered_photos.append(photo)
            photos = filtered_photos
        
        return {
            "photos": photos,
            "count": len(photos)
        }
    except Exception as ex:
        logger.exception(f"Failed to get photo locations: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/photos/add")
async def mapbox_add_photo_location(
    request: Request,
    key: str = Body(...),
    latitude: float = Body(...),
    longitude: float = Body(...),
    name: Optional[str] = Body(None),
    taken_at: Optional[str] = Body(None),
    thumbnail_url: Optional[str] = Body(None),
):
    """Add or update location data for a photo."""
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    # Validate coordinates
    if not (-90 <= latitude <= 90):
        return JSONResponse({"error": "Invalid latitude"}, status_code=400)
    if not (-180 <= longitude <= 180):
        return JSONResponse({"error": "Invalid longitude"}, status_code=400)
    
    # Validate key belongs to user
    if not key.startswith(f"users/{eff_uid}/"):
        return JSONResponse({"error": "Invalid photo key"}, status_code=403)
    
    try:
        locations = read_json_key(_photo_locations_key(eff_uid)) or {"photos": []}
        photos = locations.get("photos", [])
        
        # Update existing or add new
        photo_data = {
            "key": key,
            "latitude": latitude,
            "longitude": longitude,
            "name": name,
            "taken_at": taken_at,
            "thumbnail_url": thumbnail_url,
            "updated_at": datetime.utcnow().isoformat()
        }
        
        # Find and update existing entry
        found = False
        for i, p in enumerate(photos):
            if p.get("key") == key:
                photos[i] = photo_data
                found = True
                break
        
        if not found:
            photos.append(photo_data)
        
        locations["photos"] = photos
        locations["updated_at"] = datetime.utcnow().isoformat()
        
        write_json_key(_photo_locations_key(eff_uid), locations)
        
        return {"ok": True, "photo": photo_data}
    except Exception as ex:
        logger.exception(f"Failed to add photo location: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/photos/bulk")
async def mapbox_bulk_add_locations(
    request: Request,
    photos: List[dict] = Body(...),
):
    """Bulk add/update location data for multiple photos."""
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not photos:
        return JSONResponse({"error": "No photos provided"}, status_code=400)
    
    if len(photos) > 500:
        return JSONResponse({"error": "Maximum 500 photos per request"}, status_code=400)
    
    try:
        locations = read_json_key(_photo_locations_key(eff_uid)) or {"photos": []}
        existing = {p.get("key"): i for i, p in enumerate(locations.get("photos", []))}
        photo_list = locations.get("photos", [])
        
        added = 0
        updated = 0
        errors = []
        
        for photo in photos:
            key = photo.get("key", "")
            lat = photo.get("latitude")
            lng = photo.get("longitude")
            
            # Validate
            if not key.startswith(f"users/{eff_uid}/"):
                errors.append({"key": key, "error": "Invalid key"})
                continue
            
            if lat is None or lng is None:
                errors.append({"key": key, "error": "Missing coordinates"})
                continue
            
            try:
                lat = float(lat)
                lng = float(lng)
            except (ValueError, TypeError):
                errors.append({"key": key, "error": "Invalid coordinates"})
                continue
            
            if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
                errors.append({"key": key, "error": "Coordinates out of range"})
                continue
            
            photo_data = {
                "key": key,
                "latitude": lat,
                "longitude": lng,
                "name": photo.get("name"),
                "taken_at": photo.get("taken_at"),
                "thumbnail_url": photo.get("thumbnail_url"),
                "updated_at": datetime.utcnow().isoformat()
            }
            
            if key in existing:
                photo_list[existing[key]] = photo_data
                updated += 1
            else:
                photo_list.append(photo_data)
                existing[key] = len(photo_list) - 1
                added += 1
        
        locations["photos"] = photo_list
        locations["updated_at"] = datetime.utcnow().isoformat()
        
        write_json_key(_photo_locations_key(eff_uid), locations)
        
        return {
            "ok": True,
            "added": added,
            "updated": updated,
            "errors": errors if errors else None,
            "total": len(photo_list)
        }
    except Exception as ex:
        logger.exception(f"Failed to bulk add photo locations: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.delete("/photos/{photo_key:path}")
async def mapbox_remove_photo_location(request: Request, photo_key: str):
    """Remove location data for a photo."""
    eff_uid, req_uid = resolve_workspace_uid(request)
    if not eff_uid or not req_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not photo_key.startswith(f"users/{eff_uid}/"):
        return JSONResponse({"error": "Invalid photo key"}, status_code=403)
    
    try:
        locations = read_json_key(_photo_locations_key(eff_uid)) or {"photos": []}
        photos = locations.get("photos", [])
        
        # Filter out the photo
        new_photos = [p for p in photos if p.get("key") != photo_key]
        
        if len(new_photos) == len(photos):
            return JSONResponse({"error": "Photo not found"}, status_code=404)
        
        locations["photos"] = new_photos
        locations["updated_at"] = datetime.utcnow().isoformat()
        
        write_json_key(_photo_locations_key(eff_uid), locations)
        
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"Failed to remove photo location: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.get("/geocode")
async def mapbox_geocode(
    request: Request,
    query: str = "",
):
    """Geocode a location name to coordinates using Mapbox Geocoding API."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not MAPBOX_ACCESS_TOKEN:
        return JSONResponse({"error": "Mapbox not configured"}, status_code=500)
    
    if not query or len(query) < 2:
        return JSONResponse({"error": "Query too short"}, status_code=400)
    
    try:
        import httpx
        from urllib.parse import quote
        
        encoded_query = quote(query)
        url = f"https://api.mapbox.com/geocoding/v5/mapbox.places/{encoded_query}.json"
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params={
                "access_token": MAPBOX_ACCESS_TOKEN,
                "limit": 5,
                "types": "place,locality,neighborhood,address"
            })
            
            if resp.status_code != 200:
                return JSONResponse({"error": "Geocoding failed"}, status_code=500)
            
            data = resp.json()
            features = data.get("features", [])
            
            results = []
            for f in features:
                center = f.get("center", [])
                if len(center) >= 2:
                    results.append({
                        "name": f.get("place_name", ""),
                        "longitude": center[0],
                        "latitude": center[1],
                        "type": f.get("place_type", [None])[0]
                    })
            
            return {"results": results}
    except Exception as ex:
        logger.exception(f"Geocoding error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.get("/reverse-geocode")
async def mapbox_reverse_geocode(
    request: Request,
    latitude: float = 0,
    longitude: float = 0,
):
    """Reverse geocode coordinates to a location name."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if not MAPBOX_ACCESS_TOKEN:
        return JSONResponse({"error": "Mapbox not configured"}, status_code=500)
    
    if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
        return JSONResponse({"error": "Invalid coordinates"}, status_code=400)
    
    try:
        import httpx
        
        url = f"https://api.mapbox.com/geocoding/v5/mapbox.places/{longitude},{latitude}.json"
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params={
                "access_token": MAPBOX_ACCESS_TOKEN,
                "limit": 1,
                "types": "place,locality,neighborhood"
            })
            
            if resp.status_code != 200:
                return JSONResponse({"error": "Reverse geocoding failed"}, status_code=500)
            
            data = resp.json()
            features = data.get("features", [])
            
            if features:
                f = features[0]
                return {
                    "name": f.get("place_name", ""),
                    "type": f.get("place_type", [None])[0]
                }
            
            return {"name": None, "type": None}
    except Exception as ex:
        logger.exception(f"Reverse geocoding error: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)
