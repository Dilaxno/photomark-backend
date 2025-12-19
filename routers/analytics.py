"""
Analytics Router
Tracks and reports on photo views, client behavior, and engagement
"""
import hashlib
from datetime import datetime, timedelta, date
from typing import Optional, List

from fastapi import APIRouter, Request, Query, Body, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func, and_, or_, cast, Date

# Optional user_agents import
try:
    from user_agents import parse as parse_ua
    HAS_USER_AGENTS = True
except ImportError:
    HAS_USER_AGENTS = False
    parse_ua = None

from core.config import logger
from core.auth import get_uid_from_request
from core.database import get_db
from models.analytics import PhotoView, GalleryView, DailyAnalytics, PhotoAnalytics, DownloadEvent

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


# ============ Pydantic Models ============

class TrackPhotoView(BaseModel):
    photo_key: str
    vault_name: Optional[str] = None
    share_token: Optional[str] = None
    owner_uid: str
    view_duration_seconds: Optional[int] = None
    referrer: Optional[str] = None


class TrackGalleryView(BaseModel):
    vault_name: Optional[str] = None
    share_token: Optional[str] = None
    owner_uid: str
    page_type: Optional[str] = "vault"
    session_id: Optional[str] = None
    photos_viewed: Optional[int] = 0
    session_duration_seconds: Optional[int] = None
    referrer: Optional[str] = None


class TrackDownload(BaseModel):
    owner_uid: str
    vault_name: Optional[str] = None
    share_token: Optional[str] = None
    download_type: str  # original, lowres, single, zip
    photo_keys: Optional[List[str]] = None
    file_count: Optional[int] = 1
    total_size_bytes: Optional[int] = None
    is_paid: Optional[bool] = False
    payment_amount_cents: Optional[int] = None
    payment_id: Optional[str] = None
    referrer: Optional[str] = None


class TrackEngagement(BaseModel):
    owner_uid: str
    vault_name: Optional[str] = None
    photo_key: Optional[str] = None
    action: str  # favorite, download, share


# ============ Helper Functions ============

def _get_visitor_hash(request: Request, device_fingerprint: str = None) -> str:
    """Generate a hash to identify unique visitors without storing PII"""
    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "unknown")
    fingerprint = device_fingerprint or ""
    raw = f"{ip}:{ua}:{fingerprint}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _get_ip_hash(request: Request) -> str:
    """Generate a hash of the IP address for privacy-compliant tracking"""
    ip = request.client.host if request.client else "unknown"
    return hashlib.sha256(f"ip:{ip}".encode()).hexdigest()[:32]


def _get_device_fingerprint_hash(fingerprint_data: dict) -> str:
    """Generate a hash from device fingerprint data"""
    if not fingerprint_data:
        return ""
    
    # Create a consistent string from fingerprint data
    fingerprint_str = "|".join([
        str(fingerprint_data.get("screen", "")),
        str(fingerprint_data.get("timezone", "")),
        str(fingerprint_data.get("language", "")),
        str(fingerprint_data.get("platform", "")),
        str(fingerprint_data.get("plugins", "")),
        str(fingerprint_data.get("canvas", ""))
    ])
    
    return hashlib.sha256(fingerprint_str.encode()).hexdigest()[:32]


def _parse_user_agent(ua_string: str) -> dict:
    """Parse user agent to extract device info"""
    if not HAS_USER_AGENTS or not parse_ua:
        # Fallback: simple detection without user_agents library
        ua_lower = (ua_string or "").lower()
        if "mobile" in ua_lower or "android" in ua_lower or "iphone" in ua_lower:
            device_type = "mobile"
        elif "tablet" in ua_lower or "ipad" in ua_lower:
            device_type = "tablet"
        else:
            device_type = "desktop"
        return {
            "device_type": device_type,
            "browser": "unknown",
            "browser_version": "unknown",
            "os": "unknown",
            "os_version": "unknown"
        }
    
    try:
        ua = parse_ua(ua_string)
        device_type = "mobile" if ua.is_mobile else "tablet" if ua.is_tablet else "desktop"
        return {
            "device_type": device_type,
            "browser": ua.browser.family,
            "browser_version": ua.browser.version_string,
            "os": ua.os.family,
            "os_version": ua.os.version_string
        }
    except:
        return {
            "device_type": "unknown",
            "browser": "unknown",
            "browser_version": "unknown",
            "os": "unknown",
            "os_version": "unknown"
        }


