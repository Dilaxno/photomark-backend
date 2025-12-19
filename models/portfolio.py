"""
Portfolio models for PostgreSQL
"""
from sqlalchemy import Column, String, Text, DateTime, Integer, Boolean
from sqlalchemy.sql import func
from core.database import Base

class PortfolioPhoto(Base):
    __tablename__ = "portfolio_photos"
    
    id = Column(String(128), primary_key=True, index=True)
    uid = Column(String(128), nullable=False, index=True)
    url = Column(Text, nullable=False)
    title = Column(String(255), nullable=True)
    order = Column(Integer, default=0, nullable=False)
    source = Column(String(50), default="upload", nullable=False)
    uploaded_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    def to_dict(self, include_thumb_url=False):
        result = {
            "id": self.id,
            "url": self.url,
            "title": self.title,
            "order": self.order,
            "source": self.source,
            "uploaded_at": self.uploaded_at.isoformat() if self.uploaded_at else None
        }
        
        if include_thumb_url:
            # This will be populated by the router
            result["thumb_url"] = None
            
        return result

class PortfolioSettings(Base):
    __tablename__ = "portfolio_settings"
    
    uid = Column(String(128), primary_key=True, index=True)
    title = Column(String(255), default="My Portfolio", nullable=False)
    subtitle = Column(String(500), nullable=True)
    template = Column(String(50), default="canvas", nullable=False)
    custom_domain = Column(String(255), nullable=True)
    is_published = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    published_at = Column(DateTime(timezone=True), nullable=True)
    
    def to_dict(self):
        return {
            "title": self.title,
            "subtitle": self.subtitle,
            "template": self.template,
            "customDomain": self.custom_domain,
            "isPublished": self.is_published,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
            "publishedAt": self.published_at.isoformat() if self.published_at else None
        }