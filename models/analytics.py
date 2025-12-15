"""
Analytics Models
Tracks photo views, client behavior, and engagement across galleries/vaults
"""
from datetime import datetime
from sqlalchemy import Column, String, Text, DateTime, Integer, Float, Boolean, JSON, Date
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func
import uuid

from core.database import Base


class PhotoView(Base):
    """Track individual photo views"""
    __tablename__ = "photo_views"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Owner info
    owner_uid = Column(String(128), nullable=False, index=True)
    
    # Photo/Content info
    photo_key = Column(String(512), nullable=False, index=True)
    vault_name = Column(String(255), nullable=True, index=True)
    share_token = Column(String(255), nullable=True, index=True)
    
    # Viewer info
    visitor_hash = Column(String(64), nullable=False, index=True)  # Hash of IP + User-Agent
    ip_address = Column(String(45), nullable=True)
    country = Column(String(2), nullable=True)  # ISO country code
    city = Column(String(100), nullable=True)
    
    # Device info
    device_type = Column(String(20), nullable=True)  # mobile, tablet, desktop
    browser = Column(String(50), nullable=True)
    os = Column(String(50), nullable=True)
    
    # Engagement
    view_duration_seconds = Column(Integer, nullable=True)
    
    # Context
    referrer = Column(Text, nullable=True)
    source = Column(String(50), nullable=True)  # direct, social, email, etc.
    
    # Timestamps
    viewed_at = Column(DateTime, default=datetime.utcnow, index=True)


class GalleryView(Base):
    """Track gallery/vault page views"""
    __tablename__ = "gallery_views"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Owner info
    owner_uid = Column(String(128), nullable=False, index=True)
    
    # Gallery info
    vault_name = Column(String(255), nullable=True, index=True)
    share_token = Column(String(255), nullable=True, index=True)
    page_type = Column(String(50), nullable=True)  # vault, gallery, portfolio, shop
    
    # Viewer info
    visitor_hash = Column(String(64), nullable=False, index=True)
    ip_address = Column(String(45), nullable=True)
    country = Column(String(2), nullable=True)
    city = Column(String(100), nullable=True)
    
    # Device info
    device_type = Column(String(20), nullable=True)
    browser = Column(String(50), nullable=True)
    os = Column(String(50), nullable=True)
    
    # Session info
    session_id = Column(String(128), nullable=True, index=True)
    session_duration_seconds = Column(Integer, nullable=True)
    photos_viewed = Column(Integer, default=0)
    
    # Engagement
    favorited_count = Column(Integer, default=0)
    downloaded_count = Column(Integer, default=0)
    
    # Context
    referrer = Column(Text, nullable=True)
    source = Column(String(50), nullable=True)
    
    # Timestamps
    viewed_at = Column(DateTime, default=datetime.utcnow, index=True)


class DailyAnalytics(Base):
    """Aggregated daily analytics per vault/gallery"""
    __tablename__ = "daily_analytics"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Owner info
    owner_uid = Column(String(128), nullable=False, index=True)
    
    # Scope
    vault_name = Column(String(255), nullable=True, index=True)
    page_type = Column(String(50), nullable=True)  # vault, gallery, portfolio, shop, all
    
    # Date
    date = Column(Date, nullable=False, index=True)
    
    # View metrics
    total_views = Column(Integer, default=0)
    unique_visitors = Column(Integer, default=0)
    photo_views = Column(Integer, default=0)
    
    # Engagement metrics
    avg_session_duration = Column(Float, default=0)
    avg_photos_viewed = Column(Float, default=0)
    bounce_rate = Column(Float, default=0)  # % who left without viewing photos
    
    # Action metrics
    favorites_count = Column(Integer, default=0)
    downloads_count = Column(Integer, default=0)
    shares_count = Column(Integer, default=0)
    
    # Device breakdown (JSON)
    device_breakdown = Column(JSON, default={})  # {mobile: 40, desktop: 55, tablet: 5}
    
    # Geographic breakdown (JSON)
    geo_breakdown = Column(JSON, default={})  # {US: 60, UK: 20, ...}
    
    # Source breakdown (JSON)
    source_breakdown = Column(JSON, default={})  # {direct: 50, social: 30, email: 20}
    
    # Top photos (JSON)
    top_photos = Column(JSON, default=[])  # [{key, views, favorites}]
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "date": self.date.isoformat() if self.date else None,
            "vault_name": self.vault_name,
            "page_type": self.page_type,
            "total_views": self.total_views,
            "unique_visitors": self.unique_visitors,
            "photo_views": self.photo_views,
            "avg_session_duration": self.avg_session_duration,
            "avg_photos_viewed": self.avg_photos_viewed,
            "bounce_rate": self.bounce_rate,
            "favorites_count": self.favorites_count,
            "downloads_count": self.downloads_count,
            "shares_count": self.shares_count,
            "device_breakdown": self.device_breakdown or {},
            "geo_breakdown": self.geo_breakdown or {},
            "source_breakdown": self.source_breakdown or {},
            "top_photos": self.top_photos or [],
        }


class PhotoAnalytics(Base):
    """Per-photo analytics summary"""
    __tablename__ = "photo_analytics"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    
    # Owner info
    owner_uid = Column(String(128), nullable=False, index=True)
    
    # Photo info
    photo_key = Column(String(512), nullable=False, index=True)
    vault_name = Column(String(255), nullable=True, index=True)
    
    # Lifetime metrics
    total_views = Column(Integer, default=0)
    unique_viewers = Column(Integer, default=0)
    favorites_count = Column(Integer, default=0)
    downloads_count = Column(Integer, default=0)
    shares_count = Column(Integer, default=0)
    
    # Engagement
    avg_view_duration = Column(Float, default=0)
    
    # Time tracking
    first_viewed_at = Column(DateTime, nullable=True)
    last_viewed_at = Column(DateTime, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            "photo_key": self.photo_key,
            "vault_name": self.vault_name,
            "total_views": self.total_views,
            "unique_viewers": self.unique_viewers,
            "favorites_count": self.favorites_count,
            "downloads_count": self.downloads_count,
            "shares_count": self.shares_count,
            "avg_view_duration": self.avg_view_duration,
            "first_viewed_at": self.first_viewed_at.isoformat() if self.first_viewed_at else None,
            "last_viewed_at": self.last_viewed_at.isoformat() if self.last_viewed_at else None,
        }
