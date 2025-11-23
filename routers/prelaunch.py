from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, EmailStr
import os
from datetime import datetime, timezone

from core.config import logger
from sqlalchemy.orm import Session
from core.database import get_db
from models.prelaunch import PrelaunchSignup


# Firestore removed in Neon migration


class SubscribePayload(BaseModel):
    email: EmailStr
    name: str | None = None


router = APIRouter(prefix="/prelaunch", tags=["prelaunch"])  # e.g. POST /prelaunch/subscribe


@router.post("/subscribe")
async def subscribe(payload: SubscribePayload, request: Request, db: Session = Depends(get_db)):
    # Collect useful context
    ts = datetime.now(timezone.utc).isoformat()
    email = payload.email.strip().lower()
    name = (payload.name or "").strip()
    ip = request.headers.get("x-forwarded-for") or (request.client.host if request.client else None)
    ua = request.headers.get("user-agent", "")

    try:
        rec = db.query(PrelaunchSignup).filter(PrelaunchSignup.email == email).first()
        if rec:
            # Update basic info if changed
            rec.source = name or rec.source
            rec.ip = (ip or rec.ip)
            rec.user_agent = ua or rec.user_agent
        else:
            rec = PrelaunchSignup(email=email, source=name or None, ip=ip or None, user_agent=ua)
            db.add(rec)
        db.commit()
        return {"ok": True}
    except Exception as ex:
        db.rollback()
        logger.exception(f"prelaunch subscribe failed: {ex}")
        return JSONResponse({"ok": False, "error": "Failed to save"}, status_code=500)
