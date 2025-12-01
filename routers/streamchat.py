from fastapi import APIRouter, Request, Body, Depends
import asyncio
import httpx
from httpx import Timeout
from fastapi.responses import JSONResponse
import os
import httpx
import jwt
from datetime import datetime, timedelta

from core.auth import get_uid_from_request
from core.config import logger
from utils.emailing import render_email, send_email_smtp
from sqlalchemy.orm import Session
from core.database import get_db
from models.user import User

router = APIRouter(prefix="/api/chat/stream", tags=["streamchat"]) 

API_KEY = (os.getenv("STREAM_API_KEY", "") or "").strip()
API_SECRET = (os.getenv("STREAM_API_SECRET", "") or "").strip()

BASE_URL = "https://chat.stream-io-api.com"

def _server_token() -> str:
    if not API_SECRET:
        return ""
    payload = {"server": True, "exp": int((datetime.utcnow() + timedelta(hours=24)).timestamp())}
    try:
        token = jwt.encode(payload, API_SECRET, algorithm="HS256")
        return token if isinstance(token, str) else token.decode("utf-8")
    except Exception as ex:
        logger.warning(f"stream server token failed: {ex}")
        return ""

def _headers() -> dict:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Stream-Auth-Type": "jwt",
        "Authorization": _server_token(),
        "api_key": API_KEY,
    }


