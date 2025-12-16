from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
import os
import httpx

router = APIRouter(prefix="/api/pixabay", tags=["pixabay"])


@router.get("/search")
async def search(
    q: str = Query(""),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=200),
    orientation: str = Query("horizontal"),
    image_type: str = Query("photo"),
):
    """Search Pixabay for images. Used for background replacement in tools."""
    key = (os.getenv("PIXABAY_API_KEY") or "").strip()
    if not key:
        return JSONResponse({"error": "Missing PIXABAY_API_KEY"}, status_code=500)

    if not q.strip():
        return {"hits": [], "total": 0, "totalHits": 0}

    url = "https://pixabay.com/api/"
    params = {
        "key": key,
        "q": q.strip(),
        "page": page,
        "per_page": per_page,
        "image_type": image_type,
        "orientation": orientation,
        "safesearch": "true",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(url, params=params)
        if r.status_code != 200:
            try:
                data = r.json()
            except Exception:
                data = {"error": r.text}
            return JSONResponse(data, status_code=r.status_code)

        data = r.json()
        hits = data.get("hits") or []
        items = []
        for h in hits:
            items.append({
                "id": h.get("id"),
                "tags": h.get("tags"),
                "webformatURL": h.get("webformatURL"),
                "largeImageURL": h.get("largeImageURL"),
                "imageWidth": h.get("imageWidth"),
                "imageHeight": h.get("imageHeight"),
                "user": h.get("user"),
            })

        return {
            "hits": items,
            "total": data.get("total", 0),
            "totalHits": data.get("totalHits", 0),
            "page": page,
            "per_page": per_page,
        }
