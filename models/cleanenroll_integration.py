"""
Cleanenroll Integration model for PostgreSQL
Stores OAuth tokens and integration settings per user
"""
from sqlalchemy import Column, String, Text, JSON, DateTime, Boolean, Index
from sqlalchemy.sql import func
from core.database import Base


class CleanenrollIntegration(Base):
    __tablename__ = "cleanenroll_integrations"
    
    # Primary key - Firebase Auth UID
    uid = Column(String(128), primary_key=True, index=True)
    
    # OAuth tokens
    access_token = Column(Text, nullable=True)
    refresh_token = Column(Text, nullable=True)
    token_type = Column(String(50), default="Bearer")
    expires_at = Column(DateTime(timezone=True), nullable=True)
    
    # Account info from Cleanenroll
    cleanenroll_user_id = Column(String(255), nullable=True, index=True)
    cleanenroll_email = Column(String(255), nullable=True)
    cleanenroll_name = Column(String(255), nullable=True)
    organization_id = Column(String(255), nullable=True)
    organization_name = Column(String(255), nullable=True)
    
    # Integration status
    is_connected = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    
    # Webhook configuration
    webhook_secret = Column(String(255), nullable=True)
    webhook_enabled = Column(Boolean, default=True)
    
    # Cached data (forms, analytics summary)
    cached_forms = Column(JSON, default=[])
    cached_analytics = Column(JSON, default={})
    cache_updated_at = Column(DateTime(timezone=True), nullable=True)
    
    # Timestamps
    connected_at = Column(DateTime(timezone=True), nullable=True)
    disconnected_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # Additional settings
    settings = Column(JSON, default={})
    
    def to_dict(self):
        """Convert to dict for API responses (excludes sensitive tokens)"""
        return {
            "uid": self.uid,
            "isConnected": self.is_connected,
            "isActive": self.is_active,
            "cleanenrollUserId": self.cleanenroll_user_id,
            "cleanenrollEmail": self.cleanenroll_email,
            "cleanenrollName": self.cleanenroll_name,
            "organizationId": self.organization_id,
            "organizationName": self.organization_name,
            "webhookEnabled": self.webhook_enabled,
            "connectedAt": self.connected_at.isoformat() if self.connected_at else None,
            "cacheUpdatedAt": self.cache_updated_at.isoformat() if self.cache_updated_at else None,
            "settings": self.settings or {}
        }
