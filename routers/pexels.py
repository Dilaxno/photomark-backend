from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
import os
import httpx

router = APIRouter(prefix="/api/pexels", tags=["pexels"])

async def _pexels_request(path: str, params: dict):
    key = (os.getenv("PEXELS_API_KEY") or "").strip()
    if not key:
        return JSONResponse({"error": "Missing PEXELS_API_KEY"}, status_code=500)
    url = f"https://api.pexels.com{path}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(url, headers={"Authorization": key}, params=params)
        if r.status_code != 200:
            try:
                data = r.json()
            except Exception:
                data = {"error": r.text}
            return JSONResponse(data, status_code=r.status_code)
        data = r.json()
        photos = data.get("photos") or []
        items = []
        for p in photos:
            src = p.get("src") or {}
            items.append({
                "id": p.get("id"),
                "photographer": p.get("photographer"),
                "url": p.get("url"),
                "tiny": src.get("tiny"),
                "small": src.get("small"),
                "medium": src.get("medium"),
                "large": src.get("large"),
                "original": src.get("original"),
            })
        return {"photos": items, "page": data.get("page"), "per_page": data.get("per_page"), "total_results": data.get("total_results")}

@router.get("/search")
async def search(q: str = Query(""), page: int = Query(1, ge=1), per_page: int = Query(24, ge=1, le=80)):
    path = "/v1/search" if q else "/v1/curated"
    params = {"page": page, "per_page": per_page}
    if q:
        params["query"] = q
    return await _pexels_request(path, params)

