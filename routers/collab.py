from fastapi import APIRouter, Request, Body, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
import os
from datetime import datetime, timedelta
from typing import Optional

from core.auth import get_uid_from_request, firebase_enabled, fb_auth  # type: ignore
from core.config import logger
from core.database import get_db
from models.collaborator import Collaborator
from utils.emailing import render_email, send_email_smtp

import bcrypt
import jwt
from uuid import uuid4

router = APIRouter(prefix="/api/collab", tags=["collaboration"])  # owner-managed collaborator accounts


ALLOWED_ROLES = {
    "Vendor",
    "Vaults/client proofing Manager",
    "Editor/Retoucher",
    "General Admin",
    "gallery manager",
}

COLLAB_JWT_SECRET = (os.getenv("COLLAB_JWT_SECRET", "") or os.getenv("SECRET_KEY", "")).strip()
COLLAB_JWT_ISSUER = os.getenv("COLLAB_JWT_ISSUER", "photomark.collab")


@router.post("/invite")
async def collab_invite(
    request: Request,
    email: str = Body(..., embed=True),
    role: str = Body(..., embed=True),
    name: Optional[str] = Body(None, embed=True),
    db: Session = Depends(get_db),
):
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    em = (email or "").strip().lower()
    ro = (role or "").strip()
    if not em or "@" not in em:
        return JSONResponse({"error": "Valid email required"}, status_code=400)
    if ro not in ALLOWED_ROLES:
        return JSONResponse({"error": "Invalid role"}, status_code=400)

    try:
        raw_pw = uuid4().hex[:12]
        salt = bcrypt.gensalt()
        pw_hash = bcrypt.hashpw(raw_pw.encode("utf-8"), salt).decode("utf-8")

        existing = db.query(Collaborator).filter(Collaborator.email == em).first()
        if existing:
            existing.role = ro
            existing.owner_uid = uid
            existing.name = (name or existing.name)
            existing.password_hash = pw_hash
        else:
            rec = Collaborator(
                id=uuid4().hex,
                owner_uid=uid,
                email=em,
                name=(name or None),
                role=ro,
                password_hash=pw_hash,
                active=True,
            )
            db.add(rec)
        db.commit()

        app_name = os.getenv("APP_NAME", "Photomark")
        front = (os.getenv("FRONTEND_ORIGIN", "").split(",")[0].strip() or "https://photomark.cloud").rstrip("/")
        subject = f"{app_name} Collaboration Access"
        intro = (
            f"You have been invited to collaborate on {app_name}.<br><br>"
            f"Role: <b>{ro}</b><br>"
            f"Email: <b>{em}</b><br>"
            f"Password: <b>{raw_pw}</b><br><br>"
            f"Sign in as a collaborator from the Auth page and you'll have access according to your role."
        )
        html = render_email(
            "email_basic.html",
            title="Collaboration Invite",
            intro=intro,
            button_label="Open App",
            button_url=front,
        )
        ok = send_email_smtp(em, subject, html)
        if not ok:
            return JSONResponse({"error": "Failed to send email"}, status_code=500)
        return {"ok": True}
    except Exception as ex:
        logger.exception(f"collab_invite failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)


@router.post("/login")
async def collab_login(
    email: str = Body(..., embed=True),
    password: str = Body(..., embed=True),
    db: Session = Depends(get_db),
):
    em = (email or "").strip().lower()
    pw = (password or "").strip()
    if not em or not pw:
        return JSONResponse({"error": "email and password required"}, status_code=400)
    try:
        rec = db.query(Collaborator).filter(Collaborator.email == em, Collaborator.active == True).first()
        if not rec:
            return JSONResponse({"error": "not_found"}, status_code=404)
        try:
            ok = bcrypt.checkpw(pw.encode("utf-8"), rec.password_hash.encode("utf-8"))
        except Exception:
            ok = False
        if not ok:
            return JSONResponse({"error": "invalid_credentials"}, status_code=401)

        rec.last_login_at = datetime.utcnow()
        db.commit()

        token: Optional[str] = None
        if COLLAB_JWT_SECRET:
            payload = {
                "sub": rec.id,
                "email": rec.email,
                "role": rec.role,
                "owner_uid": rec.owner_uid,
                "iat": int(datetime.utcnow().timestamp()),
                "exp": int((datetime.utcnow() + timedelta(hours=24)).timestamp()),
                "iss": COLLAB_JWT_ISSUER,
            }
            try:
                token = jwt.encode(payload, COLLAB_JWT_SECRET, algorithm="HS256")
            except Exception as ex:
                logger.warning(f"collab_login jwt encode failed: {ex}")

        custom_jwt: Optional[str] = None
        if firebase_enabled and fb_auth:
            try:
                ct_bytes = fb_auth.create_custom_token(rec.owner_uid)
                custom_jwt = ct_bytes.decode("utf-8") if isinstance(ct_bytes, (bytes, bytearray)) else str(ct_bytes)
            except Exception as ex:
                logger.warning(f"collab_login custom_token failed: {ex}")

        return {"ok": True, "collab_token": token, "firebase_custom_token": custom_jwt, "role": rec.role, "owner_uid": rec.owner_uid}
    except Exception as ex:
        logger.exception(f"collab_login failed: {ex}")
        return JSONResponse({"error": str(ex)}, status_code=500)
