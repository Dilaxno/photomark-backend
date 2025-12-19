from sqlalchemy import Column, String, Boolean, DateTime, Text, Integer
from sqlalchemy.sql import func
from core.database import Base

class WebsiteDomain(Base):
    __tablename__ = "website_domains"
    
    id = Column(Integer, primary_key=True, index=True)
    user_uid = Column(String(128), nullable=False, index=True)
    hostname = Column(String(255), nullable=False, unique=True, index=True)
    enabled = Column(Boolean, default=True, nullable=False)
    domain_type = Column(String(50), default="portfolio", nullable=False)  # portfolio, shop, etc.
    
    # SSL/TLS configuration
    ssl_enabled = Column(Boolean, default=True, nullable=False)
    ssl_verified = Column(Boolean, default=False, nullable=False)
    ssl_verified_at = Column(DateTime(timezone=True), nullable=True)
    
    # DNS verification
    dns_verified = Column(Boolean, default=False, nullable=False)
    dns_verified_at = Column(DateTime(timezone=True), nullable=True)
    dns_challenge_token = Column(String(255), nullable=True)
    
    # Metadata
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    def __repr__(self):
        return f"<WebsiteDomain(hostname='{self.hostname}', user_uid='{self.user_uid}', enabled={self.enabled})>"