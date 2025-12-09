"""
Website Custom Domain model for PostgreSQL
Stores custom domain configuration for user portfolio websites
"""
from sqlalchemy import Column, String, Boolean, DateTime, Index
from sqlalchemy.sql import func
from core.database import Base


class WebsiteDomain(Base):
    """
    Custom domain configuration for portfolio websites.
    Each user can have one custom domain per website.
    """
    __tablename__ = "website_domains"
    
    # Primary key
    id = Column(String(36), primary_key=True)
    
    # User UID from Firebase Auth - indexed for fast lookup
    uid = Column(String(128), nullable=False, index=True)
    
    # Website ID this domain is attached to
    website_id = Column(String(64), nullable=False, index=True)
    
    # Custom domain hostname (e.g., "portfolio.example.com")
    hostname = Column(String(255), nullable=False, unique=True, index=True)
    
    # DNS verification status
    dns_verified = Column(Boolean, nullable=False, default=False)
    
    # SSL/TLS status: 'unknown', 'pending', 'active', 'blocked'
    ssl_status = Column(String(32), nullable=False, default='unknown')
    
    # Observed CNAME target from DNS lookup
    cname_observed = Column(String(255), nullable=True)
    
    # Whether the domain is enabled for use
    enabled = Column(Boolean, nullable=False, default=False)
    
    # Last error message (if any)
    last_error = Column(String(512), nullable=True)
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    last_checked = Column(DateTime(timezone=True), nullable=True)
    
    # Composite indexes
    __table_args__ = (
        Index('ix_website_domains_hostname_verified', 'hostname', 'dns_verified'),
        Index('ix_website_domains_uid_website', 'uid', 'website_id'),
    )
    
    def to_dict(self):
        """Convert to dict for API responses"""
        return {
            "hostname": self.hostname,
            "websiteId": self.website_id,
            "dnsVerified": self.dns_verified,
            "sslStatus": self.ssl_status,
            "cnameObserved": self.cname_observed,
            "enabled": self.enabled,
            "lastError": self.last_error,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
            "lastChecked": self.last_checked.isoformat() if self.last_checked else None,
        }
