from fastapi import APIRouter, Body, Request, Depends
from fastapi.responses import JSONResponse
from core.auth import firebase_enabled, fb_auth, get_uid_from_request  # type: ignore
from core.config import logger
from datetime import datetime
from sqlalchemy.orm import Session
from core.database import get_db
from models.user import User

router = APIRouter(prefix="/api/auth/ip", tags=["auth-ip"]) 


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
async def last_login(request: Request, db: Session = Depends(get_db)):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        email = None
        if firebase_enabled and fb_auth:
            try:
                user = fb_auth.get_user(uid)
                email = (getattr(user, "email", None) or "").lower()
            except Exception:
                pass
        # Update PostgreSQL user last_login_at and email if we have it
        try:
            u = db.query(User).filter(User.uid == uid).first()
            if u:
                if email:
                    u.email = email
                u.last_login_at = datetime.utcnow()
                db.commit()
        except Exception:
            db.rollback()
        return {"ok": True}
    except Exception as ex:
        logger.warning(f"last-login failed for {uid}: {ex}")
        return {"ok": False}
