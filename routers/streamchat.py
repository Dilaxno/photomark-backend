from fastapi import APIRouter, Request, Body, Depends
import asyncio
import httpx
from httpx import Timeout
from fastapi.responses import JSONResponse
import os
import jwt
from datetime import datetime, timedelta

from core.auth import get_uid_from_request
from core.config import logger
from utils.emailing import render_email, send_email_smtp
from sqlalchemy.orm import Session
from core.database import get_db
from models.user import User
from models.collaborator import Collaborator

router = APIRouter(prefix="/api/chat/stream", tags=["streamchat"]) 

API_KEY = (os.getenv("STREAM_API_KEY", "") or "").strip()
API_SECRET = (os.getenv("STREAM_API_SECRET", "") or "").strip()
COLLAB_JWT_SECRET = (os.getenv("COLLAB_JWT_SECRET", "") or os.getenv("SECRET_KEY", "")).strip()

BASE_URL = "https://chat.stream-io-api.com"

# Simple in-process dedupe for /users posts to avoid bursts of identical ensures
_RECENT_USER_POSTS: dict[str, float] = {}
_RECENT_TTL_SECONDS = 8.0

def _dedupe_key(uidv: str, presence_state: str | None) -> str:
    return f"{uidv}|{presence_state or ''}"

def _should_post(uidv: str, presence_state: str | None) -> bool:
    k = _dedupe_key(uidv, presence_state)
    now = datetime.utcnow().timestamp()
    last = _RECENT_USER_POSTS.get(k, 0)
    if now - last < _RECENT_TTL_SECONDS:
        return False
    _RECENT_USER_POSTS[k] = now
    return True

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


def _get_uid_from_any(request: Request) -> str | None:
    auth_header = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        tok = auth_header.split(" ", 1)[1].strip()
        # Try session token (signed with STREAM_API_SECRET)
        if API_SECRET:
            try:
                payload = jwt.decode(tok, API_SECRET, algorithms=["HS256"])  # type: ignore
                uid = str(payload.get("uid") or payload.get("owner_uid") or "").strip()
                if uid:
                    return uid
            except Exception:
                pass
        # Try collaborator token
        if COLLAB_JWT_SECRET:
            try:
                payload = jwt.decode(tok, COLLAB_JWT_SECRET, algorithms=["HS256"])  # type: ignore
                uid = str(payload.get("owner_uid") or payload.get("uid") or "").strip()
                if uid:
                    return uid
            except Exception:
                pass
    # Fallback to Firebase ID token
    return get_uid_from_request(request)


