"""
Vault Password Backup model for PostgreSQL (Neon)
Stores encrypted vault passwords for recovery purposes
"""
from sqlalchemy import Column, String, Text, DateTime, Integer, Boolean, ForeignKey
from sqlalchemy.sql import func
from core.database import Base


class VaultPasswordBackup(Base):
    """
    Stores encrypted vault passwords for recovery.
    Passwords are encrypted with a key derived from the user's account.
    """
    __tablename__ = "vault_password_backups"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # User identification
    owner_uid = Column(String(128), ForeignKey("users.uid", ondelete="CASCADE"), index=True, nullable=False)
    
    # Vault identification
    vault_name = Column(String(255), nullable=False, index=True)
    
    # Encrypted password (using Fernet symmetric encryption)
    encrypted_password = Column(Text, nullable=False)
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    
    def to_dict(self):
        """Convert to dict for API responses (excludes encrypted password)"""
        return {
            "id": self.id,
            "vaultName": self.vault_name,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
        }


class VaultPasswordRecoveryOTP(Base):
    """
    Stores OTP codes for vault password recovery
    """
    __tablename__ = "vault_password_recovery_otp"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    
    # User identification
    uid = Column(String(128), ForeignKey("users.uid", ondelete="CASCADE"), index=True, nullable=False)
    email = Column(String(255), index=True, nullable=False)
    
    # OTP code
    code = Column(String(10), nullable=False)
    
    # Verification status
    verified = Column(Boolean, default=False)
    
    # Expiration
    expires_at = Column(DateTime(timezone=True), nullable=False)
    
    # Usage tracking
    attempts = Column(Integer, default=0)
    max_attempts = Column(Integer, default=5)
    used = Column(Boolean, default=False)
    
    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
