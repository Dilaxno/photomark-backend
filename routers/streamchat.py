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
    # Fallback to Firebase ID token (suppress warning for non-Firebase tokens)
    try:
        return get_uid_from_request(request)
    except Exception:
        return None


def _get_sender_info_from_token(request: Request) -> tuple[str | None, str | None, bool]:
    """
    Extract sender info from token.
    Returns: (sender_uid, owner_uid, is_collaborator)
    - For owner: sender_uid = owner_uid, is_collaborator = False
    - For collaborator: sender_uid = normalized_email, owner_uid = owner's uid, is_collaborator = True
    """
    auth_header = request.headers.get("authorization") or request.headers.get("Authorization")
    if auth_header and auth_header.lower().startswith("bearer "):
        tok = auth_header.split(" ", 1)[1].strip()
        # Try session token (signed with STREAM_API_SECRET) - this is for owners
        if API_SECRET:
            try:
                payload = jwt.decode(tok, API_SECRET, algorithms=["HS256"])  # type: ignore
                uid = str(payload.get("uid") or "").strip()
                if uid:
                    # Session token - uid is the owner's Firebase UID
                    return (uid, uid, False)
            except Exception:
                pass
        # Try collaborator token (signed with COLLAB_JWT_SECRET)
        if COLLAB_JWT_SECRET:
            try:
                payload = jwt.decode(tok, COLLAB_JWT_SECRET, algorithms=["HS256"])  # type: ignore
                owner_uid = str(payload.get("owner_uid") or "").strip()
                email = str(payload.get("email") or "").strip()
                if owner_uid:
                    # Collaborator token - return normalized email as sender_uid
                    sender_uid = (email or "").lower().replace("@", "_").replace(".", "_")[:64] if email else None
                    return (sender_uid, owner_uid, True)
            except Exception:
                pass
    # Fallback to Firebase ID token
    try:
        uid = get_uid_from_request(request)
        if uid:
            return (uid, uid, False)
    except Exception:
        pass
    return (None, None, False)


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


# Simple in-memory rate limit for email notifications to prevent spam
_RECENT_EMAIL_NOTIFICATIONS: dict[str, float] = {}
_EMAIL_NOTIFY_COOLDOWN_SECONDS = 60.0  # Only send one email per recipient per minute


def _should_send_email_notification(recipient_key: str) -> bool:
    """Check if we should send an email notification (rate limiting)."""
    now = datetime.utcnow().timestamp()
    last = _RECENT_EMAIL_NOTIFICATIONS.get(recipient_key, 0)
    if now - last < _EMAIL_NOTIFY_COOLDOWN_SECONDS:
        return False
    _RECENT_EMAIL_NOTIFICATIONS[recipient_key] = now
    return True


