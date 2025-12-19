import os
import uuid
from datetime import datetime, timezone
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
import logging
from sqlalchemy.orm import Session

from core.auth import get_uid_from_request
from utils.storage import upload_bytes
from core.database import get_db
from models.portfolio import PortfolioPhoto, PortfolioSettings

logger = logging.getLogger(__name__)
router = APIRouter()

# FastAPI dependency to get current user UID
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
    isPublished: bool = False

class PublishRequest(BaseModel):
    isPublished: bool

class PortfolioUrlResponse(BaseModel):
    url: str
    slug: str

class AddFromGalleryRequest(BaseModel):
    photoUrls: List[str]

@router.get("/photos")
async def get_portfolio_photos(uid: str = Depends(get_current_user_uid), db: Session = Depends(get_db)):
    """Get all portfolio photos for the user"""
    try:
        photos = db.query(PortfolioPhoto).filter(
            PortfolioPhoto.uid == uid
        ).order_by(PortfolioPhoto.order).all()
        
        return {"photos": [photo.to_dict() for photo in photos]}
    
    except Exception as e:
        logger.error(f"Error getting portfolio photos: {e}")
        raise HTTPException(status_code=500, detail="Failed to get portfolio photos")

@router.post("/upload")
async def upload_portfolio_photos(
    photos: List[UploadFile] = File(...),
    uid: str = Depends(get_current_user_uid),
    db: Session = Depends(get_db)
):
    """Upload photos to portfolio"""
    try:
        if not photos:
            raise HTTPException(status_code=400, detail="No photos provided")
        
        uploaded_photos = []
        
        # Get current max order
        max_order = db.query(PortfolioPhoto).filter(
            PortfolioPhoto.uid == uid
        ).count()
        
        for i, photo in enumerate(photos):
            # Validate file type
            if not photo.content_type or not photo.content_type.startswith('image/'):
                continue
            
            # Generate unique filename
            file_ext = os.path.splitext(photo.filename or '')[1] or '.jpg'
            filename = f"portfolio/{uid}/{uuid.uuid4()}{file_ext}"
            
            # Upload to storage
            file_content = await photo.read()
            file_url = upload_bytes(filename, file_content, photo.content_type)
            
            if file_url:
                # Create database record
                portfolio_photo = PortfolioPhoto(
                    id=str(uuid.uuid4()),
                    uid=uid,
                    url=file_url,
                    title=photo.filename,
                    order=max_order + i,
                    source="upload"
                )
                db.add(portfolio_photo)
                uploaded_photos.append(portfolio_photo.to_dict())
        
        db.commit()
        return {"photos": uploaded_photos, "message": f"Uploaded {len(uploaded_photos)} photos"}
    
    except Exception as e:
        logger.error(f"Error uploading portfolio photos: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to upload photos")

@router.post("/add-from-gallery")
async def add_photos_from_gallery(
    request: AddFromGalleryRequest,
    uid: str = Depends(get_current_user_uid),
    db: Session = Depends(get_db)
):
    """Add existing photos from gallery/vaults to portfolio"""
    try:
        if not request.photoUrls:
            raise HTTPException(status_code=400, detail="No photo URLs provided")
        
        added_photos = []
        
        # Get current max order
        max_order = db.query(PortfolioPhoto).filter(
            PortfolioPhoto.uid == uid
        ).count()
        
        for i, photo_url in enumerate(request.photoUrls):
            try:
                # Create portfolio entry for existing photo
                portfolio_photo = PortfolioPhoto(
                    id=str(uuid.uuid4()),
                    uid=uid,
                    url=photo_url,
                    title=f"Photo {max_order + i + 1}",
                    order=max_order + i,
                    source="gallery"
                )
                db.add(portfolio_photo)
                added_photos.append(portfolio_photo.to_dict())
            
            except Exception as e:
                logger.warning(f"Failed to add photo {photo_url}: {e}")
                continue
        
        db.commit()
        return {"photos": added_photos, "message": f"Added {len(added_photos)} photos to portfolio"}
    
    except Exception as e:
        logger.error(f"Error adding photos from gallery: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to add photos from gallery")

