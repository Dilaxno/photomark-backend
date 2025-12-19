"""
Portfolio Domain model for PostgreSQL
Custom domain management for portfolio pages
"""
from sqlalchemy import Column, String, Boolean, DateTime, Text, Integer
from sqlalchemy.sql import func
from core.database import Base

class PortfolioDomain(Base):
    __tablename__ = "portfolio_domains"
    
    id = Column(Integer, primary_key=True, index=True)
    uid = Column(String(128), nullable=False, index=True)
    hostname = Column(String(255), nullable=False, unique=True, index=True)
    enabled = Column(Boolean, default=True, nullable=False)
    
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
    last_checked = Column(DateTime(timezone=True), nullable=True)
    
    def to_dict(self):
        """Convert to dict for API responses"""
        return {
            "id": self.id,
            "uid": self.uid,
            "hostname": self.hostname,
            "enabled": self.enabled,
            "sslEnabled": self.ssl_enabled,
            "sslVerified": self.ssl_verified,
            "sslVerifiedAt": self.ssl_verified_at.isoformat() if self.ssl_verified_at else None,
            "dnsVerified": self.dns_verified,
            "dnsVerifiedAt": self.dns_verified_at.isoformat() if self.dns_verified_at else None,
            "dnsToken": self.dns_challenge_token,
            "notes": self.notes,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
            "lastChecked": self.last_checked.isoformat() if self.last_checked else None
        }
    
    def __repr__(self):
        return f"<PortfolioDomain(hostname='{self.hostname}', uid='{self.uid}', enabled={self.enabled})>"