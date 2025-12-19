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
from utils.storage import upload_bytes, get_presigned_url
from core.database import get_db
from models.portfolio import PortfolioPhoto, PortfolioSettings
from core.config import s3, R2_BUCKET

logger = logging.getLogger(__name__)
router = APIRouter()

def _get_thumbnail_url(url: str) -> Optional[str]:
    """Generate Cloudinary thumbnail URL for photos following optimization standards"""
    try:
        # Check if it's a Cloudinary URL
        if 'cloudinary.com' in url:
            # For photo thumbnails: f_auto,q_auto:best,w_600,dpr_2.0
            return url.replace('/upload/', '/upload/f_auto,q_auto:best,w_600,dpr_2.0/')
        return None
    except Exception:
        return None

def _get_optimized_photo_url(url: str, width: int = 1200) -> str:
    """Generate optimized Cloudinary URL for full photos"""
    try:
        if 'cloudinary.com' in url:
            # For full photos: f_auto,q_auto:best,w_[WIDTH],dpr_2.0
            return url.replace('/upload/', f'/upload/f_auto,q_auto:best,w_{width},dpr_2.0/')
        return url
    except Exception:
        return url

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
        
        # Add optimized URLs to photos
        photos_with_thumbs = []
        for photo in photos:
            photo_dict = photo.to_dict(include_thumb_url=True)
            if photo.url:
                # Generate thumbnail for fast loading
                photo_dict["thumb_url"] = _get_thumbnail_url(photo.url)
                # Optimize full photo URL for gallery view
                photo_dict["url"] = _get_optimized_photo_url(photo.url, width=800)
            photos_with_thumbs.append(photo_dict)
        
        return {"photos": photos_with_thumbs}
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

@router.get("/my-portfolio/public")
async def get_my_public_portfolio(
    uid: str = Depends(get_current_user_uid),
    db: Session = Depends(get_db)
):
    """Get current user's public portfolio data"""
    try:
        # Get settings
        settings = db.query(PortfolioSettings).filter(
            PortfolioSettings.uid == uid
        ).first()
        
        if not settings:
            raise HTTPException(status_code=404, detail="Portfolio not found")
        
        if not settings.is_published:
            raise HTTPException(status_code=404, detail="Portfolio not published")
        
        # Get photos
        photos = db.query(PortfolioPhoto).filter(
            PortfolioPhoto.uid == uid
        ).order_by(PortfolioPhoto.order).all()
        
        # Add optimized URLs
        photos_with_thumbs = []
        for photo in photos:
            photo_dict = photo.to_dict(include_thumb_url=True)
            if photo.url:
                photo_dict["thumb_url"] = _get_thumbnail_url(photo.url)
                photo_dict["url"] = _get_optimized_photo_url(photo.url, width=1600)
            photos_with_thumbs.append(photo_dict)
        
        return {
            "settings": settings.to_dict(),
            "photos": photos_with_thumbs,
            "uid": uid  # Include UID for easy access
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting my public portfolio: {e}")
        raise HTTPException(status_code=500, detail="Failed to get portfolio")

@router.get("/{user_identifier}/public")
async def get_public_portfolio(user_identifier: str, db: Session = Depends(get_db)):
    """Get public portfolio data for viewing"""
    try:
        # For now, treat identifier as UID (can be enhanced later for slugs)
        user_id = user_identifier
        
        logger.info(f"Looking for portfolio with user_id: {user_id}")
        
        # Get settings
        settings = db.query(PortfolioSettings).filter(
            PortfolioSettings.uid == user_id
        ).first()
        
        if not settings:
            logger.warning(f"No portfolio settings found for user_id: {user_id}")
            raise HTTPException(status_code=404, detail="Portfolio not found")
        
        if not settings.is_published:
            logger.warning(f"Portfolio exists but not published for user_id: {user_id}")
            raise HTTPException(status_code=404, detail="Portfolio not published")
        
        logger.info(f"Found published portfolio for user_id: {user_id}")
        
        # Get photos with thumbnails
        photos = db.query(PortfolioPhoto).filter(
            PortfolioPhoto.uid == user_id
        ).order_by(PortfolioPhoto.order).all()
        
        logger.info(f"Found {len(photos)} photos for portfolio")
        
        # Add optimized URLs to photos for public view
        photos_with_thumbs = []
        for photo in photos:
            photo_dict = photo.to_dict(include_thumb_url=True)
            if photo.url:
                # Generate thumbnail for fast loading
                photo_dict["thumb_url"] = _get_thumbnail_url(photo.url)
                # Optimize full photo URL for public portfolio
                photo_dict["url"] = _get_optimized_photo_url(photo.url, width=1600)
            photos_with_thumbs.append(photo_dict)
        
        return {
            "settings": settings.to_dict(),
            "photos": photos_with_thumbs
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting public portfolio: {e}")
        raise HTTPException(status_code=500, detail="Failed to get portfolio")