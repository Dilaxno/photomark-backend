"""
Shop models for PostgreSQL
Replaces Firestore 'shops' collection
"""
from sqlalchemy import Column, String, Text, JSON, DateTime, Integer, Boolean
from sqlalchemy.sql import func
from core.database import Base

class Shop(Base):
    __tablename__ = "shops"
    
    # Primary key - user UID from Firebase Auth
    uid = Column(String(128), primary_key=True, index=True)
    
    # Shop settings
    name = Column(String(255), nullable=False)
    slug = Column(String(255), unique=True, index=True, nullable=False)
    description = Column(Text, nullable=True)
    owner_uid = Column(String(128), nullable=False, index=True)
    owner_name = Column(String(255), nullable=True)
    
    # Theme stored as JSON
    theme = Column(JSON, nullable=False, default={
        "primaryColor": "#3b82f6",
        "secondaryColor": "#8b5cf6",
        "accentColor": "#f59e0b",
        "backgroundColor": "#ffffff",
        "textColor": "#1f2937",
        "fontFamily": "Inter",
        "logoUrl": None,
        "bannerUrl": None
    })
    
    # Products stored as JSON array
    products = Column(JSON, nullable=False, default=[])

    # Custom domain configuration and status
    domain = Column(JSON, nullable=False, default={})
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    def to_dict(self):
        """Convert to dict for API responses"""
        return {
            "settings": {
                "name": self.name,
                "slug": self.slug,
                "description": self.description,
                "ownerUid": self.owner_uid,
                "ownerName": self.owner_name,
                "theme": self.theme,
                "domain": self.domain or {},
                "createdAt": self.created_at.isoformat() if self.created_at else None,
                "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
            },
            "products": self.products or []
        }


class ShopSlug(Base):
    """
    Slug to UID mapping for O(1) public shop lookup
    Replaces Firestore 'shop_slugs' collection
    """
    __tablename__ = "shop_slugs"
    
    slug = Column(String(255), primary_key=True, index=True)
    uid = Column(String(128), nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
