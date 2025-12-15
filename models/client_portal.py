"""
Client Portal Models
Allows photographer's clients to have accounts and view their galleries, purchases, and downloads
"""
from datetime import datetime
from typing import Optional
from sqlalchemy import Column, String, Text, DateTime, Boolean, Integer, ForeignKey, JSON
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import UUID
import uuid

from core.database import Base


class ClientAccount(Base):
    """Client accounts - clients of photographers"""
    __tablename__ = "client_accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    photographer_uid = Column(String(128), nullable=False, index=True)  # The photographer who owns this client
    
    # Client info
    email = Column(String(255), nullable=False, index=True)
    name = Column(String(255), nullable=True)
    phone = Column(String(50), nullable=True)
    
    # Authentication
    password_hash = Column(String(255), nullable=True)  # bcrypt hash
    magic_link_token = Column(String(255), nullable=True, index=True)
    magic_link_expires = Column(DateTime, nullable=True)
    
    # Status
    is_active = Column(Boolean, default=True)
    email_verified = Column(Boolean, default=False)
    
    # Metadata
    avatar_url = Column(Text, nullable=True)
    notes = Column(Text, nullable=True)  # Photographer's notes about client
    tags = Column(JSON, default=list)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)
    
    # Relationships
    gallery_access = relationship("ClientGalleryAccess", back_populates="client", cascade="all, delete-orphan")
    downloads = relationship("ClientDownload", back_populates="client", cascade="all, delete-orphan")
    
    def to_dict(self):
        return {
            "id": str(self.id),
            "photographer_uid": self.photographer_uid,
            "email": self.email,
            "name": self.name,
            "phone": self.phone,
            "is_active": self.is_active,
            "email_verified": self.email_verified,
            "avatar_url": self.avatar_url,
            "notes": self.notes,
            "tags": self.tags or [],
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }


class ClientGalleryAccess(Base):
    """Links clients to galleries/vaults they can access"""
    __tablename__ = "client_gallery_access"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(UUID(as_uuid=True), ForeignKey("client_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    photographer_uid = Column(String(128), nullable=False, index=True)
    
    # Gallery/Vault reference
    vault_name = Column(String(255), nullable=False)
    share_token = Column(String(255), nullable=True, index=True)  # Link to existing share
    
    # Access settings
    can_download = Column(Boolean, default=True)
    can_favorite = Column(Boolean, default=True)
    can_comment = Column(Boolean, default=True)
    
    # Display
    display_name = Column(String(255), nullable=True)  # Custom name shown to client
    cover_image_url = Column(Text, nullable=True)
    
    # Timestamps
    granted_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=True)
    last_viewed_at = Column(DateTime, nullable=True)
    
    # Relationships
    client = relationship("ClientAccount", back_populates="gallery_access")
    
    def to_dict(self):
        return {
            "id": str(self.id),
            "client_id": str(self.client_id),
            "vault_name": self.vault_name,
            "share_token": self.share_token,
            "can_download": self.can_download,
            "can_favorite": self.can_favorite,
            "can_comment": self.can_comment,
            "display_name": self.display_name,
            "cover_image_url": self.cover_image_url,
            "granted_at": self.granted_at.isoformat() if self.granted_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "last_viewed_at": self.last_viewed_at.isoformat() if self.last_viewed_at else None,
        }


class ClientDownload(Base):
    """Track client download history"""
    __tablename__ = "client_downloads"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(UUID(as_uuid=True), ForeignKey("client_accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    photographer_uid = Column(String(128), nullable=False, index=True)
    
    # What was downloaded
    vault_name = Column(String(255), nullable=False)
    photo_key = Column(String(512), nullable=True)  # Null if full gallery download
    download_type = Column(String(50), default="single")  # single, zip, all
    
    # Download details
    file_size_bytes = Column(Integer, nullable=True)
    ip_address = Column(String(45), nullable=True)
    user_agent = Column(Text, nullable=True)
    
    # Timestamps
    downloaded_at = Column(DateTime, default=datetime.utcnow, index=True)
    
    # Relationships
    client = relationship("ClientAccount", back_populates="downloads")
    
    def to_dict(self):
        return {
            "id": str(self.id),
            "vault_name": self.vault_name,
            "photo_key": self.photo_key,
            "download_type": self.download_type,
            "file_size_bytes": self.file_size_bytes,
            "downloaded_at": self.downloaded_at.isoformat() if self.downloaded_at else None,
        }


class ClientPurchase(Base):
    """Track client purchase history"""
    __tablename__ = "client_purchases"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id = Column(UUID(as_uuid=True), ForeignKey("client_accounts.id", ondelete="SET NULL"), nullable=True, index=True)
    client_email = Column(String(255), nullable=False, index=True)  # Keep even if client deleted
    photographer_uid = Column(String(128), nullable=False, index=True)
    
    # Purchase details
    vault_name = Column(String(255), nullable=True)
    share_token = Column(String(255), nullable=True)
    purchase_type = Column(String(50), default="license")  # license, print, digital
    
    # Payment info
    amount_cents = Column(Integer, default=0)
    currency = Column(String(3), default="USD")
    payment_provider = Column(String(50), nullable=True)  # stripe, dodo, etc
    payment_id = Column(String(255), nullable=True)  # External payment ID
    
    # Status
    status = Column(String(50), default="completed")  # pending, completed, refunded
    
    # Timestamps
    purchased_at = Column(DateTime, default=datetime.utcnow, index=True)
    
    def to_dict(self):
        return {
            "id": str(self.id),
            "vault_name": self.vault_name,
            "purchase_type": self.purchase_type,
            "amount_cents": self.amount_cents,
            "currency": self.currency,
            "status": self.status,
            "purchased_at": self.purchased_at.isoformat() if self.purchased_at else None,
        }