@router.post("/token")
async def stream_token(request: Request, user_id: str = Body(..., embed=True), expire_seconds: int = Body(24 * 3600, embed=True)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not API_KEY or not API_SECRET:
        return JSONResponse({"error": "stream_not_configured"}, status_code=500)
    try:
        exp = int((datetime.utcnow() + timedelta(seconds=max(60, int(expire_seconds or 0)))).timestamp())
        payload = {"user_id": user_id, "exp": exp}
        token = jwt.encode(payload, API_SECRET, algorithm="HS256")
        tok = token if isinstance(token, str) else token.decode("utf-8")
        return {"ok": True, "token": tok, "api_key": API_KEY}
    except Exception as ex:
        logger.exception(f"stream_token failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/users/ensure")
async def users_ensure(request: Request, users: list[dict] = Body(default=[])):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not API_KEY or not API_SECRET:
        return JSONResponse({"error": "stream_not_configured"}, status_code=500)
    try:
        payload_users = {}
        for u in (users or []):
            uidv = str((u or {}).get("id") or (u or {}).get("uid") or "").strip()
            name = str((u or {}).get("name") or "").strip()
            image = str((u or {}).get("image") or (u or {}).get("photo_url") or "").strip()
            email = str((u or {}).get("email") or "").strip()
            presence_state = str((u or {}).get("presence_state") or "").strip()
            if not uidv:
                continue
            data = {"id": uidv}
            if name:
                data["name"] = name
            if image:
                data["image"] = image
            if email:
                data["email"] = email
            if presence_state:
                data["presence_state"] = presence_state
            payload_users[uidv] = data
        if not payload_users:
            return {"ok": True, "items": []}
        async with httpx.AsyncClient(timeout=Timeout(connect=5.0, read=10.0, write=10.0)) as client:
            last_ex = None
            for attempt in range(3):
                try:
                    r = await client.post(
                        f"{BASE_URL}/users",
                        headers=_headers(),
                        params={"api_key": API_KEY},
                        json={"users": payload_users}
                    )
                    if r.status_code in (200, 201):
                        break
                    try:
                        data = r.json()
                    except Exception:
                        data = {"error": r.text}
                    return JSONResponse({"error": data.get("message") or data.get("error") or "failed"}, status_code=r.status_code)
                except Exception as ex:
                    last_ex = ex
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
            if last_ex:
                return JSONResponse({"error": "stream_users_timeout"}, status_code=504)
        return {"ok": True, "items": list(payload_users.keys())}
    except Exception as ex:
        logger.exception(f"users_ensure failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/group/create")
async def group_create(request: Request, guid: str = Body(..., embed=True), name: str = Body("Collab Chat", embed=True), members: list[str] = Body(default=[]), topic: str = Body("", embed=True)):
    owner_uid = get_uid_from_request(request)
    if not owner_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not API_KEY or not API_SECRET:
        return JSONResponse({"error": "stream_not_configured"}, status_code=500)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            create_body = {
                "type": "messaging",
                "id": guid,
                "data": {"name": name, **({"topic": topic} if topic else {})},
                "created_by_id": owner_uid,
            }
            r = await client.post(f"{BASE_URL}/channels", headers=_headers(), params={"api_key": API_KEY}, json=create_body)
            if r.status_code not in (200, 201):
                try:
                    data = r.json()
                except Exception:
                    data = {"error": r.text}
                return JSONResponse({"error": data.get("message") or data.get("error") or "failed"}, status_code=r.status_code)
            if members:
                try:
                    await client.post(f"{BASE_URL}/channels/messaging/{guid}/update", headers=_headers(), params={"api_key": API_KEY}, json={"add_members": list(set(members + [owner_uid]))})
                except Exception:
                    pass
        return {"ok": True, "guid": guid, "name": name}
    except Exception as ex:
        logger.exception(f"group_create failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/group/invite")
async def group_invite(request: Request, guid: str = Body(..., embed=True), emails: list[str] = Body(default=[]), note: str = Body("", embed=True)):
    owner_uid = get_uid_from_request(request)
    if not owner_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not API_KEY or not API_SECRET:
        return JSONResponse({"error": "stream_not_configured"}, status_code=500)
    try:
        # Normalize emails -> user ids
        ids = []
        for em in (emails or []):
            q = str(em or "").strip().lower()
            if not q:
                continue
            local = q.split("@")[0]
            ids.append("".join([c for c in local if c.isalnum() or c == "_"]))

        async with httpx.AsyncClient(timeout=10.0) as client:
            # Ensure users
            payload_users = {uidv: {"id": uidv} for uidv in ids}
            if payload_users:
                try:
                    await client.post(f"{BASE_URL}/users", headers=_headers(), params={"api_key": API_KEY}, json={"users": payload_users})
                except Exception:
                    pass
            # Add to channel
            if ids:
                try:
                    await client.post(f"{BASE_URL}/channels/messaging/{guid}/update", headers=_headers(), params={"api_key": API_KEY}, json={"add_members": ids})
                except Exception:
                    pass

        origin = (os.getenv("FRONTEND_ORIGIN", "") or "").strip() or ""
        join_url = f"{origin}/collab-chat?guid={guid}" if origin else f"/collab-chat?guid={guid}"
        subject = "You've been invited to a collaboration chat"
        html_body = render_email(
            "email_basic.html",
            title="Collaboration Chat Invitation",
            intro=(
                "You have been invited to join a collaboration chat. Click the button below to join." +
                ("" if not note else ("<br><br>Note from owner: " + note))
            ),
            button_label="Join Chat",
            button_url=join_url,
        )
        sent = 0
        for em in emails or []:
            try:
                send_email_smtp(to_email=em, subject=subject, html=html_body)
                sent += 1
            except Exception as ex:
                logger.warning(f"chat invite email failed to {em}: {ex}")
        return {"ok": True, "sent": sent, "guid": guid}
    except Exception as ex:
        logger.exception(f"group_invite failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/group/join")
async def group_join(request: Request, guid: str = Body(..., embed=True), db: Session = Depends(get_db)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not API_KEY or not API_SECRET:
        return JSONResponse({"error": "stream_not_configured"}, status_code=500)
    try:
        async with httpx.AsyncClient(timeout=Timeout(connect=5.0, read=10.0, write=10.0)) as client:
            try:
                display = ""
                image = ""
                try:
                    urec = db.query(User).filter(User.uid == uid).first() if db else None
                    if urec:
                        display = (urec.display_name or "").strip()
                        image = (urec.photo_url or "").strip()
                except Exception:
                    pass
                last_ex = None
                for attempt in range(3):
                    try:
                        await client.post(
                            f"{BASE_URL}/users",
                            headers=_headers(),
                            params={"api_key": API_KEY},
                            json={"users": {uid: {"id": uid, **({"name": display} if display else {}), **({"image": image} if image else {})}}}
                        )
                        last_ex = None
                        break
                    except Exception as ex:
                        last_ex = ex
                        await asyncio.sleep(0.5 * (2 ** attempt))
                        continue
            except Exception:
                pass
            # Add member to channel
            r = await client.post(f"{BASE_URL}/channels/messaging/{guid}/update", headers=_headers(), params={"api_key": API_KEY}, json={"add_members": [uid]})
            if r.status_code not in (200, 201):
                try:
                    data = r.json()
                except Exception:
                    data = {"error": r.text}
                # Treat already exists as success
                msg = str(data.get("message") or data.get("error") or "")
                if "already" not in msg.lower():
                    return JSONResponse({"error": msg or "failed"}, status_code=400)
        return {"ok": True, "guid": guid, "uid": uid}
    except Exception as ex:
        logger.exception(f"group_join failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/group/delete")
async def group_delete(request: Request, guid: str = Body(..., embed=True)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not API_KEY or not API_SECRET:
        return JSONResponse({"error": "stream_not_configured"}, status_code=500)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.delete(f"{BASE_URL}/channels/messaging/{guid}", headers=_headers(), params={"api_key": API_KEY})
            if r.status_code not in (200, 204):
                try:
                    data = r.json()
                except Exception:
                    data = {"error": r.text}
                return JSONResponse({"error": data.get("message") or data.get("error") or "failed"}, status_code=r.status_code)
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"group_delete failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)

