"""
User Security models for PostgreSQL (Neon)
Stores 2FA settings, backup codes, and recovery options
"""
from sqlalchemy import Column, String, Text, JSON, DateTime, Integer, Boolean, ForeignKey
from sqlalchemy.sql import func
from core.database import Base


class UserSecurity(Base):
    """
    Stores user security settings including 2FA and recovery options
    """
    __tablename__ = "user_security"
    
    # Primary key - Firebase Auth UID
    uid = Column(String(128), ForeignKey("users.uid", ondelete="CASCADE"), primary_key=True, index=True)
    
    # Secondary email for recovery
    secondary_email = Column(String(255), nullable=True, index=True)
    secondary_email_verified = Column(Boolean, default=False)
    
    # 2FA Phone number
    phone_number = Column(String(20), nullable=True)
    phone_verified = Column(Boolean, default=False)
    phone_country_code = Column(String(5), nullable=True)
    
    # 2FA Settings
    two_factor_enabled = Column(Boolean, default=False)
    two_factor_method = Column(String(20), nullable=True)  # 'app', 'sms', 'email'
    totp_secret = Column(String(64), nullable=True)  # For authenticator apps
    
    # Backup codes (stored as JSON array of hashed codes)
    backup_codes = Column(JSON, default=[])
    backup_codes_generated_at = Column(DateTime(timezone=True), nullable=True)
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    def to_dict(self):
        """Convert to dict for API responses (excludes sensitive data)"""
        return {
            "uid": self.uid,
            "secondaryEmail": self.secondary_email,
            "secondaryEmailVerified": self.secondary_email_verified,
            "phoneNumber": self._mask_phone(self.phone_number) if self.phone_number else None,
            "phoneVerified": self.phone_verified,
            "twoFactorEnabled": self.two_factor_enabled,
            "twoFactorMethod": self.two_factor_method,
            "hasBackupCodes": bool(self.backup_codes and len(self.backup_codes) > 0),
            "backupCodesCount": len(self.backup_codes) if self.backup_codes else 0,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
        }
    
    def _mask_phone(self, phone: str) -> str:
        """Mask phone number for display"""
        if not phone or len(phone) < 7:
            return "****"
        return phone[:3] + "****" + phone[-4:]


class PasswordResetRequest(Base):
    """
    Stores password reset requests and OTP codes
    """
    __tablename__ = "password_reset_requests"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # User identification
    uid = Column(String(128), ForeignKey("users.uid", ondelete="CASCADE"), index=True, nullable=False)
    email = Column(String(255), index=True, nullable=False)
    
    # Reset code/token
    code = Column(String(64), nullable=False)
    
    # Verification status
    verified = Column(Boolean, default=False)
    
    # Method used for reset
    method = Column(String(20), default="email")  # 'email', 'secondary_email', 'backup_code', 'sms'
    
    # Expiration
    expires_at = Column(DateTime(timezone=True), nullable=False)
    
    # Usage tracking
    used = Column(Boolean, default=False)
    used_at = Column(DateTime(timezone=True), nullable=True)
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SMSVerificationCode(Base):
    """
    Stores temporary SMS verification codes
    """
    __tablename__ = "sms_verification_codes"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # User identification
    uid = Column(String(128), ForeignKey("users.uid", ondelete="CASCADE"), index=True, nullable=False)
    
    # Verification code
    code = Column(String(10), nullable=False)
    
    # Purpose
    purpose = Column(String(30), default="password_reset")  # 'password_reset', '2fa_setup', 'login'
    
    # Phone number sent to
    phone_number = Column(String(20), nullable=False)
    
    # Expiration
    expires_at = Column(DateTime(timezone=True), nullable=False)
    
    # Usage tracking
    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=3)
    used = Column(Boolean, default=False)
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
