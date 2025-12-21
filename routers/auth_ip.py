from fastapi import APIRouter, Body, Request, Depends, BackgroundTasks
from fastapi.responses import JSONResponse
from core.auth import firebase_enabled, fb_auth, get_uid_from_request  # type: ignore
from core.config import logger
from datetime import datetime
from sqlalchemy.orm import Session
from core.database import get_db
from models.user import User
from models.login_history import LoginHistory
from utils.emailing import render_email, send_email_smtp
from utils.login_tracker import (
    track_login_event,
    get_client_ip,
    get_user_agent,
    detect_login_source,
    get_ip_location,
)
import httpx

router = APIRouter(prefix="/api/auth/ip", tags=["auth-ip"])


def send_new_ip_login_email(email: str, display_name: str, ip: str, city: str, country: str):
    """Send email notification about login from new IP."""
    try:
        location = f"{city}, {country}" if city != "Unknown" else "Unknown location"
        login_time = datetime.utcnow().strftime("%B %d, %Y at %H:%M UTC")
        
        html = render_email(
            "email_basic.html",
            title="New Login Detected",
            intro=f"""Hi {display_name or 'there'},<br><br>
We noticed a login to your account from a new location:<br><br>
<strong>IP Address:</strong> {ip}<br>
<strong>Location:</strong> {location}<br>
<strong>Time:</strong> {login_time}<br><br>
If this was you, no action is needed. If you don't recognize this activity, please secure your account by changing your password immediately.""",
            button_label="",
            button_url="",
            footer_note="This is an automated security notification. If you have any concerns, please contact support.",
        )
        text = f"""Hi {display_name or 'there'},

We noticed a login to your account from a new location:

IP Address: {ip}
Location: {location}
Time: {login_time}

If this was you, no action is needed. If you don't recognize this activity, please secure your account by changing your password immediately.

This is an automated security notification."""

        send_email_smtp(email, "New Login Detected - Security Alert", html, text)
        logger.info(f"[auth_ip] Sent new IP login notification to {email} for IP {ip}")
    except Exception as ex:
        logger.exception(f"[auth_ip] Failed to send new IP login email: {ex}") 


@router.post("/register-signup")
async def register_signup(payload: dict = Body(...), db: Session = Depends(get_db)):
    """
    Create a new user via Firebase Admin.
    Expected JSON body: { "email": str, "password": str, "display_name"?: str }
    Returns: { ok: true, uid: str } on success
    """
    email = str((payload or {}).get("email") or "").strip()
    password = str((payload or {}).get("password") or "").strip()
    display_name = str((payload or {}).get("display_name") or "").strip() or None

    if not email or "@" not in email:
        return JSONResponse({"error": "valid email required"}, status_code=400)
    if not password or len(password) < 6:
        return JSONResponse({"error": "password must be at least 6 characters"}, status_code=400)

    if not firebase_enabled or not fb_auth:
        return JSONResponse({"error": "auth unavailable"}, status_code=500)

    try:
        user = fb_auth.create_user(email=email, password=password, display_name=display_name)
        uid = getattr(user, "uid", None)
        # Create/update PostgreSQL user profile
        if uid:
            existing = db.query(User).filter(User.uid == uid).first()
            now = datetime.utcnow()
            if existing:
                existing.email = email.lower()
                existing.display_name = (display_name or '').strip() or existing.display_name
                existing.updated_at = now
            else:
                # Check if email already exists with different uid (shouldn't happen on signup, but be safe)
                existing_by_email = db.query(User).filter(User.email == email.lower()).first()
                if existing_by_email:
                    # Update existing user's uid
                    existing_by_email.uid = uid
                    existing_by_email.display_name = (display_name or '').strip() or existing_by_email.display_name
                    existing_by_email.updated_at = now
                else:
                    db.add(User(
                        uid=uid,
                        email=email.lower(),
                        display_name=(display_name or '').strip() or None,
                        plan='free',
                    ))
            db.commit()
        return {"ok": True, "uid": uid}
    except Exception as ex:
        logger.warning(f"register-signup failed for {email}: {ex}")
        # Map duplicate email to a user-friendly message
        msg = (getattr(ex, "message", None) or str(ex) or "").lower()
        if any(s in msg for s in ("email already exists", "email-already-in-use", "email already in use", "email_exists", "email exists")):
            return JSONResponse({"error": "This email is already used by another account"}, status_code=400)
        return JSONResponse({"error": "Failed to create account"}, status_code=400)