@router.post("/session")
async def chat_session(request: Request, ttl_seconds: int = Body(1800, embed=True)):
    """Issue a short-lived session token for chat endpoints to reduce Firebase verification in hot loops."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not API_SECRET:
        return JSONResponse({"error": "stream_not_configured"}, status_code=500)
    try:
        ttl = max(300, int(ttl_seconds or 0))
        exp = int((datetime.utcnow() + timedelta(seconds=ttl)).timestamp())
        payload = {"uid": uid, "exp": exp}
        token = jwt.encode(payload, API_SECRET, algorithm="HS256")
        tok = token if isinstance(token, str) else token.decode("utf-8")
        return {"ok": True, "token": tok, "expires_in": ttl}
    except Exception as ex:
        logger.exception(f"chat_session failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)

@router.post("/token")
async def stream_token(request: Request, user_id: str = Body(..., embed=True), expire_seconds: int = Body(24 * 3600, embed=True), db: Session = Depends(get_db)):
    uid = _get_uid_from_any(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not API_KEY or not API_SECRET:
        return JSONResponse({"error": "stream_not_configured"}, status_code=500)
    
    # SECURITY: Verify user can only get token for themselves or their collaborators
    requested_user_id = str(user_id or "").strip()[:128]
    if not requested_user_id:
        return JSONResponse({"error": "Invalid user_id"}, status_code=400)
    
    # Allow if requesting token for self
    if requested_user_id != uid:
        # Check if requester is owner and requested_user_id is their collaborator
        try:
            collab = db.query(Collaborator).filter(
                Collaborator.owner_uid == uid,
                Collaborator.active == True
            ).all()
            normalized_ids = set()
            for c in collab:
                normalized_ids.add((c.email or "").lower().replace("@", "_").replace(".", "_")[:64])
                normalized_ids.add(c.email)
            if requested_user_id not in normalized_ids:
                return JSONResponse({"error": "Cannot request token for other users"}, status_code=403)
        except Exception:
            return JSONResponse({"error": "Cannot request token for other users"}, status_code=403)
    
    try:
        # Cap expire_seconds to 24 hours max
        exp_secs = min(max(60, int(expire_seconds or 0)), 86400)
        exp = int((datetime.utcnow() + timedelta(seconds=exp_secs)).timestamp())
        payload = {"user_id": requested_user_id, "exp": exp}
        token = jwt.encode(payload, API_SECRET, algorithm="HS256")
        tok = token if isinstance(token, str) else token.decode("utf-8")
        return {"ok": True, "token": tok, "api_key": API_KEY}
    except Exception as ex:
        logger.exception(f"stream_token failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/users/ensure")
async def users_ensure(request: Request, users: list[dict] = Body(default=[])):
    uid = _get_uid_from_any(request)
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
            if not _should_post(uidv, presence_state or None):
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
        async with httpx.AsyncClient(timeout=Timeout(connect=5.0, read=10.0, write=10.0, pool=10.0)) as client:
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
async def group_create(request: Request, guid: str = Body(..., embed=True), name: str = Body("Collab Chat", embed=True), members: list[str] = Body(default=[]), topic: str = Body("", embed=True), db: Session = Depends(get_db)):
    owner_uid = _get_uid_from_any(request)
    if not owner_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not API_KEY or not API_SECRET:
        return JSONResponse({"error": "stream_not_configured"}, status_code=500)
    
    # SECURITY: Validate and sanitize inputs
    guid_clean = str(guid or "").strip()[:128]
    name_clean = str(name or "Collab Chat").strip()[:100]
    topic_clean = str(topic or "").strip()[:200]
    
    # SECURITY: Enforce channel naming convention - must be collab_{owner_uid}
    expected_guid = f"collab_{owner_uid}"
    if guid_clean != expected_guid:
        return JSONResponse({"error": "Channel ID must match your user ID"}, status_code=403)
    
    # SECURITY: Validate members are collaborators of this owner
    valid_members = [owner_uid]
    if members:
        try:
            collabs = db.query(Collaborator).filter(
                Collaborator.owner_uid == owner_uid,
                Collaborator.active == True
            ).all()
            valid_collab_ids = set()
            for c in collabs:
                valid_collab_ids.add((c.email or "").lower().replace("@", "_").replace(".", "_")[:64])
            for m in members:
                m_clean = str(m or "").strip()[:128]
                if m_clean and m_clean in valid_collab_ids:
                    valid_members.append(m_clean)
        except Exception as ex:
            logger.warning(f"Error validating members: {ex}")
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            create_body = {
                "type": "messaging",
                "id": guid_clean,
                "data": {"name": name_clean, **({"topic": topic_clean} if topic_clean else {})},
                "created_by_id": owner_uid,
            }
            r = await client.post(f"{BASE_URL}/channels", headers=_headers(), params={"api_key": API_KEY}, json=create_body)
            if r.status_code not in (200, 201):
                try:
                    data = r.json()
                except Exception:
                    data = {"error": r.text}
                return JSONResponse({"error": data.get("message") or data.get("error") or "failed"}, status_code=r.status_code)
            if valid_members:
                try:
                    await client.post(f"{BASE_URL}/channels/messaging/{guid_clean}/update", headers=_headers(), params={"api_key": API_KEY}, json={"add_members": list(set(valid_members))})
                except Exception:
                    pass
        return {"ok": True, "guid": guid_clean, "name": name_clean}
    except Exception as ex:
        logger.exception(f"group_create failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


def _sanitize_html(text: str) -> str:
    """Escape HTML special characters to prevent XSS."""
    if not text:
        return ""
    return (text
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;"))


@router.post("/group/invite")
async def group_invite(request: Request, guid: str = Body(..., embed=True), emails: list[str] = Body(default=[]), note: str = Body("", embed=True), db: Session = Depends(get_db)):
    owner_uid = _get_uid_from_any(request)
    if not owner_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not API_KEY or not API_SECRET:
        return JSONResponse({"error": "stream_not_configured"}, status_code=500)
    
    # SECURITY: Verify owner can only invite to their own channel
    guid_clean = str(guid or "").strip()[:128]
    expected_guid = f"collab_{owner_uid}"
    if guid_clean != expected_guid:
        return JSONResponse({"error": "Can only invite to your own channel"}, status_code=403)
    
    # SECURITY: Validate emails are collaborators of this owner
    valid_emails = []
    try:
        collabs = db.query(Collaborator).filter(
            Collaborator.owner_uid == owner_uid,
            Collaborator.active == True
        ).all()
        collab_emails = {(c.email or "").lower() for c in collabs}
        for em in (emails or [])[:50]:  # Limit to 50 emails
            em_clean = str(em or "").strip().lower()[:255]
            if em_clean and "@" in em_clean and em_clean in collab_emails:
                valid_emails.append(em_clean)
    except Exception as ex:
        logger.warning(f"Error validating invite emails: {ex}")
    
    if not valid_emails:
        return JSONResponse({"error": "No valid collaborator emails provided"}, status_code=400)
    
    try:
        # Normalize emails -> user ids
        ids = []
        for em in valid_emails:
            local = em.split("@")[0]
            ids.append("".join([c for c in local if c.isalnum() or c == "_"])[:64])

        async with httpx.AsyncClient(timeout=10.0) as client:
            # Ensure users
            payload_users = {}
            for uidv in ids:
                if _should_post(uidv, None):
                    payload_users[uidv] = {"id": uidv}
            if payload_users:
                try:
                    await client.post(f"{BASE_URL}/users", headers=_headers(), params={"api_key": API_KEY}, json={"users": payload_users})
                except Exception:
                    pass
            # Add to channel
            if ids:
                try:
                    await client.post(f"{BASE_URL}/channels/messaging/{guid_clean}/update", headers=_headers(), params={"api_key": API_KEY}, json={"add_members": ids})
                except Exception:
                    pass

        origin = (os.getenv("FRONTEND_ORIGIN", "") or "").strip() or ""
        join_url = f"{origin}/collab-chat?guid={guid_clean}" if origin else f"/collab-chat?guid={guid_clean}"
        subject = "You've been invited to a collaboration chat"
        
        # SECURITY: Sanitize note to prevent XSS
        note_clean = _sanitize_html(str(note or "").strip()[:500])
        
        html_body = render_email(
            "email_basic.html",
            title="Collaboration Chat Invitation",
            intro=(
                "You have been invited to join a collaboration chat. Click the button below to join." +
                ("" if not note_clean else (f"<br><br>Note from owner: {note_clean}"))
            ),
            button_label="Join Chat",
            button_url=join_url,
        )
        sent = 0
        for em in valid_emails:
            try:
                send_email_smtp(to_email=em, subject=subject, html=html_body)
                sent += 1
            except Exception as ex:
                logger.warning(f"chat invite email failed to {em}: {ex}")
        return {"ok": True, "sent": sent, "guid": guid_clean}
    except Exception as ex:
        logger.exception(f"group_invite failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


def _extract_owner_uid_from_channel(channel_id: str) -> str:
    """Extract owner UID from channel ID format: collab_{owner_uid}"""
    if channel_id.startswith("collab_"):
        return channel_id[7:]  # Remove "collab_" prefix
    return ""


@router.post("/group/join")
async def group_join(request: Request, guid: str = Body(..., embed=True), db: Session = Depends(get_db)):
    uid = _get_uid_from_any(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not API_KEY or not API_SECRET:
        return JSONResponse({"error": "stream_not_configured"}, status_code=500)
    
    # SECURITY: Validate guid format and extract owner
    guid_clean = str(guid or "").strip()[:128]
    owner_uid = _extract_owner_uid_from_channel(guid_clean)
    if not owner_uid:
        return JSONResponse({"error": "Invalid channel ID"}, status_code=400)
    
    # SECURITY: Verify user is either the owner or a collaborator
    is_owner = (uid == owner_uid)
    is_collaborator = False
    
    if not is_owner:
        try:
            collabs = db.query(Collaborator).filter(
                Collaborator.owner_uid == owner_uid,
                Collaborator.active == True
            ).all()
            for c in collabs:
                normalized = (c.email or "").lower().replace("@", "_").replace(".", "_")[:64]
                if uid == normalized or uid == c.email:
                    is_collaborator = True
                    break
        except Exception as ex:
            logger.warning(f"Error checking collaborator status: {ex}")
    
    if not is_owner and not is_collaborator:
        return JSONResponse({"error": "Not authorized to join this channel"}, status_code=403)
    
    try:
        async with httpx.AsyncClient(timeout=Timeout(connect=5.0, read=10.0, write=10.0, pool=10.0)) as client:
            try:
                display = ""
                image = ""
                try:
                    urec = db.query(User).filter(User.uid == uid).first() if db else None
                    if urec:
                        display = (urec.display_name or "").strip()[:100]
                        image = (urec.photo_url or "").strip()[:500]
                except Exception:
                    pass
                last_ex = None
                for attempt in range(3):
                    try:
                        if _should_post(uid, None):
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
            r = await client.post(f"{BASE_URL}/channels/messaging/{guid_clean}/update", headers=_headers(), params={"api_key": API_KEY}, json={"add_members": [uid]})
            if r.status_code not in (200, 201):
                try:
                    data = r.json()
                except Exception:
                    data = {"error": r.text}
                # Treat already exists as success
                msg = str(data.get("message") or data.get("error") or "")
                if "already" not in msg.lower():
                    return JSONResponse({"error": msg or "failed"}, status_code=400)
        return {"ok": True, "guid": guid_clean, "uid": uid}
    except Exception as ex:
        logger.exception(f"group_join failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/group/delete")
async def group_delete(request: Request, guid: str = Body(..., embed=True)):
    uid = _get_uid_from_any(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not API_KEY or not API_SECRET:
        return JSONResponse({"error": "stream_not_configured"}, status_code=500)
    
    # SECURITY: Only owner can delete their channel
    guid_clean = str(guid or "").strip()[:128]
    expected_guid = f"collab_{uid}"
    if guid_clean != expected_guid:
        return JSONResponse({"error": "Can only delete your own channel"}, status_code=403)
    
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.delete(f"{BASE_URL}/channels/messaging/{guid_clean}", headers=_headers(), params={"api_key": API_KEY})
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


# ============================================================================
# VIDEO/AUDIO CALL ENDPOINTS
# ============================================================================

VIDEO_BASE_URL = "https://video.stream-io-api.com"


def _video_server_token() -> str:
    """Generate server token for Stream Video API."""
    if not API_SECRET:
        return ""
    payload = {"server": True, "exp": int((datetime.utcnow() + timedelta(hours=24)).timestamp())}
    try:
        token = jwt.encode(payload, API_SECRET, algorithm="HS256")
        return token if isinstance(token, str) else token.decode("utf-8")
    except Exception as ex:
        logger.warning(f"video server token failed: {ex}")
        return ""


def _video_headers() -> dict:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Stream-Auth-Type": "jwt",
        "Authorization": _video_server_token(),
        "api_key": API_KEY,
    }


@router.post("/video/token")
async def video_token(request: Request, user_id: str = Body(..., embed=True), db: Session = Depends(get_db)):
    """Generate a token for Stream Video SDK.
    
    Supports both owners and collaborators:
    - Owners can get tokens for themselves or their collaborators
    - Collaborators can get tokens for themselves (validated via session token)
    """
    uid = _get_uid_from_any(request)
    if not API_KEY or not API_SECRET:
        return JSONResponse({"error": "stream_not_configured"}, status_code=500)
    
    requested_user_id = str(user_id or "").strip()[:128]
    if not requested_user_id:
        return JSONResponse({"error": "Invalid user_id"}, status_code=400)
    
    # Check if this is a collaborator request (no Firebase UID but has session token)
    auth_header = request.headers.get("authorization") or request.headers.get("Authorization")
    is_collab_request = False
    collab_owner_uid = None
    
    if auth_header and auth_header.lower().startswith("bearer "):
        tok = auth_header.split(" ", 1)[1].strip()
        # Try to decode as session token to get owner_uid for collaborators
        if API_SECRET:
            try:
                payload = jwt.decode(tok, API_SECRET, algorithms=["HS256"])
                collab_owner_uid = str(payload.get("owner_uid") or payload.get("uid") or "").strip()
                # If the requested user_id matches a normalized email pattern, it's likely a collaborator
                if requested_user_id and "_" in requested_user_id and not requested_user_id.startswith("collab_"):
                    is_collab_request = True
            except Exception:
                pass
    
    # Validate the request
    if not uid and not is_collab_request:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    # For collaborators, verify they're requesting their own token
    if is_collab_request:
        # Collaborator can only get token for themselves
        # The requested_user_id should be their normalized email
        if collab_owner_uid:
            try:
                collabs = db.query(Collaborator).filter(
                    Collaborator.owner_uid == collab_owner_uid,
                    Collaborator.active == True
                ).all()
                valid_ids = set()
                for c in collabs:
                    normalized = (c.email or "").lower().replace("@", "_").replace(".", "_")[:64]
                    valid_ids.add(normalized)
                if requested_user_id not in valid_ids:
                    return JSONResponse({"error": "Invalid collaborator"}, status_code=403)
            except Exception:
                return JSONResponse({"error": "Validation failed"}, status_code=403)
    elif uid and requested_user_id != uid:
        # Owner requesting token for someone else - verify it's their collaborator
        try:
            collab = db.query(Collaborator).filter(
                Collaborator.owner_uid == uid,
                Collaborator.active == True
            ).all()
            normalized_ids = set()
            for c in collab:
                normalized_ids.add((c.email or "").lower().replace("@", "_").replace(".", "_")[:64])
                normalized_ids.add(c.email)
            if requested_user_id not in normalized_ids:
                return JSONResponse({"error": "Cannot request token for other users"}, status_code=403)
        except Exception:
            return JSONResponse({"error": "Cannot request token for other users"}, status_code=403)
    
    try:
        exp = int((datetime.utcnow() + timedelta(hours=24)).timestamp())
        iat = int(datetime.utcnow().timestamp())
        payload = {
            "user_id": requested_user_id,
            "exp": exp,
            "iat": iat,
        }
        token = jwt.encode(payload, API_SECRET, algorithm="HS256")
        tok = token if isinstance(token, str) else token.decode("utf-8")
        return {"ok": True, "token": tok, "api_key": API_KEY}
    except Exception as ex:
        logger.exception(f"video_token failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/video/call/create")
async def video_call_create(
    request: Request,
    call_id: str = Body(..., embed=True),
    call_type: str = Body("default", embed=True),
    members: list[str] = Body(default=[]),
    db: Session = Depends(get_db)
):
    """Create a video/audio call."""
    owner_uid = _get_uid_from_any(request)
    if not owner_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not API_KEY or not API_SECRET:
        return JSONResponse({"error": "stream_not_configured"}, status_code=500)
    
    call_id_clean = str(call_id or "").strip()[:128]
    call_type_clean = str(call_type or "default").strip()[:32]
    
    # Validate members are collaborators
    valid_members = [{"user_id": owner_uid, "role": "admin"}]
    if members:
        try:
            collabs = db.query(Collaborator).filter(
                Collaborator.owner_uid == owner_uid,
                Collaborator.active == True
            ).all()
            valid_collab_ids = set()
            for c in collabs:
                valid_collab_ids.add((c.email or "").lower().replace("@", "_").replace(".", "_")[:64])
            for m in members:
                m_clean = str(m or "").strip()[:128]
                if m_clean and m_clean in valid_collab_ids:
                    valid_members.append({"user_id": m_clean, "role": "user"})
        except Exception as ex:
            logger.warning(f"Error validating call members: {ex}")
    
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Create call via Stream Video API
            create_body = {
                "data": {
                    "created_by_id": owner_uid,
                    "members": valid_members,
                },
                "ring": True,
            }
            r = await client.post(
                f"{VIDEO_BASE_URL}/video/call/{call_type_clean}/{call_id_clean}",
                headers=_video_headers(),
                params={"api_key": API_KEY},
                json=create_body
            )
            if r.status_code not in (200, 201):
                try:
                    data = r.json()
                except Exception:
                    data = {"error": r.text}
                return JSONResponse({"error": data.get("message") or data.get("error") or "failed"}, status_code=r.status_code)
            
            result = r.json()
            return {"ok": True, "call_id": call_id_clean, "call_type": call_type_clean, "call": result.get("call", {})}
    except Exception as ex:
        logger.exception(f"video_call_create failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/video/call/join")
async def video_call_join(
    request: Request,
    call_id: str = Body(..., embed=True),
    call_type: str = Body("default", embed=True),
    db: Session = Depends(get_db)
):
    """Join an existing video/audio call."""
    uid = _get_uid_from_any(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not API_KEY or not API_SECRET:
        return JSONResponse({"error": "stream_not_configured"}, status_code=500)
    
    call_id_clean = str(call_id or "").strip()[:128]
    call_type_clean = str(call_type or "default").strip()[:32]
    
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            # Get call to verify it exists and user can join
            r = await client.get(
                f"{VIDEO_BASE_URL}/video/call/{call_type_clean}/{call_id_clean}",
                headers=_video_headers(),
                params={"api_key": API_KEY}
            )
            if r.status_code not in (200, 201):
                try:
                    data = r.json()
                except Exception:
                    data = {"error": r.text}
                return JSONResponse({"error": data.get("message") or data.get("error") or "Call not found"}, status_code=r.status_code)
            
            call_data = r.json()
            return {"ok": True, "call_id": call_id_clean, "call_type": call_type_clean, "call": call_data.get("call", {})}
    except Exception as ex:
        logger.exception(f"video_call_join failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/video/call/end")
async def video_call_end(
    request: Request,
    call_id: str = Body(..., embed=True),
    call_type: str = Body("default", embed=True)
):
    """End a video/audio call."""
    uid = _get_uid_from_any(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    if not API_KEY or not API_SECRET:
        return JSONResponse({"error": "stream_not_configured"}, status_code=500)
    
    call_id_clean = str(call_id or "").strip()[:128]
    call_type_clean = str(call_type or "default").strip()[:32]
    
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{VIDEO_BASE_URL}/video/call/{call_type_clean}/{call_id_clean}/end",
                headers=_video_headers(),
                params={"api_key": API_KEY},
                json={}
            )
            if r.status_code not in (200, 201):
                try:
                    data = r.json()
                except Exception:
                    data = {"error": r.text}
                return JSONResponse({"error": data.get("message") or data.get("error") or "failed"}, status_code=r.status_code)
            
            return {"ok": True}
    except Exception as ex:
        logger.exception(f"video_call_end failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)
