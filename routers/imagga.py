"""
Imagga API Integration - Auto-tagging, categorization, and image analysis
Docs: https://docs.imagga.com/
Free tier: 1,000 images/month
"""

from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, List
import os
import httpx

router = APIRouter(prefix="/api/imagga", tags=["imagga"])

IMAGGA_API_BASE = "https://api.imagga.com/v2"


def _get_auth():
    """Get Imagga API credentials for HTTP Basic Auth"""
    api_key = (os.getenv("IMAGGA_API_KEY") or "").strip()
    api_secret = (os.getenv("IMAGGA_API_SECRET") or "").strip()
    if not api_key or not api_secret:
        return None
    return (api_key, api_secret)


@router.get("/status")
async def get_status():
    """Check if Imagga is configured"""
    auth = _get_auth()
    if not auth:
        return {"configured": False, "message": "IMAGGA_API_KEY and IMAGGA_API_SECRET not set"}
    
    # Test the connection with a simple API call
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{IMAGGA_API_BASE}/usage", auth=auth)
            if r.status_code == 200:
                data = r.json()
                usage = data.get("result", {})
                return {
                    "configured": True,
                    "connected": True,
                    "usage": {
                        "monthly_limit": usage.get("monthly_limit"),
                        "monthly_processed": usage.get("monthly_processed"),
                        "remaining": usage.get("monthly_limit", 0) - usage.get("monthly_processed", 0)
                    }
                }
            return {"configured": True, "connected": False, "error": "Failed to verify credentials"}
    except Exception as e:
        return {"configured": True, "connected": False, "error": str(e)}


class TagRequest(BaseModel):
    image_url: str
    language: Optional[str] = "en"
    threshold: Optional[float] = 20.0  # Minimum confidence threshold


@router.post("/tags")
async def get_tags(req: TagRequest):
    """Get auto-generated tags for an image"""
    auth = _get_auth()
    if not auth:
        raise HTTPException(status_code=500, detail="Imagga not configured")
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(
                f"{IMAGGA_API_BASE}/tags",
                params={
                    "image_url": req.image_url,
                    "language": req.language,
                    "threshold": req.threshold
                },
                auth=auth
            )
            
            if r.status_code != 200:
                return JSONResponse(r.json(), status_code=r.status_code)
            
            data = r.json()
            tags = data.get("result", {}).get("tags", [])
            
            # Format tags for easier consumption
            formatted_tags = [
                {
                    "tag": t.get("tag", {}).get(req.language, t.get("tag", {}).get("en", "")),
                    "confidence": round(t.get("confidence", 0), 2)
                }
                for t in tags
            ]
            
            return {
                "tags": formatted_tags,
                "count": len(formatted_tags)
            }
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Imagga API timeout")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tags")
async def get_tags_simple(
    image_url: str = Query(..., description="URL of the image to analyze"),
    language: str = Query("en", description="Language for tags"),
    threshold: float = Query(20.0, description="Minimum confidence threshold")
):
    """Get auto-generated tags for an image (GET version)"""
    return await get_tags(TagRequest(image_url=image_url, language=language, threshold=threshold))


class ColorRequest(BaseModel):
    image_url: str
    extract_overall_colors: Optional[bool] = True
    extract_object_colors: Optional[bool] = False


@router.post("/colors")
async def get_colors(req: ColorRequest):
    """Extract dominant colors from an image"""
    auth = _get_auth()
    if not auth:
        raise HTTPException(status_code=500, detail="Imagga not configured")
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            params = {"image_url": req.image_url}
            if req.extract_overall_colors:
                params["extract_overall_colors"] = "1"
            if req.extract_object_colors:
                params["extract_object_colors"] = "1"
            
            r = await client.get(f"{IMAGGA_API_BASE}/colors", params=params, auth=auth)
            
            if r.status_code != 200:
                return JSONResponse(r.json(), status_code=r.status_code)
            
            data = r.json()
            colors = data.get("result", {}).get("colors", {})
            
            # Format colors for easier consumption
            result = {}
            
            if "background_colors" in colors:
                result["background"] = [
                    {
                        "hex": f"#{c.get('html_code', '000000')}",
                        "name": c.get("closest_palette_color", ""),
                        "percent": round(c.get("percent", 0), 2)
                    }
                    for c in colors["background_colors"]
                ]
            
            if "foreground_colors" in colors:
                result["foreground"] = [
                    {
                        "hex": f"#{c.get('html_code', '000000')}",
                        "name": c.get("closest_palette_color", ""),
                        "percent": round(c.get("percent", 0), 2)
                    }
                    for c in colors["foreground_colors"]
                ]
            
            if "image_colors" in colors:
                result["dominant"] = [
                    {
                        "hex": f"#{c.get('html_code', '000000')}",
                        "name": c.get("closest_palette_color", ""),
                        "percent": round(c.get("percent", 0), 2)
                    }
                    for c in colors["image_colors"]
                ]
            
            return {"colors": result}
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Imagga API timeout")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/colors")
async def get_colors_simple(
    image_url: str = Query(..., description="URL of the image to analyze")
):
    """Extract dominant colors from an image (GET version)"""
    return await get_colors(ColorRequest(image_url=image_url))


class CategorizeRequest(BaseModel):
    image_url: str
    categorizer_id: Optional[str] = "personal_photos"  # Default categorizer


