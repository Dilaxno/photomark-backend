from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
import os
import httpx
from core.auth import get_uid_from_request
from core.config import logger

router = APIRouter(prefix="/api/chat/cometchat", tags=["cometchat"]) 

APP_ID = (os.getenv("COMETCHAT_APP_ID", "") or "").strip()
API_KEY = (os.getenv("COMETCHAT_API_KEY", "") or "").strip()
REGION = (os.getenv("COMETCHAT_REGION", "") or "").strip()

def _base_url() -> str:
    if not APP_ID or not REGION:
        return ""
    return f"https://{REGION}.cometchat.io/v3"

def _headers() -> dict:
    return {
        "accept": "application/json",
        "content-type": "application/json",
        "appId": APP_ID,
        "apiKey": API_KEY,
    }

@router.post("/users/ensure")
async def ensure_users(request: Request, users: list[dict] = Body(...)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not APP_ID or not API_KEY or not REGION:
        return JSONResponse({"error": "cometchat_not_configured"}, status_code=500)
    try:
        out = []
        async with httpx.AsyncClient(timeout=10.0) as client:
            for u in users:
                uidv = str(u.get("uid") or "").strip()
                name = str(u.get("name") or uidv or "").strip()
                if not uidv:
                    continue
                try:
                    r = await client.get(f"{_base_url()}/users/{uidv}", headers=_headers())
                except Exception as ex:
                    logger.warning(f"cometchat users get failed: {ex}")
                    r = None
                if r is not None and r.status_code == 200:
                    out.append({"uid": uidv, "ok": True})
                    continue
                payload = {"uid": uidv, "name": name}
                r2 = await client.post(f"{_base_url()}/users", headers=_headers(), json=payload)
                if r2.status_code in (200, 201):
                    out.append({"uid": uidv, "ok": True})
                else:
                    try:
                        data = r2.json()
                    except Exception:
                        data = {"error": r2.text}
                    out.append({"uid": uidv, "ok": False, "error": data.get("message") or data.get("error")})
        return {"ok": True, "items": out}
    except Exception as ex:
        logger.exception(f"ensure_users failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)

@router.post("/group/create")
async def group_create(
    request: Request,
    guid: str = Body(..., embed=True),
    name: str = Body(..., embed=True),
    members: list[str] = Body([], embed=True),
):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not APP_ID or not API_KEY or not REGION:
        return JSONResponse({"error": "cometchat_not_configured"}, status_code=500)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                r = await client.get(f"{_base_url()}/groups/{guid}", headers=_headers())
            except Exception:
                r = None
            if r is None or r.status_code != 200:
                payload = {"guid": guid, "name": name, "type": "private"}
                await client.post(f"{_base_url()}/groups", headers=_headers(), json=payload)
            add = [{"uid": m, "scope": "participant"} for m in members if m]
            if add:
                await client.post(f"{_base_url()}/groups/{guid}/members", headers=_headers(), json={"members": add})
        return {"ok": True, "guid": guid, "name": name}
    except Exception as ex:
        logger.exception(f"group_create failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)

