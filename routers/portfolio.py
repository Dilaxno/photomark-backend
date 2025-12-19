import os
import uuid
import json
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse, HTMLResponse
from pydantic import BaseModel
import logging

from core.auth import get_current_user_uid
from core.storage import upload_file_to_gcs
from core.firestore import get_fs_client

logger = logging.getLogger(__name__)
router = APIRouter()

class PortfolioSettings(BaseModel):
    title: str
    subtitle: Optional[str] = None
    template: str = "canvas"
    customDomain: Optional[str] = None
    isPublished: bool = False

class PublishRequest(BaseModel):
    isPublished: bool

@router.get("/photos")
async def get_portfolio_photos(uid: str = Depends(get_current_user_uid)):
    """Get all portfolio photos for the user"""
    try:
        db = get_fs_client()
        if not db:
            raise HTTPException(status_code=500, detail="Database not available")
        
        doc_ref = db.collection('portfolio_photos').document(uid)
        doc = doc_ref.get()
        
        if doc.exists:
            data = doc.to_dict()
            photos = data.get('photos', [])
        else:
            photos = []
        
        return {"photos": photos}
    
    except Exception as e:
        logger.error(f"Error getting portfolio photos: {e}")
        raise HTTPException(status_code=500, detail="Failed to get portfolio photos")

@router.post("/upload")
async def upload_portfolio_photos(
    photos: List[UploadFile] = File(...),
    uid: str = Depends(get_current_user_uid)
):
    """Upload photos to portfolio"""
    try:
        if not photos:
            raise HTTPException(status_code=400, detail="No photos provided")
        
        db = get_fs_client()
        if not db:
            raise HTTPException(status_code=500, detail="Database not available")
        
        uploaded_photos = []
        
        for photo in photos:
            # Validate file type
            if not photo.content_type or not photo.content_type.startswith('image/'):
                continue
            
            # Generate unique filename
            file_ext = os.path.splitext(photo.filename or '')[1] or '.jpg'
            filename = f"portfolio/{uid}/{uuid.uuid4()}{file_ext}"
            
            # Upload to GCS
            file_content = await photo.read()
            file_url = upload_file_to_gcs(file_content, filename, photo.content_type)
            
            if file_url:
                photo_data = {
                    "id": str(uuid.uuid4()),
                    "url": file_url,
                    "title": photo.filename,
                    "order": len(uploaded_photos),
                    "uploaded_at": datetime.now(timezone.utc).isoformat()
                }
                uploaded_photos.append(photo_data)
        
        if uploaded_photos:
            # Get existing photos
            doc_ref = db.collection('portfolio_photos').document(uid)
            doc = doc_ref.get()
            
            if doc.exists:
                existing_data = doc.to_dict()
                existing_photos = existing_data.get('photos', [])
            else:
                existing_photos = []
            
            # Add new photos
            all_photos = existing_photos + uploaded_photos
            
            # Update Firestore
            doc_ref.set({
                'photos': all_photos,
                'updated_at': datetime.now(timezone.utc).isoformat()
            })
        
        return {"photos": uploaded_photos, "message": f"Uploaded {len(uploaded_photos)} photos"}
    
    except Exception as e:
        logger.error(f"Error uploading portfolio photos: {e}")
        raise HTTPException(status_code=500, detail="Failed to upload photos")

@router.delete("/photos/{photo_id}")
async def delete_portfolio_photo(
    photo_id: str,
    uid: str = Depends(get_current_user_uid)
):
    """Delete a portfolio photo"""
    try:
        db = get_fs_client()
        if not db:
            raise HTTPException(status_code=500, detail="Database not available")
        
        doc_ref = db.collection('portfolio_photos').document(uid)
        doc = doc_ref.get()
        
        if not doc.exists:
            raise HTTPException(status_code=404, detail="Portfolio not found")
        
        data = doc.to_dict()
        photos = data.get('photos', [])
        
        # Remove photo with matching ID
        updated_photos = [p for p in photos if p.get('id') != photo_id]
        
        if len(updated_photos) == len(photos):
            raise HTTPException(status_code=404, detail="Photo not found")
        
        # Update Firestore
        doc_ref.set({
            'photos': updated_photos,
            'updated_at': datetime.now(timezone.utc).isoformat()
        })
        
        return {"message": "Photo deleted successfully"}
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting portfolio photo: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete photo")