@router.post("/message/notify")
async def message_notify(
    request: Request,
    channel_id: str = Body(..., embed=True),
    message_text: str = Body("", embed=True),
    sender_name: str = Body("", embed=True),
    sender_type: str = Body("", embed=True),
    sender_email: str = Body("", embed=True),
    db: Session = Depends(get_db)
):
    """Send email notification to recipients when a new chat message is sent."""
    # Get sender info from token (for authorization)
    sender_uid = _get_uid_from_any(request)
    if not sender_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    # SECURITY: Validate and sanitize inputs
    channel_id_clean = str(channel_id or "").strip()[:128]
    message_text_clean = _sanitize_html(str(message_text or "").strip()[:500])
    sender_name_clean = _sanitize_html(str(sender_name or "").strip()[:100])
    sender_type_clean = str(sender_type or "").strip().lower()[:20]
    sender_email_clean = str(sender_email or "").strip().lower()[:255]
    
    # Extract owner UID from channel ID (format: collab_{owner_uid})
    owner_uid = _extract_owner_uid_from_channel(channel_id_clean)
    if not owner_uid:
        return JSONResponse({"error": "Invalid channel ID"}, status_code=400)
    
    # Determine if sender is owner or collaborator based on explicit sender_type
    # This is more reliable than token inspection since collaborators use owner's Firebase token
    is_owner = sender_type_clean != "collaborator"
    
    logger.info(f"[message_notify] channel={channel_id_clean}, sender_type={sender_type_clean}, is_owner={is_owner}, sender_email={sender_email_clean}")
    
    try:
        app_name = os.getenv("APP_NAME", "Photomark")
        front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
        sent_count = 0
        
        if is_owner:
            # Owner sent message - notify all active collaborators
            collabs = db.query(Collaborator).filter(
                Collaborator.owner_uid == owner_uid,
                Collaborator.active == True
            ).all()
            
            for collab in collabs:
                if not collab.email:
                    continue
                
                # Rate limit per recipient
                rate_key = f"notify:{collab.email}:{channel_id_clean}"
                if not _should_send_email_notification(rate_key):
                    continue
                
                subject = f"New message from {sender_name_clean or 'Owner'} in {app_name} Team Chat"
                intro = (
                    f"You have a new message in your team chat.<br><br>"
                    f"<b>From:</b> {sender_name_clean or 'Owner'}<br>"
                    + (f"<b>Message:</b> {message_text_clean}<br><br>" if message_text_clean else "<br>")
                    + "Click below to view and respond."
                )
                html = render_email(
                    "email_basic.html",
                    title="New Team Chat Message",
                    intro=intro,
                    button_label="Open Chat",
                    button_url=f"{front}/collab-dashboard",
                )
                try:
                    if send_email_smtp(collab.email, subject, html):
                        sent_count += 1
                except Exception as ex:
                    logger.warning(f"Failed to send chat notification to {collab.email}: {ex}")
        else:
            # Collaborator sent message - notify the owner
            owner = db.query(User).filter(User.uid == owner_uid).first()
            if owner and owner.email:
                # Rate limit per recipient
                rate_key = f"notify:{owner.email}:{channel_id_clean}"
                if _should_send_email_notification(rate_key):
                    # Try to find collaborator info for better sender name
                    collab_info = None
                    try:
                        # Find collaborator by email (most reliable) or normalized ID
                        if sender_email_clean:
                            collab_info = db.query(Collaborator).filter(
                                Collaborator.owner_uid == owner_uid,
                                Collaborator.email == sender_email_clean,
                                Collaborator.active == True
                            ).first()
                        
                        if not collab_info:
                            collabs = db.query(Collaborator).filter(
                                Collaborator.owner_uid == owner_uid,
                                Collaborator.active == True
                            ).all()
                            for c in collabs:
                                normalized = (c.email or "").lower().replace("@", "_").replace(".", "_")[:64]
                                alt_normalized = (c.email or "").lower().replace("[^a-z0-9]", "_")[:64]
                                if sender_uid == normalized or sender_uid == alt_normalized or sender_uid == c.email:
                                    collab_info = c
                                    break
                    except Exception:
                        pass
                    
                    # Build display name: Name:Role or email
                    if collab_info:
                        if collab_info.name:
                            display_sender = f"{collab_info.name}:{collab_info.role}"
                        else:
                            display_sender = f"{collab_info.email}:{collab_info.role}"
                    else:
                        display_sender = sender_name_clean or "Collaborator"
                    
                    subject = f"New message from {display_sender} in {app_name} Team Chat"
                    intro = (
                        f"You have a new message in your team chat.<br><br>"
                        f"<b>From:</b> {display_sender}<br>"
                        + (f"<b>Message:</b> {message_text_clean}<br><br>" if message_text_clean else "<br>")
                        + "Click below to view and respond."
                    )
                    html = render_email(
                        "email_basic.html",
                        title="New Team Chat Message",
                        intro=intro,
                        button_label="Open Chat",
                        button_url=f"{front}/collaboration",
                    )
                    try:
                        if send_email_smtp(owner.email, subject, html):
                            sent_count += 1
                    except Exception as ex:
                        logger.warning(f"Failed to send chat notification to owner {owner.email}: {ex}")
        
        return {"ok": True, "sent": sent_count}
    except Exception as ex:
        logger.exception(f"message_notify failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


# ============================================================================
# WORKSPACE SYNC ENDPOINTS
# ============================================================================
# Workspaces are stored per owner and synced to collaborators who are members

# In-memory workspace storage (in production, use database)
# Format: { owner_uid: [workspace1, workspace2, ...] }
_WORKSPACES: dict[str, list[dict]] = {}


@router.get("/workspaces")
async def get_workspaces(request: Request, db: Session = Depends(get_db)):
    """
    Get workspaces for the current user.
    - For owners: returns all their workspaces
    - For collaborators: returns workspaces they are members of (filtered by their email)
    """
    sender_uid, owner_uid, is_collaborator = _get_sender_info_from_token(request)
    if not sender_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        if is_collaborator:
            # Collaborator: get owner's workspaces and filter to ones they're part of
            owner_workspaces = _WORKSPACES.get(owner_uid, [])
            
            # Get collaborator's email from database
            collab_email = None
            try:
                collabs = db.query(Collaborator).filter(
                    Collaborator.owner_uid == owner_uid,
                    Collaborator.active == True
                ).all()
                for c in collabs:
                    normalized = (c.email or "").lower().replace("@", "_").replace(".", "_")[:64]
                    if sender_uid == normalized:
                        collab_email = c.email
                        break
            except Exception:
                pass
            
            if not collab_email:
                return {"ok": True, "workspaces": [], "is_collaborator": True}
            
            # Filter workspaces: group workspaces where they're a member, or direct workspaces specifically for them
            my_workspaces = []
            for ws in owner_workspaces:
                ws_type = ws.get("type", "group")
                members = ws.get("members", [])
                
                if ws_type == "group":
                    # Group workspace: show if collaborator is a member
                    if collab_email in members:
                        my_workspaces.append(ws)
                else:
                    # Direct (1:1) workspace: only show if this collaborator is the specific member
                    if len(members) == 1 and members[0] == collab_email:
                        my_workspaces.append(ws)
            
            return {"ok": True, "workspaces": my_workspaces, "is_collaborator": True, "owner_uid": owner_uid}
        else:
            # Owner: return all their workspaces
            workspaces = _WORKSPACES.get(sender_uid, [])
            return {"ok": True, "workspaces": workspaces, "is_collaborator": False}
    except Exception as ex:
        logger.exception(f"get_workspaces failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/workspaces")
async def save_workspaces(request: Request, workspaces: list[dict] = Body(default=[])):
    """
    Save workspaces for the owner. Only owners can save workspaces.
    """
    sender_uid, owner_uid, is_collaborator = _get_sender_info_from_token(request)
    if not sender_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if is_collaborator:
        return JSONResponse({"error": "Only owners can save workspaces"}, status_code=403)
    
    try:
        # Validate and sanitize workspaces
        clean_workspaces = []
        for ws in (workspaces or [])[:100]:  # Limit to 100 workspaces
            if not isinstance(ws, dict):
                continue
            clean_ws = {
                "id": str(ws.get("id", "")).strip()[:128],
                "name": str(ws.get("name", "")).strip()[:100],
                "type": str(ws.get("type", "group")).strip()[:20],
                "members": [str(m).strip()[:255] for m in (ws.get("members") or [])[:50]],
                "owner_uid": sender_uid,
                "created_at": str(ws.get("created_at", "")).strip()[:50],
                "last_message_at": str(ws.get("last_message_at", "")).strip()[:50] if ws.get("last_message_at") else None,
            }
            if clean_ws["id"]:
                clean_workspaces.append(clean_ws)
        
        _WORKSPACES[sender_uid] = clean_workspaces
        return {"ok": True, "count": len(clean_workspaces)}
    except Exception as ex:
        logger.exception(f"save_workspaces failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/workspaces/create")
async def create_workspace(
    request: Request,
    name: str = Body(..., embed=True),
    workspace_type: str = Body("direct", embed=True),
    members: list[str] = Body(default=[]),
    db: Session = Depends(get_db)
):
    """
    Create a new workspace. Only owners can create workspaces.
    """
    sender_uid, owner_uid, is_collaborator = _get_sender_info_from_token(request)
    if not sender_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if is_collaborator:
        return JSONResponse({"error": "Only owners can create workspaces"}, status_code=403)
    
    try:
        # Validate members are collaborators of this owner
        valid_members = []
        try:
            collabs = db.query(Collaborator).filter(
                Collaborator.owner_uid == sender_uid,
                Collaborator.active == True
            ).all()
            collab_emails = {(c.email or "").lower() for c in collabs}
            for m in (members or [])[:50]:
                m_clean = str(m or "").strip().lower()[:255]
                if m_clean and m_clean in collab_emails:
                    valid_members.append(m_clean)
        except Exception as ex:
            logger.warning(f"Error validating workspace members: {ex}")
        
        if not valid_members:
            return JSONResponse({"error": "At least one valid collaborator member required"}, status_code=400)
        
        # Generate workspace ID
        import time
        import random
        import string
        ws_id = f"ws_{int(time.time())}_{(''.join(random.choices(string.ascii_lowercase + string.digits, k=8)))}"
        
        new_workspace = {
            "id": ws_id,
            "name": str(name or "").strip()[:100] or (valid_members[0] if workspace_type == "direct" else "Group Chat"),
            "type": "direct" if workspace_type == "direct" else "group",
            "members": valid_members,
            "owner_uid": sender_uid,
            "created_at": datetime.utcnow().isoformat(),
            "last_message_at": None,
        }
        
        # Add to owner's workspaces
        if sender_uid not in _WORKSPACES:
            _WORKSPACES[sender_uid] = []
        _WORKSPACES[sender_uid].insert(0, new_workspace)
        
        return {"ok": True, "workspace": new_workspace}
    except Exception as ex:
        logger.exception(f"create_workspace failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.delete("/workspaces/{workspace_id}")
async def delete_workspace(request: Request, workspace_id: str):
    """
    Delete a workspace. Only owners can delete their workspaces.
    """
    sender_uid, owner_uid, is_collaborator = _get_sender_info_from_token(request)
    if not sender_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if is_collaborator:
        return JSONResponse({"error": "Only owners can delete workspaces"}, status_code=403)
    
    try:
        ws_id_clean = str(workspace_id or "").strip()[:128]
        if sender_uid in _WORKSPACES:
            _WORKSPACES[sender_uid] = [ws for ws in _WORKSPACES[sender_uid] if ws.get("id") != ws_id_clean]
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"delete_workspace failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/workspace/invite")
async def workspace_invite(
    request: Request,
    workspace_name: str = Body(..., embed=True),
    workspace_type: str = Body("direct", embed=True),
    member_emails: list[str] = Body(default=[]),
    db: Session = Depends(get_db)
):
    """
    Send email invitations to collaborators when they are added to a workspace.
    Only owners can send workspace invitations.
    """
    sender_uid, owner_uid, is_collaborator = _get_sender_info_from_token(request)
    if not sender_uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    if is_collaborator:
        return JSONResponse({"error": "Only owners can send workspace invitations"}, status_code=403)
    
    try:
        # Get owner info
        owner = db.query(User).filter(User.uid == sender_uid).first()
        owner_name = (owner.display_name if owner else None) or "Your team owner"
        
        # Validate member emails are collaborators of this owner
        valid_emails = []
        try:
            collabs = db.query(Collaborator).filter(
                Collaborator.owner_uid == sender_uid,
                Collaborator.active == True
            ).all()
            collab_emails = {(c.email or "").lower() for c in collabs}
            for em in (member_emails or [])[:50]:
                em_clean = str(em or "").strip().lower()[:255]
                if em_clean and "@" in em_clean and em_clean in collab_emails:
                    valid_emails.append(em_clean)
        except Exception as ex:
            logger.warning(f"Error validating invite emails: {ex}")
        
        if not valid_emails:
            return {"ok": True, "sent": 0, "message": "No valid collaborator emails to notify"}
        
        # Send invitation emails
        app_name = os.getenv("APP_NAME", "Photomark")
        front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
        
        workspace_name_clean = _sanitize_html(str(workspace_name or "").strip()[:100])
        workspace_type_clean = str(workspace_type or "direct").strip().lower()
        
        sent_count = 0
        for email in valid_emails:
            try:
                # Rate limit per recipient
                rate_key = f"ws_invite:{email}:{workspace_name_clean}"
                if not _should_send_email_notification(rate_key):
                    continue
                
                if workspace_type_clean == "direct":
                    subject = f"{owner_name} started a direct chat with you on {app_name}"
                    intro = (
                        f"<b>{owner_name}</b> has started a direct conversation with you in {app_name} Team Chat.<br><br>"
                        f"Click below to open the chat and start messaging."
                    )
                else:
                    subject = f"You've been added to '{workspace_name_clean}' workspace on {app_name}"
                    intro = (
                        f"<b>{owner_name}</b> has added you to the <b>{workspace_name_clean}</b> workspace in {app_name} Team Chat.<br><br>"
                        f"Click below to join the conversation with your team."
                    )
                
                html = render_email(
                    "email_basic.html",
                    title="Team Chat Invitation",
                    intro=intro,
                    button_label="Open Team Chat",
                    button_url=f"{front}/collab-dashboard",
                )
                
                if send_email_smtp(email, subject, html):
                    sent_count += 1
            except Exception as ex:
                logger.warning(f"Failed to send workspace invite to {email}: {ex}")
        
        return {"ok": True, "sent": sent_count}
    except Exception as ex:
        logger.exception(f"workspace_invite failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)