@router.post("/categories")
async def get_categories(req: CategorizeRequest):
    """Categorize an image using Imagga's categorizers"""
    auth = _get_auth()
    if not auth:
        raise HTTPException(status_code=500, detail="Imagga not configured")
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(
                f"{IMAGGA_API_BASE}/categories/{req.categorizer_id}",
                params={"image_url": req.image_url},
                auth=auth
            )
            
            if r.status_code != 200:
                return JSONResponse(r.json(), status_code=r.status_code)
            
            data = r.json()
            categories = data.get("result", {}).get("categories", [])
            
            # Format categories
            formatted = [
                {
                    "name": c.get("name", {}).get("en", ""),
                    "confidence": round(c.get("confidence", 0), 2)
                }
                for c in categories
            ]
            
            return {
                "categories": formatted,
                "categorizer": req.categorizer_id
            }
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Imagga API timeout")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class FaceRequest(BaseModel):
    image_url: str
    return_face_id: Optional[bool] = False


@router.post("/faces")
async def detect_faces(req: FaceRequest):
    """Detect faces in an image"""
    auth = _get_auth()
    if not auth:
        raise HTTPException(status_code=500, detail="Imagga not configured")
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            params = {"image_url": req.image_url}
            if req.return_face_id:
                params["return_face_id"] = "1"
            
            r = await client.get(f"{IMAGGA_API_BASE}/faces/detections", params=params, auth=auth)
            
            if r.status_code != 200:
                return JSONResponse(r.json(), status_code=r.status_code)
            
            data = r.json()
            faces = data.get("result", {}).get("faces", [])
            
            # Format face detections
            formatted = []
            for f in faces:
                face_data = {
                    "confidence": round(f.get("confidence", 0), 2),
                    "coordinates": f.get("coordinates", {})
                }
                if "face_id" in f:
                    face_data["face_id"] = f["face_id"]
                formatted.append(face_data)
            
            return {
                "faces": formatted,
                "count": len(formatted)
            }
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Imagga API timeout")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class CropRequest(BaseModel):
    image_url: str
    resolution: Optional[str] = "800x600"  # Target aspect ratio/resolution


@router.post("/crops")
async def get_smart_crops(req: CropRequest):
    """Get smart crop suggestions for an image"""
    auth = _get_auth()
    if not auth:
        raise HTTPException(status_code=500, detail="Imagga not configured")
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(
                f"{IMAGGA_API_BASE}/croppings",
                params={
                    "image_url": req.image_url,
                    "resolution": req.resolution
                },
                auth=auth
            )
            
            if r.status_code != 200:
                return JSONResponse(r.json(), status_code=r.status_code)
            
            data = r.json()
            croppings = data.get("result", {}).get("croppings", [])
            
            return {
                "crops": croppings,
                "resolution": req.resolution
            }
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Imagga API timeout")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class AnalyzeRequest(BaseModel):
    image_url: str
    include_tags: Optional[bool] = True
    include_colors: Optional[bool] = True
    include_categories: Optional[bool] = False
    include_faces: Optional[bool] = False
    tag_language: Optional[str] = "en"
    tag_threshold: Optional[float] = 20.0


@router.post("/analyze")
async def analyze_image(req: AnalyzeRequest):
    """Comprehensive image analysis - tags, colors, categories, and faces in one call"""
    auth = _get_auth()
    if not auth:
        raise HTTPException(status_code=500, detail="Imagga not configured")
    
    result = {"image_url": req.image_url}
    
    async with httpx.AsyncClient(timeout=60.0) as client:
        # Get tags
        if req.include_tags:
            try:
                r = await client.get(
                    f"{IMAGGA_API_BASE}/tags",
                    params={
                        "image_url": req.image_url,
                        "language": req.tag_language,
                        "threshold": req.tag_threshold
                    },
                    auth=auth
                )
                if r.status_code == 200:
                    tags = r.json().get("result", {}).get("tags", [])
                    result["tags"] = [
                        {
                            "tag": t.get("tag", {}).get(req.tag_language, ""),
                            "confidence": round(t.get("confidence", 0), 2)
                        }
                        for t in tags
                    ]
            except Exception:
                result["tags"] = []
        
        # Get colors
        if req.include_colors:
            try:
                r = await client.get(
                    f"{IMAGGA_API_BASE}/colors",
                    params={"image_url": req.image_url},
                    auth=auth
                )
                if r.status_code == 200:
                    colors = r.json().get("result", {}).get("colors", {})
                    result["colors"] = {
                        "dominant": [
                            {
                                "hex": f"#{c.get('html_code', '000000')}",
                                "name": c.get("closest_palette_color", ""),
                                "percent": round(c.get("percent", 0), 2)
                            }
                            for c in colors.get("image_colors", [])
                        ]
                    }
            except Exception:
                result["colors"] = {}
        
        # Get categories
        if req.include_categories:
            try:
                r = await client.get(
                    f"{IMAGGA_API_BASE}/categories/personal_photos",
                    params={"image_url": req.image_url},
                    auth=auth
                )
                if r.status_code == 200:
                    categories = r.json().get("result", {}).get("categories", [])
                    result["categories"] = [
                        {
                            "name": c.get("name", {}).get("en", ""),
                            "confidence": round(c.get("confidence", 0), 2)
                        }
                        for c in categories
                    ]
            except Exception:
                result["categories"] = []
        
        # Detect faces
        if req.include_faces:
            try:
                r = await client.get(
                    f"{IMAGGA_API_BASE}/faces/detections",
                    params={"image_url": req.image_url},
                    auth=auth
                )
                if r.status_code == 200:
                    faces = r.json().get("result", {}).get("faces", [])
                    result["faces"] = {
                        "count": len(faces),
                        "detected": len(faces) > 0
                    }
            except Exception:
                result["faces"] = {"count": 0, "detected": False}
    
    return result