@router.get("/settings")
async def get_portfolio_settings(uid: str = Depends(get_current_user_uid)):
    """Get portfolio settings"""
    try:
        db = get_fs_client()
        if not db:
            raise HTTPException(status_code=500, detail="Database not available")
        
        doc_ref = db.collection('portfolio_settings').document(uid)
        doc = doc_ref.get()
        
        if doc.exists:
            settings = doc.to_dict()
        else:
            settings = {
                "title": "My Portfolio",
                "subtitle": "",
                "template": "canvas",
                "customDomain": "",
                "isPublished": False
            }
        
        return {"settings": settings}
    
    except Exception as e:
        logger.error(f"Error getting portfolio settings: {e}")
        raise HTTPException(status_code=500, detail="Failed to get portfolio settings")

@router.post("/settings")
async def save_portfolio_settings(
    settings: PortfolioSettings,
    uid: str = Depends(get_current_user_uid)
):
    """Save portfolio settings"""
    try:
        db = get_fs_client()
        if not db:
            raise HTTPException(status_code=500, detail="Database not available")
        
        settings_data = settings.dict()
        settings_data['updated_at'] = datetime.now(timezone.utc).isoformat()
        
        doc_ref = db.collection('portfolio_settings').document(uid)
        doc_ref.set(settings_data)
        
        return {"message": "Settings saved successfully"}
    
    except Exception as e:
        logger.error(f"Error saving portfolio settings: {e}")
        raise HTTPException(status_code=500, detail="Failed to save settings")

@router.post("/publish")
async def publish_portfolio(
    request: PublishRequest,
    uid: str = Depends(get_current_user_uid)
):
    """Publish or unpublish portfolio"""
    try:
        db = get_fs_client()
        if not db:
            raise HTTPException(status_code=500, detail="Database not available")
        
        doc_ref = db.collection('portfolio_settings').document(uid)
        doc = doc_ref.get()
        
        if doc.exists:
            settings = doc.to_dict()
        else:
            settings = {
                "title": "My Portfolio",
                "template": "canvas",
                "isPublished": False
            }
        
        settings['isPublished'] = request.isPublished
        settings['updated_at'] = datetime.now(timezone.utc).isoformat()
        
        if request.isPublished:
            settings['published_at'] = datetime.now(timezone.utc).isoformat()
        
        doc_ref.set(settings)
        
        return {"message": "Portfolio published successfully" if request.isPublished else "Portfolio unpublished"}
    
    except Exception as e:
        logger.error(f"Error publishing portfolio: {e}")
        raise HTTPException(status_code=500, detail="Failed to update portfolio status")