@router.delete("/photos/{photo_id}")
async def delete_portfolio_photo(
    photo_id: str,
    uid: str = Depends(get_current_user_uid),
    db: Session = Depends(get_db)
):
    """Delete a portfolio photo"""
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

@router.get("/settings")
async def get_portfolio_settings(uid: str = Depends(get_current_user_uid), db: Session = Depends(get_db)):
    """Get portfolio settings"""
    try:
        settings = db.query(PortfolioSettings).filter(
            PortfolioSettings.uid == uid
        ).first()
        
        if settings:
            return {"settings": settings.to_dict()}
        else:
            # Return default settings
            return {
                "settings": {
                    "title": "My Portfolio",
                    "subtitle": "",
                    "template": "canvas",
                    "customDomain": "",
                    "isPublished": False
                }
            }
    
    except Exception as e:
        logger.error(f"Error getting portfolio settings: {e}")
        raise HTTPException(status_code=500, detail="Failed to get portfolio settings")

@router.post("/settings")
async def save_portfolio_settings(
    settings: PortfolioSettingsRequest,
    uid: str = Depends(get_current_user_uid),
    db: Session = Depends(get_db)
):
    """Save portfolio settings"""
    try:
        # Check if settings exist
        existing_settings = db.query(PortfolioSettings).filter(
            PortfolioSettings.uid == uid
        ).first()
        
        if existing_settings:
            # Update existing
            existing_settings.title = settings.title
            existing_settings.subtitle = settings.subtitle
            existing_settings.template = settings.template
            existing_settings.custom_domain = settings.customDomain
            existing_settings.is_published = settings.isPublished
        else:
            # Create new
            new_settings = PortfolioSettings(
                uid=uid,
                title=settings.title,
                subtitle=settings.subtitle,
                template=settings.template,
                custom_domain=settings.customDomain,
                is_published=settings.isPublished
            )
            db.add(new_settings)
        
        db.commit()
        return {"message": "Settings saved successfully"}
    
    except Exception as e:
        logger.error(f"Error saving portfolio settings: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to save settings")

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
            # Create default settings if they don't exist
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

@router.get("/url")
async def get_portfolio_url(
    uid: str = Depends(get_current_user_uid),
    db: Session = Depends(get_db)
):
    """Get the portfolio URL for the current user"""
    try:
        # Get portfolio settings to check if published
        settings = db.query(PortfolioSettings).filter(
            PortfolioSettings.uid == uid
        ).first()
        
        if not settings:
            raise HTTPException(status_code=404, detail="Portfolio not found")
        
        # For now, we'll use a simple slug based on the UID
        # In the future, this could be based on user profile data
        slug = uid[:8]  # Use first 8 characters of UID as slug
        
        # Check if custom domain is configured
        if settings.custom_domain:
            url = f"https://{settings.custom_domain}"
        else:
            # Use the frontend origin from environment or default
            frontend_origin = os.getenv("FRONTEND_ORIGIN", "https://photomark.cloud").split(",")[0].strip()
            url = f"{frontend_origin}/portfolio/{slug}"
        
        return PortfolioUrlResponse(url=url, slug=slug)
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting portfolio URL: {e}")
        raise HTTPException(status_code=500, detail="Failed to get portfolio URL")

def _resolve_user_identifier(identifier: str, db: Session) -> str:
    """Resolve a user identifier (slug or UID) to a UID"""
    # If it looks like a Firebase UID (28 chars, alphanumeric), use it directly
    if len(identifier) == 28 and identifier.replace('_', '').replace('-', '').isalnum():
        return identifier
    
    # Otherwise, try to resolve as a slug by checking user profiles
    # For now, we'll need to query Firebase Auth or implement a user profiles table
    # As a temporary solution, we'll fall back to treating it as a UID
    return identifier

@router.get("/{user_identifier}/public")
async def get_public_portfolio(user_identifier: str, db: Session = Depends(get_db)):
    """Get public portfolio data for viewing (supports both UID and slug)"""
    try:
        # Resolve identifier to UID
        user_id = _resolve_user_identifier(user_identifier, db)
        
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