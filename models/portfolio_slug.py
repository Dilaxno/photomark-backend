"""
Portfolio Slug model for user-friendly portfolio URLs
Similar to shop_slugs but for portfolios
"""
from sqlalchemy import Column, String, DateTime, Index
from sqlalchemy.sql import func
from core.database import Base

class PortfolioSlug(Base):
    __tablename__ = "portfolio_slugs"
    
    slug = Column(String(100), primary_key=True, index=True)
    uid = Column(String(128), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # Ensure one slug per user
    __table_args__ = (
        Index('idx_portfolio_slug_uid', 'uid', unique=True),
    )
    
    def to_dict(self):
        return {
            "slug": self.slug,
            "uid": self.uid,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None
        }