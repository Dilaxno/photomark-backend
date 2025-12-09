"""
Website Builder Settings Router
Handles persistence for the Website page builder (similar to Squarespace)
Stores websites per user in Neon PostgreSQL database
"""
import json
import uuid
from datetime import datetime
from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import text

from core.config import logger
from core.auth import get_uid_from_request
from core.database import get_db

router = APIRouter(prefix="/api/website", tags=["website"])


def _ensure_websites_table(db: Session):
    """Create websites table if it doesn't exist."""
    db.execute(text(
        """
        CREATE TABLE IF NOT EXISTS user_websites (
            id VARCHAR(64) PRIMARY KEY,
            uid VARCHAR(64) NOT NULL,
            name VARCHAR(255) NOT NULL DEFAULT 'My Website',
            slug VARCHAR(255),
            data JSONB NOT NULL DEFAULT '{}',
            is_published BOOLEAN DEFAULT FALSE,
            published_url VARCHAR(512),
            thumbnail_url VARCHAR(512),
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
        CREATE INDEX IF NOT EXISTS idx_user_websites_uid ON user_websites(uid);
        """
    ))
    db.commit()


def _get_user_websites(db: Session, uid: str) -> list:
    """Get all websites for a user."""
    _ensure_websites_table(db)
    rows = db.execute(
        text("""
            SELECT id, name, slug, is_published, published_url, thumbnail_url, created_at, updated_at,
                   data->>'currentPageId' as current_page_id,
                   jsonb_array_length(COALESCE(data->'pages', '[]'::jsonb)) as page_count
            FROM user_websites 
            WHERE uid = :uid 
            ORDER BY updated_at DESC
        """),
        {"uid": uid}
    ).mappings().all()
    
    return [dict(row) for row in rows]


def _get_website(db: Session, uid: str, website_id: str) -> dict | None:
    """Get a specific website for a user."""
    _ensure_websites_table(db)
    row = db.execute(
        text("SELECT * FROM user_websites WHERE id = :id AND uid = :uid"),
        {"id": website_id, "uid": uid}
    ).mappings().first()
    
    if row:
        result = dict(row)
        data = result.get("data")
        if isinstance(data, str):
            try:
                result["data"] = json.loads(data)
            except Exception:
                result["data"] = {}
        return result
    return None


def _save_website(db: Session, uid: str, website_id: str | None, data: dict, name: str = None) -> str:
    """Save or create a website for a user."""
    _ensure_websites_table(db)
    
    # Generate new ID if not provided
    if not website_id:
        website_id = f"web_{uuid.uuid4().hex[:12]}"
    
    # Extract name from data if not provided
    if not name:
        pages = data.get("pages", [])
        if pages:
            name = f"Website ({len(pages)} pages)"
        else:
            name = "My Website"
    
    data_json = json.dumps(data) if isinstance(data, dict) else '{}'
    
    db.execute(text(
        """
        INSERT INTO user_websites (id, uid, name, data, updated_at)
        VALUES (:id, :uid, :name, CAST(:data_json AS jsonb), NOW())
        ON CONFLICT (id) DO UPDATE SET
            name = COALESCE(EXCLUDED.name, user_websites.name),
            data = EXCLUDED.data,
            updated_at = NOW();
        """
    ), {"id": website_id, "uid": uid, "name": name, "data_json": data_json})
    db.commit()
    
    return website_id


def _delete_website(db: Session, uid: str, website_id: str) -> bool:
    """Delete a website for a user."""
    _ensure_websites_table(db)
    result = db.execute(
        text("DELETE FROM user_websites WHERE id = :id AND uid = :uid"),
        {"id": website_id, "uid": uid}
    )
    db.commit()
    return result.rowcount > 0


# Legacy endpoint for backward compatibility
@router.get("/settings")
async def get_settings(request: Request, db: Session = Depends(get_db)):
    """Get the most recent website settings for the authenticated user."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        websites = _get_user_websites(db, uid)
        if websites:
            # Return the most recently updated website
            website = _get_website(db, uid, websites[0]["id"])
            if website:
                return {"ok": True, "data": website.get("data"), "websiteId": website["id"]}
        return {"ok": True, "data": None}
    except Exception as ex:
        logger.warning(f"get_website_settings failed: {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


# Legacy endpoint for backward compatibility
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
        
        website_id = payload.get("websiteId")
        name = payload.get("name")
        
        # If no website_id, check if user has any websites
        if not website_id:
            websites = _get_user_websites(db, uid)
            if websites:
                website_id = websites[0]["id"]
        
        saved_id = _save_website(db, uid, website_id, data, name)
        return {"ok": True, "websiteId": saved_id}
    except Exception as ex:
        logger.warning(f"save_website_settings failed: {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


# New endpoints for multiple websites
@router.get("/list")
async def list_websites(request: Request, db: Session = Depends(get_db)):
    """List all websites for the authenticated user."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        websites = _get_user_websites(db, uid)
        return {"ok": True, "websites": websites}
    except Exception as ex:
        logger.warning(f"list_websites failed: {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


@router.get("/{website_id}")
async def get_website_by_id(website_id: str, request: Request, db: Session = Depends(get_db)):
    """Get a specific website by ID."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        website = _get_website(db, uid, website_id)
        if not website:
            return JSONResponse({"error": "Website not found"}, status_code=404)
        return {"ok": True, "website": website}
    except Exception as ex:
        logger.warning(f"get_website failed: {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


@router.post("/create")
async def create_website(request: Request, payload: dict, db: Session = Depends(get_db)):
    """Create a new website."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        name = payload.get("name", "My Website")
        data = payload.get("data", {"pages": [], "currentPageId": ""})
        
        website_id = _save_website(db, uid, None, data, name)
        return {"ok": True, "websiteId": website_id}
    except Exception as ex:
        logger.warning(f"create_website failed: {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


@router.put("/{website_id}")
async def update_website(website_id: str, request: Request, payload: dict, db: Session = Depends(get_db)):
    """Update a specific website."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        # Verify ownership
        existing = _get_website(db, uid, website_id)
        if not existing:
            return JSONResponse({"error": "Website not found"}, status_code=404)
        
        data = payload.get("data")
        name = payload.get("name")
        
        if data is not None:
            _save_website(db, uid, website_id, data, name)
        
        return {"ok": True}
    except Exception as ex:
        logger.warning(f"update_website failed: {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)


@router.delete("/{website_id}")
async def delete_website(website_id: str, request: Request, db: Session = Depends(get_db)):
    """Delete a website."""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    try:
        deleted = _delete_website(db, uid, website_id)
        if not deleted:
            return JSONResponse({"error": "Website not found"}, status_code=404)
        return {"ok": True}
    except Exception as ex:
        logger.warning(f"delete_website failed: {ex}")
        return JSONResponse({"error": "server error"}, status_code=500)