@router.get("/{user_id}/public")
async def get_public_portfolio(user_id: str):
    """Get public portfolio data for viewing"""
    try:
        db = get_fs_client()
        if not db:
            raise HTTPException(status_code=500, detail="Database not available")
        
        # Get settings
        settings_ref = db.collection('portfolio_settings').document(user_id)
        settings_doc = settings_ref.get()
        
        if not settings_doc.exists:
            raise HTTPException(status_code=404, detail="Portfolio not found")
        
        settings = settings_doc.to_dict()
        
        if not settings.get('isPublished', False):
            raise HTTPException(status_code=404, detail="Portfolio not published")
        
        # Get photos
        photos_ref = db.collection('portfolio_photos').document(user_id)
        photos_doc = photos_ref.get()
        
        photos = []
        if photos_doc.exists:
            photos_data = photos_doc.to_dict()
            photos = photos_data.get('photos', [])
        
        return {
            "settings": settings,
            "photos": photos
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting public portfolio: {e}")
        raise HTTPException(status_code=500, detail="Failed to get portfolio")

# Template rendering endpoints
CANVAS_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #fff; }}
        .header {{ padding: 2rem; text-align: center; border-bottom: 1px solid #eee; }}
        .title {{ font-size: 2rem; font-weight: 300; margin-bottom: 0.5rem; }}
        .subtitle {{ color: #666; font-size: 1rem; }}
        .gallery {{ padding: 2rem; max-width: 1200px; margin: 0 auto; }}
        .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 2rem; }}
        .photo {{ aspect-ratio: 1; overflow: hidden; border-radius: 8px; }}
        .photo img {{ width: 100%; height: 100%; object-fit: cover; transition: transform 0.3s; }}
        .photo:hover img {{ transform: scale(1.05); }}
        .footer {{ text-align: center; padding: 2rem; color: #666; font-size: 0.9rem; }}
    </style>
</head>
<body>
    <header class="header">
        <h1 class="title">{title}</h1>
        {subtitle_html}
    </header>
    <main class="gallery">
        <div class="grid">
            {photos_html}
        </div>
    </main>
    <footer class="footer">
        <p>© {year} {title}</p>
    </footer>
</body>
</html>
"""

EDITORIAL_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: 'Georgia', serif; background: #f8f6f3; color: #333; }}
        .header {{ background: #fff; padding: 3rem 2rem; text-align: center; }}
        .title {{ font-size: 3rem; font-weight: normal; margin-bottom: 1rem; letter-spacing: -1px; }}
        .subtitle {{ color: #666; font-size: 1.2rem; font-style: italic; }}
        .gallery {{ padding: 3rem 2rem; max-width: 1000px; margin: 0 auto; }}
        .photo {{ margin-bottom: 3rem; text-align: center; }}
        .photo img {{ max-width: 100%; height: auto; box-shadow: 0 10px 30px rgba(0,0,0,0.1); }}
        .photo-title {{ margin-top: 1rem; font-size: 0.9rem; color: #666; font-style: italic; }}
        .footer {{ background: #fff; text-align: center; padding: 2rem; color: #666; }}
    </style>
</head>
<body>
    <header class="header">
        <h1 class="title">{title}</h1>
        {subtitle_html}
    </header>
    <main class="gallery">
        {photos_html}
    </main>
    <footer class="footer">
        <p>© {year} {title}</p>
    </footer>
</body>
</html>
"""

NOIR_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0a; color: #fff; }}
        .header {{ padding: 4rem 2rem; text-align: center; }}
        .title {{ font-size: 4rem; font-weight: 100; margin-bottom: 1rem; letter-spacing: 2px; }}
        .subtitle {{ color: #999; font-size: 1rem; text-transform: uppercase; letter-spacing: 1px; }}
        .gallery {{ padding: 2rem; max-width: 1400px; margin: 0 auto; }}
        .masonry {{ columns: 3; column-gap: 2rem; }}
        .photo {{ break-inside: avoid; margin-bottom: 2rem; }}
        .photo img {{ width: 100%; height: auto; filter: contrast(1.1) brightness(0.9); }}
        .footer {{ text-align: center; padding: 3rem; color: #666; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 1px; }}
        @media (max-width: 768px) {{ .masonry {{ columns: 2; }} .title {{ font-size: 2.5rem; }} }}
    </style>
</head>
<body>
    <header class="header">
        <h1 class="title">{title}</h1>
        {subtitle_html}
    </header>
    <main class="gallery">
        <div class="masonry">
            {photos_html}
        </div>
    </main>
    <footer class="footer">
        <p>© {year} {title}</p>
    </footer>
</body>
</html>
"""

@router.get("/{user_id}")
async def render_portfolio(user_id: str):
    """Render the public portfolio page"""
    try:
        db = get_fs_client()
        if not db:
            raise HTTPException(status_code=500, detail="Database not available")
        
        # Get portfolio data
        settings_ref = db.collection('portfolio_settings').document(user_id)
        settings_doc = settings_ref.get()
        
        if not settings_doc.exists:
            raise HTTPException(status_code=404, detail="Portfolio not found")
        
        settings = settings_doc.to_dict()
        
        if not settings.get('isPublished', False):
            raise HTTPException(status_code=404, detail="Portfolio not published")
        
        # Get photos
        photos_ref = db.collection('portfolio_photos').document(user_id)
        photos_doc = photos_ref.get()
        
        photos = []
        if photos_doc.exists:
            photos_data = photos_doc.to_dict()
            photos = sorted(photos_data.get('photos', []), key=lambda x: x.get('order', 0))
        
        # Prepare template variables
        title = settings.get('title', 'Portfolio')
        subtitle = settings.get('subtitle', '')
        template = settings.get('template', 'canvas')
        year = datetime.now().year
        
        subtitle_html = f'<p class="subtitle">{subtitle}</p>' if subtitle else ''
        
        # Generate photos HTML based on template
        if template == 'canvas':
            photos_html = ''.join([
                f'<div class="photo"><img src="{photo["url"]}" alt="{photo.get("title", "")}" loading="lazy"></div>'
                for photo in photos
            ])
            html_template = CANVAS_TEMPLATE
        elif template == 'editorial':
            photos_html = ''.join([
                f'<div class="photo"><img src="{photo["url"]}" alt="{photo.get("title", "")}" loading="lazy">'
                f'<div class="photo-title">{photo.get("title", "")}</div></div>'
                for photo in photos
            ])
            html_template = EDITORIAL_TEMPLATE
        else:  # noir
            photos_html = ''.join([
                f'<div class="photo"><img src="{photo["url"]}" alt="{photo.get("title", "")}" loading="lazy"></div>'
                for photo in photos
            ])
            html_template = NOIR_TEMPLATE
        
        # Render template
        html_content = html_template.format(
            title=title,
            subtitle_html=subtitle_html,
            photos_html=photos_html,
            year=year
        )
        
        return HTMLResponse(content=html_content)
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error rendering portfolio: {e}")
        raise HTTPException(status_code=500, detail="Failed to render portfolio")