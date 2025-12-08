"""
Website Builder Settings Router
Handles persistence for the Website page builder (similar to Squarespace)
"""
import json
from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import text

from core.config import logger
from core.auth import get_uid_from_request
from core.database import get_db

router = APIRouter(prefix="/api/website", tags=["website"])


def _ensure_website_table(db: Session):
    """Create website_settings table if it doesn't exist."""
    db.execute(text(
        """
        CREATE TABLE IF NOT EXISTS website_settings (
            uid VARCHAR(64) PRIMARY KEY,
            data JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
        """
    ))
    db.commit()


def _get_website_settings(db: Session, uid: str) -> dict | None:
    """Get website settings for a user."""
    _ensure_website_table(db)
    row = db.execute(
        text("SELECT data FROM website_settings WHERE uid = :uid"),
        {"uid": uid}
    ).mappings().first()
    
    if row:
        data = row.get("data")
        if isinstance(data, str):
            try:
                return json.loads(data)
            except Exception:
                return {}
        return data if isinstance(data, dict) else {}
    return None


def _save_website_settings(db: Session, uid: str, data: dict):
    """Save website settings for a user."""
    _ensure_website_table(db)
    data_json = json.dumps(data) if isinstance(data, dict) else '{}'
    db.execute(text(
        """
        INSERT INTO website_settings (uid, data)
        VALUES (:uid, CAST(:data_json AS jsonb))
        ON CONFLICT (uid) DO UPDATE SET
            data = EXCLUDED.data,
            updated_at = NOW();
        """
    ), {"uid": uid, "data_json": data_json})
    db.commit()


@router.get("/settings")
async def get_settings(request: Request, db: Session = Depends(get_db)):
    """Get website settings for the authenticated user."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        data = _get_website_settings(db, uid)
        return {"ok": True, "data": data}
    except Exception as ex:
        logger.warning(f"get_website_settings failed: {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


@router.post("/settings")
async def save_settings(request: Request, payload: dict, db: Session = Depends(get_db)):
    """Save website settings for the authenticated user."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        data = payload.get("data")
        if not isinstance(data, dict):
            return JSONResponse({"error": "Invalid data format"}, status_code=400)
        
        _save_website_settings(db, uid, data)
        return {"ok": True}
    except Exception as ex:
        logger.warning(f"save_website_settings failed: {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)
