import os
import uuid
from datetime import datetime, timezone
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import logging
from sqlalchemy.orm import Session

from core.auth import get_uid_from_request
from utils.storage import upload_bytes
from core.database import get_db
from models.portfolio import PortfolioPhoto, PortfolioSettings

logger = logging.getLogger(__name__)
router = APIRouter()

async def get_current_user_uid(request: Request) -> str:
    uid = get_uid_from_request(request)
    if not uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return uid

class PortfolioSettingsRequest(BaseModel):
    title: str
    subtitle: Optional[str] = None
    template: str = "canvas"
    customDomain: Optional[str] = None

class PublishRequest(BaseModel):
    isPublished: bool

@router.get("/settings")
async def get_portfolio_settings(
    uid: str = Depends(get_current_user_uid),
    db: Session = Depends(get_db)
):
    """Get portfolio settings for the current user"""
    try:
        settings = db.query(PortfolioSettings).filter(
            PortfolioSettings.uid == uid
        ).first()
        
        if not settings:
            # Create default settings
            settings = PortfolioSettings(
                uid=uid,
                title="My Portfolio",
                template="canvas",
                is_published=False
            )
            db.add(settings)
            db.commit()
            db.refresh(settings)
        
        return settings.to_dict()
    except Exception as e:
        logger.error(f"Error getting portfolio settings: {e}")
        raise HTTPException(status_code=500, detail="Failed to get portfolio settings")

@router.post("/settings")
async def update_portfolio_settings(
    request: PortfolioSettingsRequest,
    uid: str = Depends(get_current_user_uid),
    db: Session = Depends(get_db)
):
    """Update portfolio settings"""
    try:
        settings = db.query(PortfolioSettings).filter(
            PortfolioSettings.uid == uid
        ).first()
        
        if not settings:
            settings = PortfolioSettings(uid=uid)
            db.add(settings)
        
        settings.title = request.title
        settings.subtitle = request.subtitle
        settings.template = request.template
        settings.custom_domain = request.customDomain
        
        db.commit()
        return {"message": "Settings updated successfully"}
    except Exception as e:
        logger.error(f"Error updating portfolio settings: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to update settings")

@router.get("/photos")
async def get_portfolio_photos(
    uid: str = Depends(get_current_user_uid),
    db: Session = Depends(get_db)
):
    """Get all photos in the portfolio"""
    try:
        photos = db.query(PortfolioPhoto).filter(
            PortfolioPhoto.uid == uid
        ).order_by(PortfolioPhoto.order).all()
        
        return {"photos": [photo.to_dict() for photo in photos]}
    except Exception as e:
        logger.error(f"Error getting portfolio photos: {e}")
        raise HTTPException(status_code=500, detail="Failed to get photos")

@router.post("/photos/upload")
async def upload_portfolio_photo(
    file: UploadFile = File(...),
    title: Optional[str] = None,
    uid: str = Depends(get_current_user_uid),
    db: Session = Depends(get_db)
):
    """Upload a photo to the portfolio"""
    try:
        if not file.content_type or not file.content_type.startswith('image/'):
            raise HTTPException(status_code=400, detail="File must be an image")
        
        # Read file content
        content = await file.read()
        
        # Generate unique filename
        photo_id = str(uuid.uuid4())
        ext = os.path.splitext(file.filename or '')[1] or '.jpg'
        key = f"users/{uid}/portfolio/{photo_id}{ext}"
        
        # Upload to storage
        upload_bytes(key, content, content_type=file.content_type)
        
        # Get next order
        max_order = db.query(PortfolioPhoto).filter(
            PortfolioPhoto.uid == uid
        ).count()
        
        # Create photo record
        photo = PortfolioPhoto(
            id=photo_id,
            uid=uid,
            url=f"https://your-cdn.com/{key}",  # Replace with actual CDN URL
            title=title,
            order=max_order,
            source="upload"
        )
        
        db.add(photo)
        db.commit()
        db.refresh(photo)
        
        return photo.to_dict()
    except Exception as e:
        logger.error(f"Error uploading portfolio photo: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to upload photo")

@router.delete("/photos/{photo_id}")
async def delete_portfolio_photo(
    photo_id: str,
    uid: str = Depends(get_current_user_uid),
    db: Session = Depends(get_db)
):
    """Delete a photo from the portfolio"""
    try:
        photo = db.query(PortfolioPhoto).filter(
            PortfolioPhoto.id == photo_id,
            PortfolioPhoto.uid == uid
        ).first()
        
        if not photo:
            raise HTTPException(status_code=404, detail="Photo not found")
        
        db.delete(photo)
        db.commit()
        
        return {"message": "Photo deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting portfolio photo: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to delete photo")

@router.post("/publish")
async def publish_portfolio(
    request: PublishRequest,
    uid: str = Depends(get_current_user_uid),
    db: Session = Depends(get_db)
):
    """Publish or unpublish portfolio"""
    try:
        settings = db.query(PortfolioSettings).filter(
            PortfolioSettings.uid == uid
        ).first()
        
        if not settings:
            settings = PortfolioSettings(
                uid=uid,
                title="My Portfolio",
                template="canvas",
                is_published=request.isPublished
            )
            db.add(settings)
        else:
            settings.is_published = request.isPublished
        
        if request.isPublished:
            settings.published_at = datetime.now(timezone.utc)
        
        db.commit()
        
        return {"message": "Portfolio published successfully" if request.isPublished else "Portfolio unpublished"}
    except Exception as e:
        logger.error(f"Error publishing portfolio: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to update portfolio status")

@router.get("/{user_identifier}/public")
async def get_public_portfolio(user_identifier: str, db: Session = Depends(get_db)):
    """Get public portfolio data for viewing"""
    try:
        # For now, treat identifier as UID (can be enhanced later for slugs)
        user_id = user_identifier
        
        # Get settings
        settings = db.query(PortfolioSettings).filter(
            PortfolioSettings.uid == user_id
        ).first()
        
        if not settings or not settings.is_published:
            raise HTTPException(status_code=404, detail="Portfolio not found or not published")
        
        # Get photos
        photos = db.query(PortfolioPhoto).filter(
            PortfolioPhoto.uid == user_id
        ).order_by(PortfolioPhoto.order).all()
        
        return {
            "settings": settings.to_dict(),
            "photos": [photo.to_dict() for photo in photos]
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting public portfolio: {e}")
        raise HTTPException(status_code=500, detail="Failed to get portfolio")