def _get_source(referrer: Optional[str]) -> str:
    """Determine traffic source from referrer"""
    if not referrer:
        return "direct"
    referrer = referrer.lower()
    if any(s in referrer for s in ["facebook", "fb.com", "instagram", "twitter", "linkedin", "pinterest", "tiktok"]):
        return "social"
    if any(s in referrer for s in ["google", "bing", "yahoo", "duckduckgo"]):
        return "search"
    if "mail" in referrer or "outlook" in referrer or "gmail" in referrer:
        return "email"
    return "referral"


# ============ Public Tracking Endpoints ============

@router.post("/track/photo")
async def track_photo_view(
    request: Request,
    data: TrackPhotoView,
    fingerprint_data: dict = Body(default={}),
    db: Session = Depends(get_db)
):
    """Track a photo view (called from frontend)"""
    try:
        # Generate hashes for privacy-compliant tracking
        device_fingerprint = _get_device_fingerprint_hash(fingerprint_data)
        visitor_hash = _get_visitor_hash(request, device_fingerprint)
        ip_hash = _get_ip_hash(request)
        
        # Parse user agent for device info
        ua_info = _parse_user_agent(request.headers.get("user-agent", ""))
        source = _get_source(data.referrer)
        
        # Extract screen resolution from fingerprint data
        screen_resolution = fingerprint_data.get("screen", "")
        
        # Create view record
        view = PhotoView(
            owner_uid=data.owner_uid,
            photo_key=data.photo_key,
            vault_name=data.vault_name,
            share_token=data.share_token,
            visitor_hash=visitor_hash,
            ip_hash=ip_hash,
            device_fingerprint=device_fingerprint,
            device_type=ua_info["device_type"],
            browser=ua_info["browser"],
            browser_version=ua_info["browser_version"],
            os=ua_info["os"],
            os_version=ua_info["os_version"],
            screen_resolution=screen_resolution,
            view_duration_seconds=data.view_duration_seconds,
            referrer=data.referrer,
            source=source
        )
        db.add(view)
        
        # Update photo analytics summary
        photo_stats = db.query(PhotoAnalytics).filter(
            PhotoAnalytics.owner_uid == data.owner_uid,
            PhotoAnalytics.photo_key == data.photo_key
        ).first()
        
        if photo_stats:
            photo_stats.total_views += 1
            photo_stats.last_viewed_at = datetime.utcnow()
            # Check if unique viewer
            existing_view = db.query(PhotoView).filter(
                PhotoView.owner_uid == data.owner_uid,
                PhotoView.photo_key == data.photo_key,
                PhotoView.visitor_hash == visitor_hash
            ).first()
            
            if not existing_view:
                photo_stats.unique_viewers += 1
        else:
            # Create new photo analytics record
            photo_stats = PhotoAnalytics(
                owner_uid=data.owner_uid,
                photo_key=data.photo_key,
                vault_name=data.vault_name,
                total_views=1,
                unique_viewers=1,
                last_viewed_at=datetime.utcnow()
            )
            db.add(photo_stats)
        
        db.commit()
        
        return JSONResponse({
            "success": True,
            "message": "Photo view tracked successfully"
        })
        
    except Exception as e:
        logger.error(f"Error tracking photo view: {e}")
        db.rollback()
        return JSONResponse({
            "success": False,
            "error": "Failed to track photo view"
        }, status_code=500)


