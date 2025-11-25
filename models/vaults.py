"""
Vault models for PostgreSQL (Neon)
Stores vault metadata - photos still stored in R2
"""
from sqlalchemy import Column, String, Text, JSON, DateTime, Integer, Boolean
from sqlalchemy.sql import func
from core.database import Base

class Vault(Base):
    """
    Vault metadata storage
    - Photo files remain in R2
    - Metadata (settings, branding, etc.) stored here
    """
    __tablename__ = "vaults"
    
    # Composite primary key: uid + vault_name
    id = Column(Integer, primary_key=True, autoincrement=True)
    uid = Column(String(128), nullable=False, index=True)
    vault_name = Column(String(255), nullable=False, index=True)
    
    # Display and branding
    display_name = Column(String(255), nullable=True)
    logo_url = Column(Text, nullable=True)
    welcome_message = Column(Text, nullable=True)
    
    # Protection settings
    protected = Column(Boolean, default=False)
    password_hash = Column(String(255), nullable=True)
    
    # Share customization
    share_hide_ui = Column(Boolean, default=False)
    share_color = Column(String(50), nullable=True)
    share_layout = Column(String(20), default="grid")  # 'grid' or 'masonry'
    
    # Licensing
    license_price_cents = Column(Integer, default=0)
    license_currency = Column(String(10), default="USD")
    
    # Channel and communication
    channel_url = Column(Text, nullable=True)
    
    # Additional metadata stored as JSON
    # - descriptions: dict of photo_key -> description
    # - slideshow: list of slideshow items
    # - order: custom photo ordering
    # - system_vault: special vault type (e.g., "favorites")
    metadata = Column(JSON, default={})
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    def __repr__(self):
        return f"<Vault(uid={self.uid}, name={self.vault_name})>"
    
    def to_dict(self):
        """Convert to dict for API responses"""
        return {
            "name": self.vault_name,
            "display_name": self.display_name,
            "logo_url": self.logo_url,
            "welcome_message": self.welcome_message,
            "protected": self.protected,
            "share_hide_ui": self.share_hide_ui,
            "share_color": self.share_color,
            "share_layout": self.share_layout,
            "license_price_cents": self.license_price_cents,
            "license_currency": self.license_currency,
            "channel_url": self.channel_url,
            "metadata": self.metadata or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