@router.post("/last-login")
async def last_login(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        email = None
        display_name = None
        if firebase_enabled and fb_auth:
            try:
                user = fb_auth.get_user(uid)
                email = (getattr(user, "email", None) or "").lower()
                display_name = getattr(user, "display_name", None)
            except Exception:
                pass
        
        # Get client IP and detect source
        current_ip = get_client_ip(request)
        user_agent = get_user_agent(request)
        source = detect_login_source(request)
        
        # Update PostgreSQL user last_login_at, email, and check IP
        try:
            u = db.query(User).filter(User.uid == uid).first()
            if u:
                if email:
                    u.email = email
                
                # Check if IP has changed
                previous_ip = u.last_login_ip
                ip_changed = previous_ip and previous_ip != current_ip and current_ip != "unknown"
                
                # Update login info
                u.last_login_at = datetime.utcnow()
                u.last_login_ip = current_ip
                db.commit()
                
                # Track login event and send notification in background
                async def process_login():
                    location = await get_ip_location(current_ip)
                    
                    # Store login history
                    try:
                        from core.database import SessionLocal
                        db_session = SessionLocal()
                        try:
                            login_record = LoginHistory(
                                uid=uid,
                                ip_address=current_ip,
                                city=location["city"],
                                country=location["country"],
                                country_code=location.get("country_code"),
                                user_agent=user_agent,
                                source=source,
                                success=True,
                            )
                            db_session.add(login_record)
                            db_session.commit()
                            logger.info(f"[auth_ip] Stored login history for {uid} from {current_ip} via {source}")
                        finally:
                            db_session.close()
                    except Exception as ex:
                        logger.warning(f"[auth_ip] Failed to store login history: {ex}")
                    
                    # Send notification email if IP changed
                    if ip_changed and email:
                        send_new_ip_login_email(
                            email=email,
                            display_name=display_name or u.display_name,
                            ip=current_ip,
                            city=location["city"],
                            country=location["country"]
                        )
                        logger.info(f"[auth_ip] IP changed for user {uid}: {previous_ip} -> {current_ip}")
                
                import asyncio
                asyncio.create_task(process_login())
                    
        except Exception as ex:
            logger.warning(f"[auth_ip] DB update failed: {ex}")
            db.rollback()
        return {"ok": True}
    except Exception as ex:
        logger.warning(f"last-login failed for {uid}: {ex}")
        return {"ok": False}



@router.get("/login-history")
async def get_login_history(request: Request, db: Session = Depends(get_db), limit: int = 20):
    """
    Get recent login history for the authenticated user.
    Returns list of logins with IP, city, country, country_code, and timestamp.
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        # Get recent logins, ordered by most recent first
        logins = (
            db.query(LoginHistory)
            .filter(LoginHistory.uid == uid)
            .order_by(LoginHistory.logged_in_at.desc())
            .limit(min(limit, 50))  # Cap at 50 max
            .all()
        )
        
        return {
            "logins": [login.to_dict() for login in logins],
            "count": len(logins)
        }
    except Exception as ex:
        logger.exception(f"[auth_ip] Failed to get login history for {uid}: {ex}")
        return JSONResponse({"error": "Failed to retrieve login history"}, status_code=500)


@router.post("/track-login")
async def track_login_endpoint(request: Request, db: Session = Depends(get_db)):
    """
    Track a login event. Called by desktop plugins and API clients after successful authentication.
    The source is auto-detected from User-Agent or X-Photomark-Source header.
    
    This endpoint is authenticated - requires valid Firebase token or API token.
    """
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        # Track the login event
        await track_login_event(
            request=request,
            uid=uid,
            success=True,
            update_last_login=True,
        )
        return {"ok": True}
    except Exception as ex:
        logger.warning(f"[auth_ip] track-login failed for {uid}: {ex}")
        return {"ok": False}


@router.post("/track-failed-login")
async def track_failed_login_endpoint(
    request: Request,
    payload: dict = Body(...),
):
    """
    Track a failed login attempt. Called by frontend/plugins when login fails.
    This endpoint is NOT authenticated (since login failed).
    
    Body: { "email": str, "reason": str (optional) }
    """
    email = (payload or {}).get("email", "").strip().lower()
    reason = (payload or {}).get("reason", "").strip() or "invalid_credentials"
    
    if not email:
        return JSONResponse({"error": "email required"}, status_code=400)
    
    try:
        # Track the failed login event
        await track_login_event(
            request=request,
            uid=None,
            success=False,
            failure_reason=reason,
            attempted_email=email,
            update_last_login=False,
        )
        return {"ok": True}
    except Exception as ex:
        logger.warning(f"[auth_ip] track-failed-login failed for {email}: {ex}")
        return {"ok": False}