@router.post("/track/download")
async def track_download(
    request: Request,
    data: TrackDownload,
    fingerprint_data: dict = Body(default={}),
    db: Session = Depends(get_db)
):
    """Track a download event with enhanced analytics"""
    try:
        # Generate hashes for privacy-compliant tracking
        device_fingerprint = _get_device_fingerprint_hash(fingerprint_data)
        visitor_hash = _get_visitor_hash(request, device_fingerprint)
        ip_hash = _get_ip_hash(request)
        
        # Parse user agent for device info
        ua_info = _parse_user_agent(request.headers.get("user-agent", ""))
        source = _get_source(data.referrer)
        
        # Extract screen resolution from fingerprint data
        screen_resolution = fingerprint_data.get("screen", "")
        
        # Create download event record
        download_event = DownloadEvent(
            owner_uid=data.owner_uid,
            vault_name=data.vault_name,
            share_token=data.share_token,
            download_type=data.download_type,
            photo_keys=data.photo_keys,
            file_count=data.file_count or 1,
            total_size_bytes=data.total_size_bytes,
            visitor_hash=visitor_hash,
            ip_hash=ip_hash,
            device_fingerprint=device_fingerprint,
            device_type=ua_info["device_type"],
            browser=ua_info["browser"],
            browser_version=ua_info["browser_version"],
            os=ua_info["os"],
            os_version=ua_info["os_version"],
            screen_resolution=screen_resolution,
            is_paid=data.is_paid or False,
            payment_amount_cents=data.payment_amount_cents,
            payment_id=data.payment_id,
            referrer=data.referrer,
            source=source
        )
        db.add(download_event)
        
        # Update photo analytics for each downloaded photo
        if data.photo_keys:
            for photo_key in data.photo_keys:
                photo_stats = db.query(PhotoAnalytics).filter(
                    PhotoAnalytics.owner_uid == data.owner_uid,
                    PhotoAnalytics.photo_key == photo_key
                ).first()
                
                if photo_stats:
                    photo_stats.downloads_count += 1
                    photo_stats.last_downloaded_at = datetime.utcnow()
                else:
                    # Create new photo analytics record
                    photo_stats = PhotoAnalytics(
                        owner_uid=data.owner_uid,
                        photo_key=photo_key,
                        vault_name=data.vault_name,
                        downloads_count=1,
                        last_downloaded_at=datetime.utcnow()
                    )
                    db.add(photo_stats)
        
        # Update gallery view session if session_id provided
        session_id = request.headers.get("x-session-id")
        if session_id and data.vault_name:
            gallery_view = db.query(GalleryView).filter(
                GalleryView.session_id == session_id,
                GalleryView.vault_name == data.vault_name,
                GalleryView.owner_uid == data.owner_uid
            ).order_by(GalleryView.viewed_at.desc()).first()
            
            if gallery_view:
                gallery_view.photos_downloaded += data.file_count or 1
                gallery_view.downloaded_count += 1
        
        db.commit()
        
        return JSONResponse({
            "success": True,
            "message": "Download tracked successfully"
        })
        
    except Exception as e:
        logger.error(f"Error tracking download: {e}")
        db.rollback()
        return JSONResponse({
            "success": False,
            "error": "Failed to track download"
        }, status_code=500)


@router.post("/track/gallery")
async def track_gallery_view(
    request: Request,
    data: TrackGalleryView,
    db: Session = Depends(get_db)
):
    """Track a gallery/vault page view"""
    visitor_hash = _get_visitor_hash(request)
    ua_info = _parse_user_agent(request.headers.get("user-agent", ""))
    source = _get_source(data.referrer)
    
    view = GalleryView(
        owner_uid=data.owner_uid,
        vault_name=data.vault_name,
        share_token=data.share_token,
        page_type=data.page_type,
        visitor_hash=visitor_hash,
        ip_address=request.client.host if request.client else None,
        device_type=ua_info["device_type"],
        browser=ua_info["browser"],
        os=ua_info["os"],
        session_id=data.session_id,
        session_duration_seconds=data.session_duration_seconds,
        photos_viewed=data.photos_viewed,
        referrer=data.referrer,
        source=source
    )
    db.add(view)
    db.commit()
    return {"ok": True}


@router.post("/track/engagement")
async def track_engagement(
    request: Request,
    data: TrackEngagement,
    db: Session = Depends(get_db)
):
    """Track engagement actions (favorite, download, share)"""
    if data.photo_key:
        photo_stats = db.query(PhotoAnalytics).filter(
            PhotoAnalytics.owner_uid == data.owner_uid,
            PhotoAnalytics.photo_key == data.photo_key
        ).first()
        
        if photo_stats:
            if data.action == "favorite":
                photo_stats.favorites_count += 1
            elif data.action == "download":
                photo_stats.downloads_count += 1
            elif data.action == "share":
                photo_stats.shares_count += 1
            db.commit()
    
    return {"ok": True}


# ============ Owner Dashboard Endpoints ============

