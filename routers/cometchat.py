from fastapi import APIRouter, Request, Body
from fastapi.responses import JSONResponse
import os
import httpx
from core.auth import get_uid_from_request
from core.config import logger
from utils.emailing import render_email, send_email_smtp

router = APIRouter(prefix="/api/chat/cometchat", tags=["cometchat"]) 

APP_ID = (os.getenv("COMETCHAT_APP_ID", "") or "").strip()
API_KEY = (os.getenv("COMETCHAT_API_KEY", "") or "").strip()
REGION = (os.getenv("COMETCHAT_REGION", "") or "").strip()

def _base_url() -> str:
    if not APP_ID or not REGION:
        return ""
    return f"https://{APP_ID}.api-{REGION}.cometchat.io/v3"

def _headers() -> dict:
    return {
        "accept": "application/json",
        "content-type": "application/json",
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
            ids: list[str] = []
            for m in [uid] + [x for x in members if x]:
                if m and (m not in ids):
                    ids.append(m)
            for m in ids:
                try:
                    ur = await client.get(f"{_base_url()}/users/{m}", headers=_headers())
                except Exception:
                    ur = None
                if ur is None or ur.status_code != 200:
                    up = {"uid": m, "name": m}
                    try:
                        await client.post(f"{_base_url()}/users", headers=_headers(), json=up)
                    except Exception:
                        pass
            try:
                r = await client.get(f"{_base_url()}/groups/{guid}", headers=_headers())
            except Exception:
                r = None
            if r is None or r.status_code != 200:
                gp = {"guid": guid, "name": name, "type": "private"}
                await client.post(f"{_base_url()}/groups", headers=_headers(), json=gp)
            add = [{"uid": m, "scope": ("admin" if m == uid else "participant")} for m in ids]
            if add:
                await client.post(f"{_base_url()}/groups/{guid}/members", headers=_headers(), json={"members": add})
        return {"ok": True, "guid": guid, "name": name}
    except Exception as ex:
        logger.exception(f"group_create failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)

@router.post("/group/join")
async def group_join(
    request: Request,
    guid: str = Body(..., embed=True),
    scope: str = Body("participant", embed=True),
):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not APP_ID or not API_KEY or not REGION:
        return JSONResponse({"error": "cometchat_not_configured"}, status_code=500)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                gr = await client.get(f"{_base_url()}/groups/{guid}", headers=_headers())
            except Exception:
                gr = None
            if gr is None or gr.status_code != 200:
                return JSONResponse({"error": "group_not_found"}, status_code=404)
            try:
                ur = await client.get(f"{_base_url()}/users/{uid}", headers=_headers())
            except Exception:
                ur = None
            if ur is None or ur.status_code != 200:
                up = {"uid": uid, "name": uid}
                try:
                    await client.post(f"{_base_url()}/users", headers=_headers(), json=up)
                except Exception:
                    pass
            sc = ("admin" if scope == "admin" else "participant")
            payload = {"members": [{"uid": uid, "scope": sc}]}
            r = await client.post(f"{_base_url()}/groups/{guid}/members", headers=_headers(), json=payload)
            if r.status_code in (200, 201):
                return {"ok": True, "guid": guid, "uid": uid, "scope": sc}
            try:
                data = r.json()
            except Exception:
                data = {"error": r.text}
            return JSONResponse({"error": data.get("message") or data.get("error")}, status_code=400)
    except Exception as ex:
        logger.exception(f"group_join failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)

@router.post("/group/invite")
async def group_invite(
    request: Request,
    guid: str = Body(..., embed=True),
    emails: list[str] = Body(..., embed=True),
    note: str = Body("", embed=True),
):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not APP_ID or not API_KEY or not REGION:
        return JSONResponse({"error": "cometchat_not_configured"}, status_code=500)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                gr = await client.get(f"{_base_url()}/groups/{guid}", headers=_headers())
            except Exception:
                gr = None
            if gr is None or gr.status_code != 200:
                return JSONResponse({"error": "group_not_found"}, status_code=404)

            def norm(e: str) -> str:
                return (e or "").lower().replace("[^a-z0-9]", "_")

            members = []
            for em in emails:
                emv = str(em or "").strip()
                if not emv:
                    continue
                cuid = emv.lower()
                cuid = "".join([c if c.isalnum() else "_" for c in cuid])[:64]
                up = {"uid": cuid, "name": emv}
                try:
                    ur = await client.get(f"{_base_url()}/users/{cuid}", headers=_headers())
                except Exception:
                    ur = None
                if ur is None or ur.status_code != 200:
                    try:
                        await client.post(f"{_base_url()}/users", headers=_headers(), json=up)
                    except Exception:
                        pass
                members.append({"uid": cuid, "scope": "participant"})

            if members:
                try:
                    await client.post(f"{_base_url()}/groups/{guid}/members", headers=_headers(), json={"members": members})
                except Exception:
                    pass

        origin = (os.getenv("FRONTEND_ORIGIN", "") or "").strip() or ""
        join_url = f"{origin}/collab-chat?guid={guid}" if origin else f"/collab-chat?guid={guid}"
        subject = "You've been invited to a collaboration chat"
        html_body = render_email(
            subject,
            f"You have been invited to join a collaboration chat. Click the button below to join. {('' if not note else '<br><br>Note from owner: ' + note)}",
            cta_text="Join Chat",
            cta_url=join_url,
        )
        sent = 0
        for em in emails:
            try:
                send_email_smtp(to_email=em, subject=subject, html=html_body)
                sent += 1
            except Exception as ex:
                logger.warning(f"chat invite email failed to {em}: {ex}")
        return {"ok": True, "sent": sent, "guid": guid}
    except Exception as ex:
        logger.exception(f"group_invite failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)

