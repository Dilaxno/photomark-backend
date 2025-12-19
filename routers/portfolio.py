import os
import uuid
import re
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
from models.portfolio_slug import PortfolioSlug
from core.config import s3, R2_BUCKET

logger = logging.getLogger(__name__)
router = APIRouter()

def slugify(text: str) -> str:
    """Convert text to URL-friendly slug"""
    if not text:
        return 'portfolio'
    
    # Convert to lowercase and replace spaces/special chars with hyphens
    slug = re.sub(r'[^\w\s-]', '', text.lower().strip())
    slug = re.sub(r'[\s_-]+', '-', slug)
    slug = slug.strip('-')[:50]  # Limit length
    
    return slug or 'portfolio'

def generate_user_slug(display_name: Optional[str] = None, email: Optional[str] = None) -> str:
    """Generate a user-friendly slug from display name or email"""
    if display_name and display_name.strip():
        return slugify(display_name.strip())
    
    if email:
        username = email.split('@')[0]
        return slugify(username)
    
    return 'portfolio'

def get_or_create_portfolio_slug(uid: str, portfolio_title: Optional[str] = None, display_name: Optional[str] = None, email: Optional[str] = None, db: Session = None) -> str:
    """Get existing portfolio slug or create a new one using portfolio title"""
    if not db:
        raise ValueError("Database session required")
    
    # Check if user already has a slug
    existing_slug = db.query(PortfolioSlug).filter(PortfolioSlug.uid == uid).first()
    if existing_slug:
        return existing_slug.slug
    
    # Generate base slug - prioritize portfolio title
    if portfolio_title and portfolio_title.strip():
        base_slug = slugify(portfolio_title.strip())
    else:
        base_slug = generate_user_slug(display_name, email)
    
    # Ensure uniqueness by checking existing slugs
    slug = base_slug
    counter = 1
    while db.query(PortfolioSlug).filter(PortfolioSlug.slug == slug).first():
        slug = f"{base_slug}-{counter}"
        counter += 1
    
    # Create new slug record
    portfolio_slug = PortfolioSlug(slug=slug, uid=uid)
    db.add(portfolio_slug)
    db.commit()
    
    return slug

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

@router.get("/url")
async def get_portfolio_url(
    uid: str = Depends(get_current_user_uid),
    db: Session = Depends(get_db)
):
    """Get the public portfolio URL for the current user"""
    try:
        # Get portfolio settings to access the title
        settings = db.query(PortfolioSettings).filter(
            PortfolioSettings.uid == uid
        ).first()
        
        portfolio_title = settings.title if settings else None
        
        # Get or create portfolio slug using the portfolio title
        slug = get_or_create_portfolio_slug(uid, portfolio_title=portfolio_title, db=db)
        
        is_published = settings.is_published if settings else False
        
        # Generate URLs
        cloud_url = f"https://photomark.cloud/portfolio/{slug}"
        
        # Check for custom domain
        custom_domain_url = None
        if settings and settings.custom_domain:
            custom_domain_url = f"https://{settings.custom_domain}"
        
        return {
            "slug": slug,
            "cloudUrl": cloud_url,
            "customDomainUrl": custom_domain_url,
            "isPublished": is_published,
            "uid": uid
        }
    except Exception as e:
        logger.error(f"Error getting portfolio URL: {e}")
        raise HTTPException(status_code=500, detail="Failed to get portfolio URL")

@router.post("/regenerate-slug")
async def regenerate_portfolio_slug(
    uid: str = Depends(get_current_user_uid),
    db: Session = Depends(get_db)
):
    """Regenerate portfolio slug based on current portfolio title"""
    try:
        # Get current portfolio settings
        settings = db.query(PortfolioSettings).filter(
            PortfolioSettings.uid == uid
        ).first()
        
        if not settings:
            raise HTTPException(status_code=404, detail="Portfolio settings not found")
        
        # Delete existing slug
        existing_slug = db.query(PortfolioSlug).filter(PortfolioSlug.uid == uid).first()
        if existing_slug:
            db.delete(existing_slug)
            db.commit()
        
        # Generate new slug based on portfolio title
        new_slug = get_or_create_portfolio_slug(uid, portfolio_title=settings.title, db=db)
        
        # Generate new URL
        cloud_url = f"https://photomark.cloud/portfolio/{new_slug}"
        
        return {
            "slug": new_slug,
            "cloudUrl": cloud_url,
            "message": "Portfolio slug regenerated successfully"
        }
    except Exception as e:
        logger.error(f"Error regenerating portfolio slug: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to regenerate slug")

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

@router.post("/create-demo")
async def create_demo_portfolio(db: Session = Depends(get_db)):
    """Create a demo portfolio for testing"""
    try:
        demo_uid = "demo-user"
        
        # Check if demo portfolio already exists
        existing = db.query(PortfolioSettings).filter(
            PortfolioSettings.uid == demo_uid
        ).first()
        
        if existing:
            # Update to published
            existing.is_published = True
            existing.published_at = datetime.now(timezone.utc)
            db.commit()
            return {"message": "Demo portfolio already exists and is now published", "uid": demo_uid}
        
        # Create demo portfolio settings
        demo_settings = PortfolioSettings(
            uid=demo_uid,
            title="Demo Photography Portfolio",
            subtitle="Professional Photography Services",
            template="canvas",
            is_published=True,
            published_at=datetime.now(timezone.utc)
        )
        
        db.add(demo_settings)
        
        # Create some demo photos (using placeholder images)
        demo_photos = [
            {
                "id": "demo-photo-1",
                "title": "Landscape Photography",
                "url": "https://res.cloudinary.com/demo/image/upload/f_auto,q_auto:best,w_1600,dpr_2.0/sample.jpg",
                "order": 0
            },
            {
                "id": "demo-photo-2", 
                "title": "Portrait Session",
                "url": "https://res.cloudinary.com/demo/image/upload/f_auto,q_auto:best,w_1600,dpr_2.0/woman.jpg",
                "order": 1
            },
            {
                "id": "demo-photo-3",
                "title": "Wedding Photography", 
                "url": "https://res.cloudinary.com/demo/image/upload/f_auto,q_auto:best,w_1600,dpr_2.0/couple.jpg",
                "order": 2
            }
        ]
        
        for photo_data in demo_photos:
            demo_photo = PortfolioPhoto(
                id=photo_data["id"],
                uid=demo_uid,
                url=photo_data["url"],
                title=photo_data["title"],
                order=photo_data["order"],
                source="demo"
            )
            db.add(demo_photo)
        
        db.commit()
        
        return {
            "message": "Demo portfolio created successfully",
            "uid": demo_uid,
            "url": f"/portfolio/{demo_uid}"
        }
        
    except Exception as e:
        logger.error(f"Error creating demo portfolio: {e}")
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to create demo portfolio")

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
    """Get public portfolio data for viewing (supports both slug and UID)"""
    try:
        user_id = user_identifier
        
        # Try to resolve slug to UID first
        slug_record = db.query(PortfolioSlug).filter(
            PortfolioSlug.slug == user_identifier
        ).first()
        
        if slug_record:
            user_id = slug_record.uid
            logger.info(f"Resolved slug '{user_identifier}' to UID: {user_id}")
        else:
            # Treat as direct UID
            logger.info(f"Using identifier as UID: {user_identifier}")
        
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