@router.get("/dashboard")
async def get_analytics_dashboard(
    request: Request,
    days: int = Query(30, ge=1, le=90),
    vault_name: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Get analytics dashboard for owner"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    # Base filters
    gallery_filter = [GalleryView.owner_uid == uid, GalleryView.viewed_at >= cutoff]
    photo_filter = [PhotoView.owner_uid == uid, PhotoView.viewed_at >= cutoff]
    
    if vault_name:
        gallery_filter.append(GalleryView.vault_name == vault_name)
        photo_filter.append(PhotoView.vault_name == vault_name)
    
    # Total views
    total_gallery_views = db.query(func.count(GalleryView.id)).filter(*gallery_filter).scalar() or 0
    total_photo_views = db.query(func.count(PhotoView.id)).filter(*photo_filter).scalar() or 0
    
    # Unique visitors
    unique_visitors = db.query(func.count(func.distinct(GalleryView.visitor_hash))).filter(*gallery_filter).scalar() or 0
    
    # Views by day
    views_by_day = db.query(
        cast(GalleryView.viewed_at, Date).label('date'),
        func.count(GalleryView.id).label('views'),
        func.count(func.distinct(GalleryView.visitor_hash)).label('unique')
    ).filter(*gallery_filter).group_by(
        cast(GalleryView.viewed_at, Date)
    ).order_by(cast(GalleryView.viewed_at, Date)).all()
    
    # Device breakdown
    device_stats = db.query(
        GalleryView.device_type,
        func.count(GalleryView.id)
    ).filter(*gallery_filter).group_by(GalleryView.device_type).all()
    device_breakdown = {d[0] or "unknown": d[1] for d in device_stats}
    
    # Source breakdown
    source_stats = db.query(
        GalleryView.source,
        func.count(GalleryView.id)
    ).filter(*gallery_filter).group_by(GalleryView.source).all()
    source_breakdown = {s[0] or "direct": s[1] for s in source_stats}
    
    # Top photos
    top_photos = db.query(PhotoAnalytics).filter(
        PhotoAnalytics.owner_uid == uid,
        PhotoAnalytics.vault_name == vault_name if vault_name else True
    ).order_by(PhotoAnalytics.total_views.desc()).limit(10).all()
    
    # Recent activity
    recent_views = db.query(GalleryView).filter(*gallery_filter).order_by(
        GalleryView.viewed_at.desc()
    ).limit(20).all()
    
    return {
        "summary": {
            "total_gallery_views": total_gallery_views,
            "total_photo_views": total_photo_views,
            "unique_visitors": unique_visitors,
            "period_days": days
        },
        "views_by_day": [
            {"date": str(v.date), "views": v.views, "unique": v.unique}
            for v in views_by_day
        ],
        "device_breakdown": device_breakdown,
        "source_breakdown": source_breakdown,
        "top_photos": [p.to_dict() for p in top_photos],
        "recent_activity": [
            {
                "vault_name": v.vault_name,
                "page_type": v.page_type,
                "device": v.device_type,
                "source": v.source,
                "viewed_at": v.viewed_at.isoformat() if v.viewed_at else None
            }
            for v in recent_views
        ]
    }


@router.get("/vault/{vault_name}")
async def get_vault_analytics(
    request: Request,
    vault_name: str,
    days: int = Query(30, ge=1, le=90),
    db: Session = Depends(get_db)
):
    """Get detailed analytics for a specific vault"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    # Gallery views for this vault
    gallery_filter = [
        GalleryView.owner_uid == uid,
        GalleryView.vault_name == vault_name,
        GalleryView.viewed_at >= cutoff
    ]
    
    total_views = db.query(func.count(GalleryView.id)).filter(*gallery_filter).scalar() or 0
    unique_visitors = db.query(func.count(func.distinct(GalleryView.visitor_hash))).filter(*gallery_filter).scalar() or 0
    
    # Photo views in this vault
    photo_views = db.query(func.count(PhotoView.id)).filter(
        PhotoView.owner_uid == uid,
        PhotoView.vault_name == vault_name,
        PhotoView.viewed_at >= cutoff
    ).scalar() or 0
    
    # Avg session duration
    avg_duration = db.query(func.avg(GalleryView.session_duration_seconds)).filter(
        *gallery_filter,
        GalleryView.session_duration_seconds != None
    ).scalar() or 0
    
    # Avg photos viewed per session
    avg_photos = db.query(func.avg(GalleryView.photos_viewed)).filter(
        *gallery_filter,
        GalleryView.photos_viewed != None
    ).scalar() or 0
    
    # Views by day
    views_by_day = db.query(
        cast(GalleryView.viewed_at, Date).label('date'),
        func.count(GalleryView.id).label('views')
    ).filter(*gallery_filter).group_by(
        cast(GalleryView.viewed_at, Date)
    ).order_by(cast(GalleryView.viewed_at, Date)).all()
    
    # Top photos in vault
    top_photos = db.query(PhotoAnalytics).filter(
        PhotoAnalytics.owner_uid == uid,
        PhotoAnalytics.vault_name == vault_name
    ).order_by(PhotoAnalytics.total_views.desc()).limit(20).all()
    
    # Device breakdown
    device_stats = db.query(
        GalleryView.device_type,
        func.count(GalleryView.id)
    ).filter(*gallery_filter).group_by(GalleryView.device_type).all()
    
    # Source breakdown
    source_stats = db.query(
        GalleryView.source,
        func.count(GalleryView.id)
    ).filter(*gallery_filter).group_by(GalleryView.source).all()
    
    # Engagement totals
    total_favorites = db.query(func.sum(PhotoAnalytics.favorites_count)).filter(
        PhotoAnalytics.owner_uid == uid,
        PhotoAnalytics.vault_name == vault_name
    ).scalar() or 0
    
    total_downloads = db.query(func.sum(PhotoAnalytics.downloads_count)).filter(
        PhotoAnalytics.owner_uid == uid,
        PhotoAnalytics.vault_name == vault_name
    ).scalar() or 0
    
    return {
        "vault_name": vault_name,
        "period_days": days,
        "summary": {
            "total_views": total_views,
            "unique_visitors": unique_visitors,
            "photo_views": photo_views,
            "avg_session_duration": round(avg_duration, 1),
            "avg_photos_viewed": round(avg_photos, 1),
            "total_favorites": total_favorites,
            "total_downloads": total_downloads
        },
        "views_by_day": [
            {"date": str(v.date), "views": v.views}
            for v in views_by_day
        ],
        "device_breakdown": {d[0] or "unknown": d[1] for d in device_stats},
        "source_breakdown": {s[0] or "direct": s[1] for s in source_stats},
        "top_photos": [p.to_dict() for p in top_photos]
    }


@router.get("/photo/{photo_key:path}")
async def get_photo_analytics(
    request: Request,
    photo_key: str,
    days: int = Query(30, ge=1, le=90),
    db: Session = Depends(get_db)
):
    """Get analytics for a specific photo"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    # Get photo stats
    photo_stats = db.query(PhotoAnalytics).filter(
        PhotoAnalytics.owner_uid == uid,
        PhotoAnalytics.photo_key == photo_key
    ).first()
    
    if not photo_stats:
        return {
            "photo_key": photo_key,
            "total_views": 0,
            "unique_viewers": 0,
            "views_by_day": [],
            "device_breakdown": {},
            "source_breakdown": {}
        }
    
    # Views by day
    views_by_day = db.query(
        cast(PhotoView.viewed_at, Date).label('date'),
        func.count(PhotoView.id).label('views')
    ).filter(
        PhotoView.owner_uid == uid,
        PhotoView.photo_key == photo_key,
        PhotoView.viewed_at >= cutoff
    ).group_by(cast(PhotoView.viewed_at, Date)).order_by(cast(PhotoView.viewed_at, Date)).all()
    
    # Device breakdown
    device_stats = db.query(
        PhotoView.device_type,
        func.count(PhotoView.id)
    ).filter(
        PhotoView.owner_uid == uid,
        PhotoView.photo_key == photo_key,
        PhotoView.viewed_at >= cutoff
    ).group_by(PhotoView.device_type).all()
    
    # Source breakdown
    source_stats = db.query(
        PhotoView.source,
        func.count(PhotoView.id)
    ).filter(
        PhotoView.owner_uid == uid,
        PhotoView.photo_key == photo_key,
        PhotoView.viewed_at >= cutoff
    ).group_by(PhotoView.source).all()
    
    return {
        **photo_stats.to_dict(),
        "period_days": days,
        "views_by_day": [
            {"date": str(v.date), "views": v.views}
            for v in views_by_day
        ],
        "device_breakdown": {d[0] or "unknown": d[1] for d in device_stats},
        "source_breakdown": {s[0] or "direct": s[1] for s in source_stats}
    }


@router.get("/vaults")
async def list_vault_analytics(
    request: Request,
    days: int = Query(30, ge=1, le=90),
    db: Session = Depends(get_db)
):
    """Get analytics summary for all vaults"""
    uid = get_uid_from_request(request)
    if not uid:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    
    cutoff = datetime.utcnow() - timedelta(days=days)
    
    # Get views per vault
    vault_stats = db.query(
        GalleryView.vault_name,
        func.count(GalleryView.id).label('views'),
        func.count(func.distinct(GalleryView.visitor_hash)).label('unique_visitors')
    ).filter(
        GalleryView.owner_uid == uid,
        GalleryView.vault_name != None,
        GalleryView.viewed_at >= cutoff
    ).group_by(GalleryView.vault_name).order_by(func.count(GalleryView.id).desc()).all()
    
    return {
        "vaults": [
            {
                "vault_name": v.vault_name,
                "views": v.views,
                "unique_visitors": v.unique_visitors
            }
            for v in vault_stats
        ],
        "period_days": days
    }
