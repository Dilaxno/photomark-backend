"""
Vault Custom Domain model for PostgreSQL
Stores custom domain configuration for vault share pages
"""
from sqlalchemy import Column, String, Boolean, DateTime, Index
from sqlalchemy.sql import func
from core.database import Base


class VaultDomain(Base):
    """
    Custom domain configuration for vault share pages.
    Each vault can have one custom domain.
    """
    __tablename__ = "vault_domains"
    
    # Primary key - auto-increment ID
    id = Column(String(36), primary_key=True)
    
    # User UID from Firebase Auth - indexed for fast lookup
    uid = Column(String(128), nullable=False, index=True)
    
    # Vault name (safe identifier)
    vault_name = Column(String(255), nullable=False, index=True)
    
    # Share token for this vault (used to load the share page)
    share_token = Column(String(128), nullable=True)
    
    # Custom domain hostname (e.g., "gallery.example.com")
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
        Index('ix_vault_domains_hostname_verified', 'hostname', 'dns_verified'),
        Index('ix_vault_domains_uid_vault', 'uid', 'vault_name'),
    )
    
    def to_dict(self):
        """Convert to dict for API responses"""
        return {
            "hostname": self.hostname,
            "vaultName": self.vault_name,
            "shareToken": self.share_token,
            "dnsVerified": self.dns_verified,
            "sslStatus": self.ssl_status,
            "cnameObserved": self.cname_observed,
            "enabled": self.enabled,
            "lastError": self.last_error,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
            "lastChecked": self.last_checked.isoformat() if self.last_checked else None,
        }
