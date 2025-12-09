"""
User and account models for PostgreSQL
Replaces Firestore 'users' collection
"""
from sqlalchemy import Column, String, Text, JSON, DateTime, Integer, Boolean, Float
from sqlalchemy.sql import func
from core.database import Base

class User(Base):
    __tablename__ = "users"
    
    # Primary key - Firebase Auth UID
    uid = Column(String(128), primary_key=True, index=True)
    
    # Basic info
    email = Column(String(255), unique=True, index=True, nullable=False)
    secondary_email = Column(String(255), nullable=True, index=True)  # Backup/recovery email
    display_name = Column(String(255), nullable=True)
    photo_url = Column(Text, nullable=True)
    
    # Account type and metadata
    account_type = Column(String(50), default="individual")  # individual, business
    referral_source = Column(String(100), nullable=True)
    company_name = Column(String(255), nullable=True)
    company_size = Column(String(50), nullable=True)
    company_revenue = Column(String(50), nullable=True)
    
    # Subscription and billing
    plan = Column(String(50), default="free")  # free, pro, business, enterprise
    stripe_customer_id = Column(String(255), nullable=True, index=True)
    subscription_id = Column(String(255), nullable=True)
    subscription_status = Column(String(50), nullable=True)
    subscription_end_date = Column(DateTime(timezone=True), nullable=True)
    
    # Usage and limits
    storage_used_bytes = Column(Integer, default=0)
    storage_limit_bytes = Column(Integer, default=1073741824)  # 1GB default
    monthly_uploads = Column(Integer, default=0)
    monthly_upload_limit = Column(Integer, default=100)
    
    # Affiliate tracking
    affiliate_code = Column(String(50), unique=True, index=True, nullable=True)
    referred_by = Column(String(50), nullable=True, index=True)
    affiliate_earnings = Column(Float, default=0.0)
    
    # Account status
    is_active = Column(Boolean, default=True)
    is_admin = Column(Boolean, default=False)
    email_verified = Column(Boolean, default=False)
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    
    # Login IP tracking for security notifications
    last_login_ip = Column(String(45), nullable=True)  # IPv6 max length is 45 chars
    
    # Additional metadata as JSON
    extra_metadata = Column(JSON, default={})
    
    # Brand Kit
    brand_logo_url = Column(Text, nullable=True)
    brand_primary_color = Column(String(20), nullable=True)
    brand_secondary_color = Column(String(20), nullable=True)
    brand_accent_color = Column(String(20), nullable=True)
    brand_background_color = Column(String(20), nullable=True)
    brand_text_color = Column(String(20), nullable=True)
    brand_slogan = Column(String(255), nullable=True)
    brand_font_family = Column(String(100), nullable=True)
    brand_custom_font_url = Column(Text, nullable=True)
    brand_custom_font_name = Column(String(100), nullable=True)
    
    def to_dict(self):
        """Convert to dict for API responses"""
        return {
            "uid": self.uid,
            "email": self.email,
            "secondaryEmail": self.secondary_email,
            "displayName": self.display_name,
            "photoUrl": self.photo_url,
            "accountType": self.account_type,
            "referralSource": self.referral_source,
            "companyName": self.company_name,
            "companySize": self.company_size,
            "plan": self.plan,
            "stripeCustomerId": self.stripe_customer_id,
            "subscriptionStatus": self.subscription_status,
            "storageUsed": self.storage_used_bytes,
            "storageLimit": self.storage_limit_bytes,
            "affiliateCode": self.affiliate_code,
            "referredBy": self.referred_by,
            "affiliateEarnings": self.affiliate_earnings,
            "isActive": self.is_active,
            "emailVerified": self.email_verified,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
            "lastLoginAt": self.last_login_at.isoformat() if self.last_login_at else None,
            "metadata": self.extra_metadata or {}
        }


